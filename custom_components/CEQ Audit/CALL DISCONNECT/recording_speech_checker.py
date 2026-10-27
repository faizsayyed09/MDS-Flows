import os

import re

import sys

import time

import shutil

import tempfile

import threading

import importlib

import subprocess

from collections import Counter

from datetime import datetime

import requests

from lfx.custom.custom_component.component import Component

from lfx.io import DataInput, DropdownInput, IntInput, StrInput, Output

from lfx.schema.data import Data

SR = 16000

_LOCK = threading.Lock()

_FW = {"mod": None}

_MODELS = {}

ENGAGE = re.compile(r"\b(hello|hi|hey|anyone there|anybody there|are you there|can you hear)\b", re.I)

TICKET = re.compile(r"\b(INC|RITM|REQ|SCTASK|TASK)\s?-?\s?(\d{5,})\b", re.I)

HALLUCINATION = re.compile(r"(thanks? (?:you )?for watching|subscribe|subtitles? by|captions? by|amara\.org)", re.I)

ADVERSE = {"SILENT_ANSWER", "DELAYED_GREETING", "CALLER_UNANSWERED", "AGENT_RELEASED_SILENT_CALL",

                                         "CALLER_ABANDONED_SILENT_CALL", "LONG_AGENT_SILENCE", "SNOW_ACTIVITY_DURING_CALL",

                                         "GENESYS_EMAIL_DURING_CALL", "BOTH_SILENT"}

WEIGHTS = {"SILENT_ANSWER": 35, "CALLER_UNANSWERED": 20, "AGENT_RELEASED_SILENT_CALL": 20,

                                         "CALLER_ABANDONED_SILENT_CALL": 20, "DELAYED_GREETING": 10, "LONG_AGENT_SILENCE": 15,

                                         "SNOW_ACTIVITY_DURING_CALL": 25, "GENESYS_EMAIL_DURING_CALL": 25, "BOTH_SILENT": 10}

TIMEOUT_DISCONNECTS = ("conversation inactivity", "session expired", "system")

def _unwrap(value):

    if isinstance(value, list) and value:

        value = value[0]

    if hasattr(value, "data"):

        value = value.data or {}

    return value if isinstance(value, dict) else {}

def _dt(value):

    try:

        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    except (TypeError, ValueError):

        return None

def _int(value, default):

    try:

        return int(float(value))

    except (TypeError, ValueError):

        return default

def _num(value, default=0.0):

    try:

        return float(value)

    except (TypeError, ValueError):

        return default

def _persist_dir(name):

    base = os.environ.get("LANGFLOW_CONFIG_DIR") or ""

    root = tempfile.gettempdir()

    if base and os.path.isdir(base) and os.access(base, os.W_OK):

        root = base

    path = os.path.join(root, name)

    os.makedirs(path, exist_ok=True)

    return path

def _load_fw():

    if _FW["mod"] is not None:

        return _FW["mod"]

    with _LOCK:

        if _FW["mod"] is not None:

            return _FW["mod"]

        target = _persist_dir("_pylibs")

        if target not in sys.path:

            sys.path.append(target)

        importlib.invalidate_caches()

        try:

            import faster_whisper

        except ImportError:

            subprocess.run([sys.executable, "-m", "pip", "install", "--target", target,

                                                                     "--disable-pip-version-check", "faster-whisper"],

                                                                 capture_output=True, text=True, timeout=900)

            importlib.invalidate_caches()

            import faster_whisper

        try:

            import av

            if not getattr(av.open, "_ceq_patched", False):

                original = av.open

                def _open(*a, **k):

                    try:

                        return original(*a, **k)

                    except TypeError as e:

                        if "metadata_errors" in k and "metadata_errors" in str(e):

                            k.pop("metadata_errors", None)

                            return original(*a, **k)

                        raise

                _open._ceq_patched = True

                av.open = _open

        except Exception:

            pass

        _FW["mod"] = faster_whisper

        return faster_whisper

def _model(size):

    fw = _load_fw()

    with _LOCK:

        if size not in _MODELS:

            _MODELS[size] = fw.WhisperModel(size, device="auto", compute_type="int8", cpu_threads=4,

                                                                                                                                         download_root=_persist_dir("_whisper_models"))

        return _MODELS[size]

