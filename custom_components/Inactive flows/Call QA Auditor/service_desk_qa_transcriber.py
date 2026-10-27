from __future__ import annotations

import re

from lfx.custom.custom_component.component import Component
from lfx.io import (
    FileInput,
    DropdownInput,
    BoolInput,
    IntInput,
    StrInput,
    MultilineInput,
    Output,
)
from lfx.schema.data import Data
from lfx.schema.message import Message
from faster_whisper import WhisperModel


# ============================================================
# Module-level model cache (persists across component runs)
# ============================================================
_MODEL_CACHE = {}


def _get_model(size: str, device: str, compute_type: str, cpu_threads: int):
    key = (size, device, compute_type, cpu_threads)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(
            size,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=1,
        )
    return _MODEL_CACHE[key]


# ============================================================
# Detection vocabularies
# ============================================================
HOLD_ANNOUNCE = [
    "place you on hold", "put you on hold", "on hold for",
    "pop you on hold", "place you on a brief hold", "hold for a moment",
    "bear with me", "give me a moment", "give me a second",
    "one moment please", "just a moment", "one second",
    "let me check", "let me look into", "stay on the line",
]

HOLD_RETURN = [
    "thank you for holding", "thanks for holding",
    "thank you for being on hold", "thanks for waiting",
    "thank you for your patience", "thanks for your patience",
    "sorry for the wait", "sorry about the wait",
    "are you still there", "still with me", "still there",
]

CREDENTIAL_TRIGGERS = re.compile(
    r"(passwords?|pass\s?codes?|passphrase|\bpin\b|"
    r"temporary password|your username is|username is|"
    r"log\s?in details|credentials|security (?:code|answer|question)|"
    r"one[- ]time (?:code|password)|\botp\b|verification code)",
    re.IGNORECASE,
)

AGENT_CUES = [
    "my name is", "may i", "thank you for calling", "one moment",
    "i'll be glad", "i will be glad", "could you help me", "can you confirm",
    "please note", "is there anything else", "have a wonderful day",
    "have a great day", "thank you for being on hold", "bear with me",
    "let me check", "i can help", "happy to help", "may i know",
    "thank you for confirming", "could you please", "help me with",
]

IVR_CUES = [
    "your call may be recorded", "for training purposes",
    "please have your", "connect you to", "all our agents",
    "press one", "press two", "calls are recorded",
]

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20, "thirty": 30, "couple": 2, "few": 3,
}

_SPELLED_DIGIT = (
    r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|"
    r"double|triple)\b"
)

_PHONETIC_RUN = re.compile(
    r"((?:\b[\w']+\s+as\s+(?:in|an)\s+[\w']+[\s,.\-]*){2,})",
    re.IGNORECASE,
)


# ============================================================
# Redaction
# ============================================================
def redact_text(text: str) -> tuple[str, list[str]]:
    """Best-effort removal of credentials and identifiers.

    Returns (redacted_text, list_of_event_types).
    NOT a compliance control - see caveats.
    """
    hits: list[str] = []
    out = text

    # 1. Phonetic spelling runs: "s as in sam, e as in echo, r as in robert"
    if len(_PHONETIC_RUN.findall(out)) > 0 and \
            len(re.findall(r"\bas\s+(?:in|an)\b", out, re.IGNORECASE)) >= 2:
        out = _PHONETIC_RUN.sub(" [REDACTED_SPELLED_CREDENTIAL] ", out)
        hits.append("phonetic_credential")

    # 2. Everything following a credential trigger word
    if CREDENTIAL_TRIGGERS.search(out):
        out = CREDENTIAL_TRIGGERS.sub(
            lambda m: f"{m.group(0)} [REDACTED]", out
        )
        hits.append("credential_phrase")

    # 3. Long numeric runs (IDs, phone numbers, registration numbers)
    out, n = re.subn(r"\b\d{4,}\b", "[REDACTED_NUMBER]", out)
    if n:
        hits.append("numeric_id")

    # 4. Alphanumeric identifiers e.g. "2180413E", "AB12CD34"
    out, n = re.subn(
        r"\b(?=[A-Za-z0-9]{5,})(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+\b",
        "[REDACTED_ID]",
        out,
    )
    if n:
        hits.append("alphanumeric_id")

    # 5. Spoken digit sequences
    if len(re.findall(_SPELLED_DIGIT, out, re.IGNORECASE)) >= 4:
        out = re.sub(
            rf"((?:{_SPELLED_DIGIT}[\s,.\-]*){{4,}})",
            " [REDACTED_SPOKEN_DIGITS] ",
            out,
            flags=re.IGNORECASE,
        )
        hits.append("spoken_digits")

    # 6. Email addresses
    out, n = re.subn(
        r"\b[\w.\-+]+\s?(?:@|\bat\b)\s?[\w.\-]+\.\w{2,}\b",
        "[REDACTED_EMAIL]",
        out,
    )
    if n:
        hits.append("email")

    out = re.sub(r"\s{2,}", " ", out).strip()
    return out, hits


def _promised_seconds(text: str) -> int | None:
    """Extract a promised hold duration from an announcement phrase."""
    m = re.search(
        r"(\d+|a|an|one|two|three|four|five|ten|fifteen|twenty|couple|few)"
        r"[\s\-]*(?:of\s+)?(minute|minutes|min|second|seconds|sec)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    raw, unit = m.group(1).lower(), m.group(2).lower()
    val = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    if val is None:
        return None
    return val * 60 if unit.startswith("min") else val


# ============================================================
# Component
# ============================================================
class CustomComponent(Component):
    display_name = "Service Desk QA Transcriber"
    description = (
        "Accurate call transcription with word-level hold detection, "
        "PII redaction and an audit-ready mission payload."
    )

    inputs = [
        FileInput(
            name="audio_file",
            display_name="Audio File",
            file_types=["mp3", "wav", "webm", "m4a", "ogg", "flac"],
            required=True,
        ),
        DropdownInput(
            name="model_size",
            display_name="Model Size",
            options=[
                "tiny.en", "base.en", "small.en", "medium.en",
                "distil-small.en", "distil-medium.en", "distil-large-v3",
                "large-v3", "large-v3-turbo",
            ],
            value="large-v3-turbo",
            info="large-v3-turbo or distil-large-v3 recommended for QA work.",
        ),
        DropdownInput(
            name="device",
            display_name="Device",
            options=["auto", "cpu", "cuda"],
            value="auto",
            advanced=True,
        ),
        DropdownInput(
            name="compute_type",
            display_name="Compute Type",
            options=["int8", "int8_float16", "float16", "float32"],
            value="int8",
            info="int8 for CPU, float16 for GPU.",
            advanced=True,
        ),
        IntInput(
            name="cpu_threads",
            display_name="CPU Threads",
            value=0,
            info="0 = all available cores.",
            advanced=True,
        ),
        StrInput(
            name="language",
            display_name="Language",
            value="en",
            info="Pin to skip detection. Blank = auto-detect.",
            advanced=True,
        ),
        IntInput(
            name="beam_size",
            display_name="Beam Size",
            value=5,
            advanced=True,
        ),
        BoolInput(
            name="use_vad",
            display_name="Use VAD Filter",
            value=True,
            info="Removes non-speech and prevents hallucination.",
            advanced=True,
        ),
        IntInput(
            name="silence_threshold",
            display_name="Hold Threshold (seconds)",
            value=15,
            info="Word-level gaps at or above this are flagged as holds.",
        ),
        IntInput(
            name="dead_air_threshold",
            display_name="Dead Air Threshold (seconds)",
            value=5,
            info="Shorter pauses counted separately as dead air.",
            advanced=True,
        ),
        BoolInput(
            name="redact_pii",
            display_name="Redact Credentials / PII",
            value=True,
            info="Strongly recommended. Masks passwords, IDs and phone numbers.",
        ),
        BoolInput(
            name="merge_turns",
            display_name="Merge Segments into Turns",
            value=True,
            advanced=True,
        ),
        IntInput(
            name="turn_gap",
            display_name="Turn Break Gap (seconds)",
            value=2,
            advanced=True,
        ),
        BoolInput(
            name="label_speakers",
            display_name="Heuristic Speaker Labels",
            value=True,
            info="Rule-based guess only. Not true diarization.",
            advanced=True,
        ),
        MultilineInput(
            name="initial_prompt",
            display_name="Domain Prompt",
            value=(
                "Hello, thank you for calling the service desk. My name is Alex. "
                "May I take your name, please? Of course - let me check that "
                "ticket for you. I'll place you on hold for two minutes while I "
                "verify your Active Directory account and reset the VPN access."
            ),
            info=(
                "A short, well-punctuated in-domain sample. Whisper mimics its "
                "style, which restores punctuation and casing."
            ),
            advanced=True,
        ),
        MultilineInput(
            name="audit_instructions",
            display_name="Audit Mission Instructions",
            value=(
                "You are a Service Desk QA auditor. Audit the call transcript below.\n\n"
                "Score each category 1-5 and cite a timestamp for every "
                "justification:\n"
                "1. Greeting & Identification\n"
                "2. Active Listening & Empathy\n"
                "3. Technical Accuracy\n"
                "4. Hold & Dead-Air Handling\n"
                "5. Security & Credential Handling\n"
                "6. Resolution, Next Steps & Closing\n\n"
                "Then provide:\n"
                "- Overall score (1-5)\n"
                "- Verdict: PASS / REVIEW / FAIL\n"
                "- Top 3 coaching points\n"
                "- Any compliance or security risk flags\n\n"
                "Treat the AUTOMATED SIGNALS block as measured fact. Do not "
                "estimate hold durations from timestamps yourself.\n\n"
                'Return JSON: {"scores": {}, "overall": 0, "verdict": "", '
                '"coaching": [], "flags": [], "summary": ""}'
            ),
        ),
        IntInput(
            name="max_transcript_chars",
            display_name="Max Transcript Characters",
            value=60000,
            info="0 = no limit.",
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Mission Payload", name="mission_payload",
               method="build_mission_payload"),
        Output(display_name="QA Report", name="report",
               method="build_report"),
        Output(display_name="Structured Data", name="structured",
               method="build_structured"),
    ]

    # -------------------- helpers --------------------
    @staticmethod
    def _ts(seconds: float) -> str:
        m, s = divmod(int(max(seconds, 0)), 60)
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _dur(seconds: float) -> str:
        m, s = divmod(int(max(seconds, 0)), 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def _merge(self, segments: list[dict]) -> list[dict]:
        if not segments:
            return []
        gap_limit = float(self.turn_gap)
        turns = []
        cur = dict(segments[0])
        for seg in segments[1:]:
            gap = seg["start"] - cur["end"]
            too_long = (cur["end"] - cur["start"]) > 30
            if gap <= gap_limit and not too_long:
                cur["text"] = f"{cur['text']} {seg['text']}".strip()
                cur["end"] = seg["end"]
            else:
                turns.append(cur)
                cur = dict(seg)
        turns.append(cur)
        return turns

    @staticmethod
    def _guess_speaker(text: str, start: float, prev: str) -> str:
        low = text.lower()
        if start < 35 and any(c in low for c in IVR_CUES):
            return "IVR"
        if any(c in low for c in AGENT_CUES):
            return "AGENT"
        if len(text.split()) <= 6:
            return "CALLER" if prev in ("AGENT", "IVR") else prev or "CALLER"
        if prev == "AGENT":
            return "CALLER"
        if prev in ("CALLER", "IVR"):
            return "AGENT"
        return "AGENT"

    # -------------------- core --------------------
    def _transcribe(self) -> dict:
        if hasattr(self, "_cached_result"):
            return self._cached_result

        device = self.device
        compute_type = self.compute_type
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        if device == "cuda" and compute_type == "int8":
            compute_type = "float16"
        if device == "cpu" and compute_type in ("float16", "int8_float16"):
            compute_type = "int8"

        model = _get_model(
            self.model_size,
            device,
            compute_type,
            int(self.cpu_threads) if self.cpu_threads else 0,
        )

        segments_gen, info = model.transcribe(
            self.audio_file,
            beam_size=int(self.beam_size),
            language=(self.language or "").strip() or None,
            vad_filter=bool(self.use_vad),
            vad_parameters={
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 200,
            },
            initial_prompt=(self.initial_prompt or "").strip() or None,
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            word_timestamps=True,          # required for hold detection
        )

        hold_threshold = float(self.silence_threshold)
        dead_threshold = float(self.dead_air_threshold)

        raw_segments: list[dict] = []
        hold_events: list[dict] = []
        dead_air_events: list[dict] = []
        hold_announcements: list[dict] = []
        hold_returns: list[dict] = []
        credential_events: list[dict] = []
        repeats: list[dict] = []
        low_conf: list[dict] = []
        pii_types: set[str] = set()

        seen_texts: dict[str, float] = {}
        prev_word_end: float | None = None
        word_ts_available = False
        first_start: float | None = None
        last_end: float | None = None
        total_word_count = 0

        for seg in segments_gen:
            text = (seg.text or "").strip()
            if not text:
                continue

            raw_lower = text.lower()

            if first_start is None:
                first_start = seg.start
            last_end = seg.end

            # ---- hold phrase cross-reference (on RAW text) ----
            for p in HOLD_ANNOUNCE:
                if p in raw_lower:
                    hold_announcements.append({
                        "time": seg.start,
                        "phrase": p,
                        "promised_sec": _promised_seconds(text),
                    })
                    break
            for p in HOLD_RETURN:
                if p in raw_lower:
                    hold_returns.append({"time": seg.start, "phrase": p})
                    break

            # ---- credential exposure window (on RAW text) ----
            if CREDENTIAL_TRIGGERS.search(text):
                credential_events.append({
                    "start": seg.start,
                    "end": seg.end,
                })

            # ---- word-level gap detection ----
            words = getattr(seg, "words", None) or []
            if words:
                word_ts_available = True
                for w in words:
                    if not (w.word or "").strip():
                        continue
                    total_word_count += 1
                    if prev_word_end is not None:
                        gap = w.start - prev_word_end
                        if gap >= hold_threshold:
                            hold_events.append({
                                "start": prev_word_end,
                                "end": w.start,
                                "duration": round(gap, 1),
                                "source": "word_gap",
                            })
                        elif gap >= dead_threshold:
                            dead_air_events.append({
                                "start": prev_word_end,
                                "end": w.start,
                                "duration": round(gap, 1),
                            })
                    prev_word_end = w.end
            else:
                # Fallback: abnormally slow segment implies swallowed silence
                seg_dur = seg.end - seg.start
                wc = len(text.split())
                total_word_count += wc
                wps = (wc / seg_dur) if seg_dur else 99
                if seg_dur > 20 and wps < 0.8:
                    hold_events.append({
                        "start": seg.start,
                        "end": seg.end,
                        "duration": round(seg_dur, 1),
                        "source": "inferred_slow_segment",
                        "note": "INFERRED - verify manually",
                    })
                if prev_word_end is not None:
                    gap = seg.start - prev_word_end
                    if gap >= hold_threshold:
                        hold_events.append({
                            "start": prev_word_end,
                            "end": seg.start,
                            "duration": round(gap, 1),
                            "source": "segment_gap",
                        })
                prev_word_end = seg.end

            # ---- confidence ----
            if getattr(seg, "avg_logprob", 0.0) < -0.9:
                low_conf.append({"time": seg.start, "text": "[flagged passage]"})

            # ---- repetition (on RAW text, before redaction) ----
            norm = re.sub(r"[^\w\s]", "", raw_lower).strip()
            if len(norm) > 10:
                if norm in seen_texts:
                    repeats.append({
                        "time": seg.start,
                        "first_seen": seen_texts[norm],
                    })
                else:
                    seen_texts[norm] = seg.start

            # ---- redaction before text is retained ----
            if self.redact_pii:
                text, hits = redact_text(text)
                pii_types.update(hits)

            raw_segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": text,
            })

        # ---- merge / label ----
        turns = self._merge(raw_segments) if self.merge_turns else raw_segments
        prev_label = ""
        for t in turns:
            if self.label_speakers:
                t["speaker"] = self._guess_speaker(
                    t["text"], t["start"], prev_label
                )
                prev_label = t["speaker"]
            else:
                t["speaker"] = ""

        # ---- inject hold markers into the readable transcript ----
        lines = []
        hold_sorted = sorted(hold_events, key=lambda e: e["start"])
        hi = 0
        for t in turns:
            while hi < len(hold_sorted) and hold_sorted[hi]["start"] < t["start"]:
                h = hold_sorted[hi]
                lines.append(
                    f"        ----- HOLD / SILENCE: {self._dur(h['duration'])} "
                    f"({self._ts(h['start'])} -> {self._ts(h['end'])}) -----"
                )
                hi += 1
            prefix = f"{t['speaker']}: " if t["speaker"] else ""
            lines.append(f"[{self._ts(t['start'])}] {prefix}{t['text']}")
        while hi < len(hold_sorted):
            h = hold_sorted[hi]
            lines.append(
                f"        ----- HOLD / SILENCE: {self._dur(h['duration'])} "
                f"({self._ts(h['start'])} -> {self._ts(h['end'])}) -----"
            )
            hi += 1
        clean_transcript = "\n".join(lines)

        # ---- metrics ----
        speech_span = (last_end - first_start) if first_start is not None else 0.0
        audio_duration = getattr(info, "duration", None) or speech_span or 0.0
        total_hold = sum(e["duration"] for e in hold_events)
        total_dead = sum(e["duration"] for e in dead_air_events)
        longest_hold = max((e["duration"] for e in hold_events), default=0.0)

        # ---- discrepancy: announced vs measured ----
        discrepancies = []
        for ann in hold_announcements:
            match = next(
                (h for h in hold_events
                 if -10 <= (h["start"] - ann["time"]) <= 60), None
            )
            if match and ann["promised_sec"]:
                over = match["duration"] - ann["promised_sec"]
                if over > 20:
                    discrepancies.append(
                        f"Agent promised ~{self._dur(ann['promised_sec'])} at "
                        f"{self._ts(ann['time'])} but hold lasted "
                        f"{self._dur(match['duration'])} "
                        f"(+{self._dur(over)} over)"
                    )
            elif not match:
                discrepancies.append(
                    f"Hold announced at {self._ts(ann['time'])} "
                    f"(\"{ann['phrase']}\") but no matching silence was "
                    f"measured - verify audio"
                )
        for h in hold_events:
            if not any(-60 <= (h["start"] - a["time"]) <= 10
                       for a in hold_announcements):
                discrepancies.append(
                    f"Unannounced silence of {self._dur(h['duration'])} at "
                    f"{self._ts(h['start'])} - no hold warning detected"
                )

        # ---- check-in during hold ----
        no_checkin = []
        for h in hold_events:
            if h["duration"] >= 60:
                inside = any(h["start"] < r["time"] < h["end"]
                             for r in hold_returns)
                if not inside:
                    no_checkin.append(
                        f"No check-in during {self._dur(h['duration'])} hold "
                        f"starting {self._ts(h['start'])}"
                    )

        # ---- credential window summary ----
        cred_windows = []
        if credential_events:
            cur = dict(credential_events[0])
            for c in credential_events[1:]:
                if c["start"] - cur["end"] <= 15:
                    cur["end"] = c["end"]
                else:
                    cred_windows.append(cur)
                    cur = dict(c)
            cred_windows.append(cur)

        self._cached_result = {
            "clean_transcript": clean_transcript,
            "turn_count": len(turns),
            "segment_count": len(raw_segments),
            "word_count": total_word_count,
            "language": getattr(info, "language", self.language),
            "language_probability": round(
                getattr(info, "language_probability", 0.0), 3
            ),
            "audio_duration_sec": round(audio_duration, 1),
            "speech_span_sec": round(speech_span, 1),
            "total_hold_sec": round(total_hold, 1),
            "hold_percentage": round(total_hold / audio_duration * 100, 1)
                               if audio_duration else 0.0,
            "hold_event_count": len(hold_events),
            "longest_hold_sec": round(longest_hold, 1),
            "hold_events": hold_events,
            "dead_air_event_count": len(dead_air_events),
            "total_dead_air_sec": round(total_dead, 1),
            "dead_air_events": dead_air_events,
            "hold_announcements": hold_announcements,
            "hold_returns": hold_returns,
            "discrepancies": discrepancies,
            "no_checkin_flags": no_checkin,
            "credential_windows": cred_windows,
            "credential_event_count": len(cred_windows),
            "repeated_segments": repeats,
            "low_confidence_count": len(low_conf),
            "pii_redaction_active": bool(self.redact_pii),
            "pii_types_found": sorted(pii_types),
            "word_timestamps_available": word_ts_available,
            "model_used": self.model_size,
            "device_used": device,
            "speaker_labels": "heuristic" if self.label_speakers else "none",
        }
        return self._cached_result

    # -------------------- signals block --------------------
    def _signals_block(self, r: dict) -> str:
        out = []

        if r["hold_events"]:
            for h in r["hold_events"]:
                line = (
                    f"[!] HOLD: {self._ts(h['start'])} -> {self._ts(h['end'])}, "
                    f"duration {self._dur(h['duration'])} "
                    f"({round(h['duration'] / r['audio_duration_sec'] * 100, 1)}% of call)"
                )
                if h.get("note"):
                    line += f"  << {h['note']}"
                out.append(line)
        else:
            out.append("[ok] No holds detected at or above threshold.")

        for a in r["hold_announcements"]:
            promised = (f", promised ~{self._dur(a['promised_sec'])}"
                        if a["promised_sec"] else "")
            out.append(
                f"    Hold announced at {self._ts(a['time'])}: "
                f"\"{a['phrase']}\"{promised}"
            )
        for h in r["hold_returns"]:
            out.append(
                f"    Return-from-hold at {self._ts(h['time'])}: \"{h['phrase']}\""
            )

        for d in r["discrepancies"]:
            out.append(f"[!] DISCREPANCY: {d}")
        for c in r["no_checkin_flags"]:
            out.append(f"[!] {c}")

        if r["dead_air_event_count"]:
            out.append(
                f"[!] Dead air: {r['dead_air_event_count']} pause(s) totalling "
                f"{self._dur(r['total_dead_air_sec'])}"
            )

        if r["credential_windows"]:
            for c in r["credential_windows"]:
                out.append(
                    f"[!] CREDENTIAL HANDLING: agent discussed login details "
                    f"{self._ts(c['start'])} -> {self._ts(c['end'])} "
                    f"({self._dur(c['end'] - c['start'])}). Content redacted. "
                    f"Assess against credential-delivery policy."
                )
        else:
            out.append("[ok] No credential disclosure detected.")

        if r["repeated_segments"]:
            out.append(
                f"[!] {len(r['repeated_segments'])} repeated phrase(s) - "
                f"possible hold-music bleed or looping audio"
            )
        if r["low_confidence_count"]:
            out.append(
                f"[!] {r['low_confidence_count']} low-confidence passage(s) - "
                f"quote with caution"
            )
        if r["language_probability"] < 0.8:
            out.append(
                f"[!] Language confidence low ({r['language_probability']}) - "
                f"transcript may be degraded"
            )
        if not r["word_timestamps_available"]:
            out.append(
                "[!] Word timestamps unavailable - hold figures are inferred "
                "and less reliable"
            )
        if r["speaker_labels"] == "heuristic":
            out.append(
                "[i] Speaker labels are rule-based guesses, not diarization. "
                "Do not treat attribution as certain."
            )
        return "\n".join(out)

    # -------------------- outputs --------------------
    def build_mission_payload(self) -> Message:
        try:
            r = self._transcribe()
        except Exception as e:
            return Message(text=f"TRANSCRIPTION_ERROR: {e}")

        transcript = r["clean_transcript"]
        limit = int(self.max_transcript_chars or 0)
        note = ""
        if limit and len(transcript) > limit:
            transcript = transcript[:limit]
            note = (
                f"\n\n[NOTE: truncated at {limit} chars. Full call was "
                f"{self._dur(r['audio_duration_sec'])} / {r['turn_count']} turns.]"
            )

        payload = f"""{self.audit_instructions}

=== CALL METADATA ===
Duration: {self._dur(r['audio_duration_sec'])} ({r['audio_duration_sec']}s)
Speaking turns: {r['turn_count']}
Word count: {r['word_count']}
Hold time: {self._dur(r['total_hold_sec'])} ({r['hold_percentage']}% of call)
Hold events: {r['hold_event_count']} (longest {self._dur(r['longest_hold_sec'])})
Transcription: {r['model_used']} on {r['device_used']}
PII redaction: {"ACTIVE - " + ", ".join(r['pii_types_found']) if r['pii_types_found'] else ("ACTIVE - nothing matched" if r['pii_redaction_active'] else "DISABLED")}

=== AUTOMATED SIGNALS (measured - treat as evidence, do not re-estimate) ===
{self._signals_block(r)}

=== TRANSCRIPT (timestamps mm:ss) ===
{transcript}{note}

=== END OF CALL ===

Produce your audit now. Cite a timestamp for every score justification.
Base all hold and silence judgements on the AUTOMATED SIGNALS block."""

        return Message(text=payload)

    def build_structured(self) -> Data:
        try:
            return Data(data=self._transcribe())
        except Exception as e:
            return Data(data={"error": str(e)})

    def build_report(self) -> Data:
        try:
            r = self._transcribe()
        except Exception as e:
            return Data(value=f"Component Error: {e}")

        report = f"""CALL QA DATASET
========================
TRANSCRIPTION INFO
========================
Model: {r['model_used']} on {r['device_used']}
Language: {r['language']} (confidence {r['language_probability']})
Segments: {r['segment_count']} | Turns: {r['turn_count']} | Words: {r['word_count']}
Word timestamps: {"yes" if r['word_timestamps_available'] else "NO - holds inferred"}
Speaker labels: {r['speaker_labels']}
PII redaction: {"ON" if r['pii_redaction_active'] else "OFF"} {r['pii_types_found']}

========================
CALL METRICS
========================
Total Audio Duration: {r['audio_duration_sec']}s ({self._dur(r['audio_duration_sec'])})
Total Hold Time: {r['total_hold_sec']}s ({r['hold_percentage']}%)
Hold Events: {r['hold_event_count']}
Longest Hold: {r['longest_hold_sec']}s
Dead Air Events: {r['dead_air_event_count']} ({r['total_dead_air_sec']}s)
Credential Exposure Windows: {r['credential_event_count']}

========================
AUTOMATED SIGNALS
========================
{self._signals_block(r)}

========================
TIMESTAMPED TRANSCRIPT
========================
{r['clean_transcript']}
"""
        return Data(value=report)