def _decode(paths):

    _load_fw()

    import numpy as np

    from faster_whisper.audio import decode_audio

    if len(paths) >= 2:

        a = decode_audio(paths[0], sampling_rate=SR)

        b = decode_audio(paths[1], sampling_rate=SR)

        return [a, b], "two-track"

    left, right = decode_audio(paths[0], sampling_rate=SR, split_stereo=True)

    n = min(len(left), len(right))

    if n and not np.allclose(left[:n], right[:n], atol=1e-4):

        return [left, right], "stereo-split"

    return [left], "mono"

def _spans(audio, threshold):

    try:

        from faster_whisper.vad import VadOptions, get_speech_timestamps

        opts = VadOptions(threshold=threshold, min_speech_duration_ms=250,

                                                                             min_silence_duration_ms=300, speech_pad_ms=100)

        out = []

        for t in get_speech_timestamps(audio, opts):

            out.append((t["start"] / SR, t["end"] / SR))

        return out, "silero-vad"

    except Exception:

        import numpy as np

        f = int(0.03 * SR)

        n = len(audio) // f

        if n == 0:

            return [], "energy"

        rms = np.sqrt((audio[: n * f].reshape(n, f) ** 2).mean(axis=1))

        thr = max(0.01, float(np.percentile(rms, 20)) * 4)

        out = []

        start = None

        flags = list(rms > thr) + [False]

        for i, on in enumerate(flags):

            if on and start is None:

                start = i

            elif not on and start is not None:

                if i - start >= 8:

                    out.append((start * 0.03, i * 0.03))

                start = None

        return out, "energy"

def _clip(spans, a, b):

    out = []

    for s, e in spans:

        if e > a and s < b:

            out.append((max(s, a) - a, min(e, b) - a))

    return sorted(out)

def _overlap(a0, a1, ranges):

    total = 0.0

    for r0, r1 in ranges:

        gap = min(a1, r1) - max(a0, r0)

        if gap > 0:

            total += gap

    return total

def _longest_silence(spans, length, holds):

    points = [0.0]

    for s, e in spans:

        points.append(s)

        points.append(e)

    points.append(length)

    best = 0.0

    for i in range(0, len(points) - 1, 2):

        g0 = points[i]

        g1 = points[i + 1]

        if g1 > g0:

            best = max(best, (g1 - g0) - _overlap(g0, g1, holds))

    return round(best, 1)

def _redact(text):

    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]", text)

    def keep(m):

        if TICKET.fullmatch(m.group(0)):

            return m.group(0)

        return "[NUM]"

    return re.sub(r"\b\w*\d{4,}\w*\b", keep, text)

def _transcribe(model, audio, a, b):

    clip = audio[int(a * SR): int(b * SR)]

    if len(clip) < SR // 2:

        return []

    segs, _ = model.transcribe(clip, language="en", beam_size=1, vad_filter=True,

                                                                                                                 condition_on_previous_text=False, temperature=0.0)

    out = []

    for s in segs:

        text = (s.text or "").strip()

        if not text or HALLUCINATION.search(text):

            continue

        if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:

            continue

        if s.compression_ratio > 2.6:

            continue

        out.append((round(s.start, 1), round(s.end, 1), text))

    return out

class CustomComponent(Component):

    display_name = "Recording Speech Checker"

    description = "Downloads each candidate recording, measures agent and caller speech, transcribes, and emits work-avoidance packets."

    inputs = [

        DataInput(name="auth", display_name="Auth Token", required=True),

        DataInput(name="detections", display_name="Detections", required=True),

        DropdownInput(name="agent_track", display_name="Agent Track",

            options=["track 0 is agent", "track 1 is agent"], value="track 1 is agent"),

        DropdownInput(name="transcribe_mode", display_name="Whisper Transcription",

            options=["concerns only", "all checked calls", "never"], value="all checked calls"),

        DropdownInput(name="packet_scope", display_name="Send To AI",

            options=["flagged only", "all checked calls"], value="all checked calls"),

        DropdownInput(name="whisper_model", display_name="Whisper Model",

            options=["small.en", "distil-small.en", "base.en", "medium.en", "large-v3-turbo"],

            value="large-v3-turbo"),

        IntInput(name="transcribe_window_sec", display_name="Transcribe First N Seconds After Answer", value=120),

        IntInput(name="max_calls", display_name="Max Calls to Check per Run", value=2000),

        IntInput(name="min_talk_sec", display_name="Min Connected Seconds for Silence Check", value=15),

        StrInput(name="min_agent_speech_sec", display_name="Silent if Agent Speech Below (s)", value="1.5"),

        IntInput(name="min_agent_words", display_name="Silent if Agent Words Below", value=3),

        IntInput(name="greeting_sec", display_name="Max Seconds to First Agent Speech", value=15),

        IntInput(name="dead_air_sec", display_name="Long Agent Silence (s, excl. hold)", value=60),

        StrInput(name="vad_threshold", display_name="VAD Threshold", value="0.5", advanced=True),

        StrInput(name="agent_disconnect_types", display_name="Agent-side Disconnect Types", value="Agent",

            advanced=True,

            info="Derived values: Agent, External, Conversation Inactivity, Session Expired, System."),

        IntInput(name="poll_attempts", display_name="Recording Poll Attempts", value=3, advanced=True),

    ]

    outputs = [Output(display_name="Work Avoidance Packets", name="packets", method="run")]

    def _recordings(self, api, headers, cid):

        for _ in range(max(1, _int(self.poll_attempts, 3))):

            r = requests.get(api + "/api/v2/conversations/" + cid + "/recordings", headers=headers,

                                                                             params={"maxWaitMs": 5000}, timeout=60)

            if r.status_code == 429:

                time.sleep(_int(r.headers.get("Retry-After"), 5))

                continue

            if r.status_code == 404:

                return None, "NO_RECORDING"

            if r.status_code == 202 or not (r.text or "").strip():

                time.sleep(5)

                continue

            if r.status_code >= 400:

                return None, "HTTP_" + str(r.status_code)

            data = r.json()

            if isinstance(data, dict):

                data = data.get("entities") or [data]

            audio = []

            for x in data:

                if x.get("media") == "audio":

                    audio.append(x)

            if not audio:

                return None, "NO_AUDIO_RECORDING"

            ready = []

            for x in audio:

                state = str(x.get("fileState") or "AVAILABLE").upper()

                if x.get("mediaUris") and state == "AVAILABLE":

                    ready.append(x)

            if ready:

                return ready, "OK"

            states = set()

            for x in audio:

                states.add(str(x.get("fileState") or "").upper())

            if "ARCHIVED" in states:

                return None, "ARCHIVED"

            if "DELETED" in states:

                return None, "DELETED"

            time.sleep(5)

        return None, "NOT_READY"

    def _pick(self, recs, call):

        start = _dt(call.get("answered_utc"))

        end = _dt(call.get("ended_utc"))

        best = None

        best_score = 0.0

        for r in recs:

            rs = _dt(r.get("startTime"))

            re_ = _dt(r.get("endTime"))

            if not rs or not re_ or not start or not end:

                continue

            ov = (min(end, re_) - max(start, rs)).total_seconds()

            if ov <= 0:

                continue

            score = ov

            for u in r.get("users") or []:

                if u.get("id") == call.get("user_id"):

                    score += 100000

            if score > best_score:

                best = r

                best_score = score

        return best

    def _speech(self, api, headers, call):

        recs, state = self._recordings(api, headers, call.get("conversation_id"))

        if not recs:

            return {"transcript_status": state}, None

        rec = self._pick(recs, call)

        if not rec:

            return {"transcript_status": "NO_MATCHING_RECORDING"}, None

        uris = []

        for k, v in (rec.get("mediaUris") or {}).items():

            uri = (v or {}).get("mediaUri")

            if uri:

                uris.append((k, uri))

        uris = sorted(uris)[:2]

        tmp = tempfile.mkdtemp(prefix="wa_")

        try:

            paths = []

            for key, uri in uris:

                path = os.path.join(tmp, "track" + str(key))

                with requests.get(uri, stream=True, timeout=180) as r:

                    r.raise_for_status()

                    with open(path, "wb") as f:

                        for chunk in r.iter_content(1048576):

                            f.write(chunk)

                paths.append(path)

            channels, layout = _decode(paths)

        finally:

            shutil.rmtree(tmp, ignore_errors=True)

        rec_start = _dt(rec.get("startTime"))

        a = 0.0

        b = 0.0

        if rec_start:

            a = max(0.0, (_dt(call.get("answered_utc")) - rec_start).total_seconds())

            b = max(a, (_dt(call.get("ended_utc")) - rec_start).total_seconds())

        limit = len(channels[0]) / SR

        if limit:

            b = min(b, limit)

        thr = _num(self.vad_threshold, 0.5)

        res = {"audio_layout": layout, "recording_id": rec.get("id"),

                                 "answer_offset_in_recording_s": round(a, 1)}

        if len(channels) == 1:

            sp, engine = _spans(channels[0], thr)

            total = 0.0

            for s, e in _clip(sp, a, b):

                total += e - s

            res["transcript_status"] = "MONO_UNATTRIBUTED"

            res["vad_engine"] = engine

            res["any_speech_seconds"] = round(total, 1)

            return res, {"agent": channels[0], "caller": None, "a": a, "b": b, "agent_spans": None}

        ai = 0

        if str(self.agent_track or "").startswith("track 1"):

            ai = 1

        asp, engine = _spans(channels[ai], thr)

        csp, _ = _spans(channels[1 - ai], thr)

        asp = _clip(asp, a, b)

        csp = _clip(csp, a, b)

        first = None

        if asp:

            first = asp[0][0]

        agent_s = 0.0

        for s, e in asp:

            agent_s += e - s

        cust_s = 0.0

        bursts = 0

        for s, e in csp:

            cust_s += e - s

            if (first is None or s < first) and (e - s) >= 0.3:

                bursts += 1

        res["transcript_status"] = "VAD_ONLY"

        res["vad_engine"] = engine

        res["agent_track"] = str(ai)

        res["agent_speech_seconds"] = round(agent_s, 1)

        res["caller_speech_seconds"] = round(cust_s, 1)

        res["first_agent_speech_seconds"] = None if first is None else round(first, 1)

        res["caller_bursts_before_agent"] = bursts

        res["longest_agent_silence_excl_hold_seconds"] = _longest_silence(asp, b - a, call.get("holds_offsets") or [])

        return res, {"agent": channels[ai], "caller": channels[1 - ai], "a": a, "b": b, "agent_spans": asp}

    def _words(self, res, ctx):

        model = _model(self.whisper_model)

        end = min(ctx["b"], ctx["a"] + max(30, _int(self.transcribe_window_sec, 120)))

        agent_w = _transcribe(model, ctx["agent"], ctx["a"], end)

        caller_w = []

        if ctx["caller"] is not None:

            caller_w = _transcribe(model, ctx["caller"], ctx["a"], end)

        who = "AGENT" if ctx["caller"] is not None else "SPEAKER"

        lines = []

        for s, _e, t in agent_w:

            lines.append((s, who, t))

        for s, _e, t in caller_w:

            lines.append((s, "CALLER", t))

        lines.sort()

        first = None

        if agent_w and ctx["caller"] is not None:

            first = agent_w[0][0]

        engage = 0

        for s, _e, t in caller_w:

            if ENGAGE.search(t) and (first is None or s < first):

                engage += 1

        tickets = set()

        for _s, _e, t in agent_w + caller_w:

            for m in TICKET.finditer(t):

                tickets.add((m.group(1) + m.group(2)).upper())

        excerpt = []

        for s, w, t in lines:

            stamp = "[+%02d:%02d] " % (int(s) // 60, int(s) % 60)

            excerpt.append(stamp + w + ": " + _redact(t))

        agent_words = 0

        for _s, _e, t in agent_w:

            agent_words += len(t.split())

        caller_words = 0

        for _s, _e, t in caller_w:

            caller_words += len(t.split())

        def cues(words):

            n = 0

            for _s, _e, t in words:

                low = t.lower()

                for c in ("thank you for calling", "service desk", "my name is", "how can i help",

                                                     "how may i help", "may i have your", "let me check", "one moment"):

                    if c in low:

                        n += 1

            return n

        res["transcribed_window_seconds"] = round(end - ctx["a"], 1)

        res["agent_words"] = agent_words

        res["caller_words"] = caller_words

        res["first_agent_word_seconds"] = first

        res["caller_engagement_attempts_before_agent_spoke"] = engage if ctx["caller"] is not None else None

        res["tickets_mentioned"] = sorted(tickets)

        res["transcript_excerpt"] = "\n".join(excerpt)[:1500]

        res["mapping_warning"] = bool(ctx["caller"] is not None and cues(caller_w) > cues(agent_w) + 1)

        if res.get("transcript_status") == "VAD_ONLY":

            res["transcript_status"] = "COMPLETE"

    def _concern(self, call, res):

        if res.get("transcript_status") == "MONO_UNATTRIBUTED":

            return True

        first = res.get("first_agent_speech_seconds")

        if res.get("agent_speech_seconds", 0) < _num(self.min_agent_speech_sec, 1.5) * 3:

            return True

        if first is None or first > _int(self.greeting_sec, 15):

            return True

        for r in call.get("audit_reasons") or []:

            if r in ("SNOW_ACTIVITY_DURING_CALL", "GENESYS_EMAIL_DURING_CALL"):

                return True

        return False

    def _finalise(self, c, agent_dc):

        sp = c.get("speech") or {}

        st = sp.get("transcript_status")

        spoken = set(sp.get("tickets_mentioned") or [])

        for act in c.get("snow_activity_during_call") or []:

            if act.get("ticket") in spoken and not act.get("related_to_call"):

                act["related_to_call"] = True

                act["related_reason"] = "ticket number spoken in call"

        unrelated = []

        for x in c.get("snow_activity_during_call") or []:

            if not x.get("related_to_call") and not x.get("within_grace"):

                unrelated.append(x)

        flags = []

        disc = str(c.get("disconnect_type") or "").lower()

        if st in ("VAD_ONLY", "COMPLETE"):

            noise_only = False

            if st == "COMPLETE":

                if sp.get("agent_words", 0) < _int(self.min_agent_words, 3) and sp.get("agent_speech_seconds", 0) < 10:

                    noise_only = True

            silent = False

            if c.get("connected_seconds", 0) >= _int(self.min_talk_sec, 15):

                if sp.get("agent_speech_seconds", 0) < _num(self.min_agent_speech_sec, 1.5) or noise_only:

                    silent = True

            if silent and sp.get("caller_speech_seconds", 0) < 1:

                flags.append("BOTH_SILENT")

            elif silent:

                flags.append("SILENT_ANSWER")

            first = sp.get("first_agent_speech_seconds")

            if not silent and first is not None and first > _int(self.greeting_sec, 15):

                flags.append("DELAYED_GREETING")

            attempts = sp.get("caller_engagement_attempts_before_agent_spoke")

            if attempts is not None and attempts >= 2:

                flags.append("CALLER_UNANSWERED")

            elif attempts is None and sp.get("caller_bursts_before_agent", 0) >= 3:

                if "SILENT_ANSWER" in flags or "DELAYED_GREETING" in flags:

                    flags.append("CALLER_UNANSWERED")

            if "SILENT_ANSWER" in flags:

                if disc in agent_dc:

                    flags.append("AGENT_RELEASED_SILENT_CALL")

                elif disc == "external":

                    flags.append("CALLER_ABANDONED_SILENT_CALL")

            if not silent and sp.get("longest_agent_silence_excl_hold_seconds", 0) >= _int(self.dead_air_sec, 60):

                flags.append("LONG_AGENT_SILENCE")

        elif st == "MONO_UNATTRIBUTED":

            flags.append("MONO_AUDIO")

            if sp.get("any_speech_seconds", 0) < 1 and c.get("connected_seconds", 0) >= _int(self.min_talk_sec, 15):

                flags.append("BOTH_SILENT")

        elif c.get("audio_check"):

            flags.append("NO_AUDIO_EVIDENCE")

        if unrelated:

            flags.append("SNOW_ACTIVITY_DURING_CALL")

        for o in c.get("genesys_email_overlap") or []:

            if o.get("overlap_seconds", 0) > c.get("grace_seconds", 60):

                flags.append("GENESYS_EMAIL_DURING_CALL")

                break

        if disc in TIMEOUT_DISCONNECTS and ("SILENT_ANSWER" in flags or "BOTH_SILENT" in flags):

            flags.append("TIMEOUT_DISCONNECT")

        score = 0

        for f in flags:

            score += WEIGHTS.get(f, 0)

        c["flags"] = flags

        c["pre_score"] = min(100, score)

    def run(self) -> Data:

        d = _unwrap(self.detections)

        if d.get("error"):

            return Data(data=d)

        auth = _unwrap(self.auth)

        if not auth.get("access_token"):

            return Data(data={"error": "AUTH", "detail": str(auth.get("error") or "no token")})

        api = "https://api." + auth.get("region", "mypurecloud.com")

        headers = {"Authorization": "Bearer " + auth["access_token"], "Accept": "application/json"}

        agent_dc = set()

        for x in (self.agent_disconnect_types or "").split(","):

            if x.strip():

                agent_dc.add(x.strip().lower())

        calls = d.get("calls") or []

        todo = []

        for c in calls:

            if c.get("audio_check"):

                todo.append(c)

        def rank(c):

            reasons = c.get("audit_reasons") or []

            if len(reasons) == 1 and reasons[0] == "SHORT_CALL":

                return 1

            return 0

        todo.sort(key=rank)

        cap = max(0, _int(self.max_calls, 2000))

        skipped = max(0, len(todo) - cap)

        todo = todo[:cap]

        checked = set()

        for c in todo:

            checked.add(str(c.get("conversation_id")) + str(c.get("user_id")))

            try:

                res, ctx = self._speech(api, headers, c)

            except Exception as e:

                res = {"transcript_status": "ERROR", "error": str(e)[:200]}

                ctx = None

            mode = str(self.transcribe_mode or "")

            if ctx and mode != "never":

                if mode.startswith("all") or self._concern(c, res):

                    try:

                        self._words(res, ctx)

                    except Exception as e:

                        res["whisper_error"] = str(e)[:200]

            if ctx and ctx.get("agent_spans") is not None:

                for act in c.get("snow_activity_during_call") or []:

                    off = _num(act.get("offset_s"))

                    act["agent_speech_s_within_30s"] = round(_overlap(off - 30, off + 30, ctx["agent_spans"]), 1)

            c["speech"] = res

        for c in calls:

            if "speech" not in c:

                key = str(c.get("conversation_id")) + str(c.get("user_id"))

                if key in checked:

                    c["speech"] = {"transcript_status": "ERROR"}

                else:

                    c["speech"] = {"transcript_status": "NOT_CHECKED"}

            self._finalise(c, agent_dc)

        pattern = {}

        for c in calls:

            p = pattern.setdefault(c.get("agent_email", ""), Counter())

            p["answered_calls"] += 1

            flags = set(c.get("flags") or [])

            if flags & ADVERSE:

                p["flagged_calls"] += 1

            if "SILENT_ANSWER" in flags:

                p["silent_calls"] += 1

            if "SNOW_ACTIVITY_DURING_CALL" in flags:

                p["calls_with_unrelated_snow_activity"] += 1

        all_scope = str(self.packet_scope or "").startswith("all")

        packets = []

        for c in calls:

            flags = set(c.get("flags") or [])

            if not all_scope and not (flags & ADVERSE):

                continue

            if all_scope and c["speech"].get("transcript_status") == "NOT_CHECKED":

                continue

            p = {}

            for k, v in c.items():

                if k not in ("audio_check", "user_id"):

                    p[k] = v

            p["agent_pattern"] = dict(pattern.get(c.get("agent_email", ""), {}))

            packets.append(p)

        summary = []

        for c in calls:

            summary.append({"agent_name": c.get("agent_name", ""), "agent_email": c.get("agent_email", ""),

                                                                         "manager": c.get("manager", ""), "flags": c.get("flags") or []})

        warn = 0

        for c in calls:

            if c["speech"].get("mapping_warning"):

                warn += 1

        status = str(len(todo)) + " calls audio-checked, " + str(len(packets)) + " sent to AI"

        if warn:

            status = status + " | " + str(warn) + " SPEAKER MAPPING WARNINGS - recalibrate Agent Track"

        self.status = status

        return Data(data={

            "packets": packets,

            "calls": summary,

            "interval": d.get("interval", ""),

            "snow_file": d.get("snow_file", ""),

            "duration_band": d.get("duration_band", ""),

            "audio_checked": len(todo),

            "audio_check_skipped": skipped,

            "mapping_warnings": warn,

            "agent_track": self.agent_track,

            "whisper_model": self.whisper_model,

        })
