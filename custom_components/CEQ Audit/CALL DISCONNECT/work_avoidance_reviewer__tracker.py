import copy

import io

import json

import re

import threading

import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from datetime import datetime

from zoneinfo import ZoneInfo

import pandas as pd

import requests

from lfx.custom.custom_component.component import Component

from lfx.io import BoolInput, DataInput, IntInput, SecretStrInput, StrInput, Output

from lfx.schema.data import Data

from lfx.schema.message import Message

ET = ZoneInfo("America/New_York")

ET_FMT = "%m/%d/%Y %H:%M:%S"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ADVERSE = {"SILENT_ANSWER", "DELAYED_GREETING", "CALLER_UNANSWERED", "AGENT_RELEASED_SILENT_CALL",

                                         "CALLER_ABANDONED_SILENT_CALL", "LONG_AGENT_SILENCE", "SNOW_ACTIVITY_DURING_CALL",

                                         "GENESYS_EMAIL_DURING_CALL", "BOTH_SILENT"}

VERDICTS = ["LIKELY_WORK_AVOIDANCE", "POSSIBLE_WORK_AVOIDANCE", "NOT_WORK_AVOIDANCE",

                                             "TECHNICAL_OR_DATA_ISSUE", "INSUFFICIENT_DATA"]

ALL_VERDICTS = VERDICTS + ["NOT_REVIEWED", "REVIEW_FAILED"]

ACTIONS = {"NO_ACTION", "COACHING", "TEAM_LEAD_REVIEW", "ESCALATE_TO_MANAGER", "CHECK_TELEPHONY"}

AUDIO_OK = {"COMPLETE", "VAD_ONLY"}

VERDICT_FILL = {"LIKELY_WORK_AVOIDANCE": "FFC7CE", "POSSIBLE_WORK_AVOIDANCE": "FFEB9C",

                                                             "NOT_WORK_AVOIDANCE": "C6EFCE", "TECHNICAL_OR_DATA_ISSUE": "DDEBF7",

                                                             "INSUFFICIENT_DATA": "EDEDED", "NOT_REVIEWED": "EDEDED", "REVIEW_FAILED": "F4B084"}

WRAP_COLS = {"AI Review", "Behaviours Observed", "Legitimate Explanations", "Coaching Points", "QA Check",

                                                 "Review Error", "Unrelated SNOW Detail", "Rule Flags", "Event", "Reviewer Comments",

                                                 "Value", "Transcript (opening)", "Email vs Call", "Audit Reasons"}

ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

TRACKER_COLUMNS = [

    "Review Date", "Manager", "Agent", "Agent Email", "Conversation ID", "Direction", "Queue ID",

    "Answered (ET)", "Ended (ET)", "Connected (s)", "Hold (s)", "Disconnect Type", "Disconnect (raw segment)",

    "Audited Agent Released", "Wrap-up Code", "Audit Reasons", "Audio Evidence", "Audio Layout",

    "Agent Speech (s)", "Caller Speech (s)", "First Agent Speech (s)",

    "Longest Agent Silence excl. Hold (s)", "Caller Speech Bursts Before Agent", "Agent Words (opening)",

    "Caller Words (opening)", "Caller Hello Attempts", "Speaker Mapping Warning", "Call Left Unspoken",

    "SNOW Actions During Call", "Unrelated SNOW Actions", "Unrelated SNOW Detail", "SNOW Tickets Touched",

    "Genesys Email Overlap (s)", "Email vs Call", "Email Actioned During Call", "Call's Own Ticket",

    "Rule Flags", "Pre-Score", "AI Verdict", "AI Confidence", "AI Risk Score", "AI Review",

    "Behaviours Observed", "Legitimate Explanations", "Recommended Action", "Coaching Points",

    "AI Call Left Unspoken", "AI Email Actioned", "QA Check", "Review Error", "Transcript (opening)",

    "Reviewer", "Reviewer Decision", "Reviewer Comments", "Status",

]

TIMELINE_COLUMNS = ["Conversation ID", "Manager", "Agent", "Time (ET)", "Source", "Event", "Origin"]

SUMMARY_FLAGS = [("Silent Answers", "SILENT_ANSWER"), ("Both Silent", "BOTH_SILENT"),

                                                                     ("Delayed Greeting", "DELAYED_GREETING"), ("Long Agent Silence", "LONG_AGENT_SILENCE"),

                                                                     ("Caller Abandoned Silent Call", "CALLER_ABANDONED_SILENT_CALL"),

                                                                     ("Unrelated SNOW During Call", "SNOW_ACTIVITY_DURING_CALL"),

                                                                     ("Genesys Email During Call", "GENESYS_EMAIL_DURING_CALL")]

class _Stop(Exception):

    pass

def _unwrap(value):

    if isinstance(value, list) and value:

        value = value[0]

    if hasattr(value, "data"):

        value = value.data or {}

    return value if isinstance(value, dict) else {}

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

def _risk(value):

    try:

        return max(0, min(100, int(round(float(str(value).strip().rstrip("%"))))))

    except (TypeError, ValueError):

        return ""

def _choice(value, allowed, default):

    v = re.sub(r"[\s\-]+", "_", str(value or "").strip().upper())

    if v in allowed:

        return v

    return default

def _list(value):

    if value is None or value == "":

        return []

    if isinstance(value, (list, tuple)):

        out = []

        for x in value:

            if str(x).strip():

                out.append(str(x).strip())

        return out

    return [str(value).strip()]

def _cell(value):

    if value is None:

        return ""

    if isinstance(value, float) and value != value:

        return ""

    if isinstance(value, (list, tuple, set)):

        parts = []

        for x in value:

            if x not in (None, ""):

                parts.append(str(x))

        value = "; ".join(parts)

    elif isinstance(value, dict):

        value = json.dumps(value, ensure_ascii=False)

    if isinstance(value, str):

        value = ILLEGAL_XML.sub("", value)[:32000]

        if value.startswith("="):

            value = "'" + value

    return value

def _parse_et(text):

    try:

        return datetime.strptime(str(text).strip(), ET_FMT)

    except (TypeError, ValueError):

        return None

def _response_text(out):

    if isinstance(out, str):

        return out

    if not isinstance(out, dict):

        return ""

    parts = []

    for item in out.get("output") or []:

        if not isinstance(item, dict):

            continue

        if str(item.get("type", "")).lower() == "reasoning":

            continue

        for c in item.get("content") or []:

            if isinstance(c, dict) and c.get("text"):

                parts.append(str(c["text"]))

        if isinstance(item.get("text"), str):

            parts.append(item["text"])

    if not parts:

        for key in ("text", "response", "answer", "content"):

            if isinstance(out.get(key), str):

                parts.append(out[key])

                break

    return "\n".join(parts)

def _extract_json(text):

    if isinstance(text, dict):

        return text

    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S)

    text = re.sub(r"```(?:json)?", "", text)

    decoder = json.JSONDecoder()

    best = None

    first = None

    for m in re.finditer(r"\{", text):

        try:

            obj, _ = decoder.raw_decode(text[m.start():])

        except ValueError:

            continue

        if isinstance(obj, dict):

            if "verdict" in obj:

                best = obj

            elif first is None:

                first = obj

        return best or first or {} if False else (best or first or {}) if False else (best or first or {})

    return best or first or {}

def _unrelated(p):

    out = []

    for a in p.get("snow_activity_during_call") or []:

        if a.get("related_to_call") or a.get("within_grace"):

            continue

        if str(a.get("type", "")).upper() == "EMAIL_RECEIVED":

            continue

        out.append(a)

    return out

def _email_overlap(p):

    grace = _num(p.get("grace_seconds"), 60.0)

    out = []

    for o in p.get("genesys_email_overlap") or []:

        if _num(o.get("overlap_seconds")) > grace:

            out.append(o)

    return out

def _email_rule(p, snow_ok):

    if _unrelated(p) or _email_overlap(p):

        return "YES"

    if snow_ok:

        return "NO"

    return "UNKNOWN"

def _audio_reliable(p):

    sp = p.get("speech") or {}

    if sp.get("transcript_status") not in AUDIO_OK:

        return False

    if sp.get("mapping_warning"):

        return False

    return True

def _unspoken_rule(p):

    flags = set(p.get("flags") or [])

    if not _audio_reliable(p) or "BOTH_SILENT" in flags:

        return "UNKNOWN"

    if "SILENT_ANSWER" in flags:

        return "YES"

    if flags & {"DELAYED_GREETING", "LONG_AGENT_SILENCE"}:

        return "PARTIAL"

    return "NO"

def _email_vs_call(p):

    parts = []

    for o in p.get("genesys_email_overlap") or []:

        parts.append("Genesys email " + str(o.get("email_conversation_id", ""))[:8]

                                                         + " " + str(o.get("email_start_et", "")) + " to " + str(o.get("email_end_et", ""))

                                                         + " (overlap " + str(o.get("overlap_seconds", "")) + " s)")

    for a in _unrelated(p):

        parts.append(str(a.get("type", "")) + " at " + str(a.get("time_et", ""))

                                                         + " (+" + str(a.get("offset_s", "")) + " s) on " + str(a.get("ticket", ""))

                                                         + " - agent speech nearby " + str(a.get("agent_speech_s_within_30s", "?")) + " s")

    return "; ".join(parts)

def _qa_check(p, verdict, confidence, risk, ai_email, ai_unspoken, email_rule, unspoken_rule):

    notes = []

    flags = set(p.get("flags") or [])

    other_work = bool(_unrelated(p) or _email_overlap(p))

    if verdict == "LIKELY_WORK_AVOIDANCE" and not _audio_reliable(p) and not other_work:

        notes.append("LIKELY verdict without reliable audio or overlapping work")

    if verdict == "LIKELY_WORK_AVOIDANCE" and "BOTH_SILENT" in flags and not _unrelated(p):

        notes.append("LIKELY verdict on a both-silent call (check telephony)")

    if verdict == "LIKELY_WORK_AVOIDANCE" and "TIMEOUT_DISCONNECT" in flags:

        notes.append("LIKELY verdict on a timeout disconnect (" + str(p.get("disconnect_type")) + ")")

    if "AGENT_RELEASED_SILENT_CALL" in flags and not p.get("agent_released_own_session"):

        notes.append("conversation released by agent side, but not by this agent's own session")

    if confidence == "HIGH" and not _audio_reliable(p):

        notes.append("HIGH confidence with unreliable/missing audio")

    if risk != "":

        if verdict == "LIKELY_WORK_AVOIDANCE" and risk < 51:

            notes.append("risk score " + str(risk) + " low for LIKELY verdict")

        if verdict == "NOT_WORK_AVOIDANCE" and risk > 50:

            notes.append("risk score " + str(risk) + " high for NOT verdict")

    if ai_email and ai_email != email_rule:

        notes.append("AI email answer " + ai_email + " differs from data (" + email_rule + ")")

    if ai_unspoken and ai_unspoken != unspoken_rule:

        notes.append("AI unspoken answer " + ai_unspoken + " differs from data (" + unspoken_rule + ")")

    return "; ".join(notes) or "OK"

def _evidence(p):

    events = []

    answered = _parse_et(p.get("answered_et"))

    sp = p.get("speech") or {}

    def add(when, source, text):

        if isinstance(when, datetime):

            events.append((when, when.strftime(ET_FMT), source, text))

        else:

            events.append((answered or datetime.max, str(when or ""), source, text))

    add(answered or p.get("answered_et"), "GENESYS",

             "Agent answered " + str(p.get("direction") or "") + " call ("

             + str(p.get("connected_seconds", "")) + " s connected)")

    status = sp.get("transcript_status") or ""

    if answered and status in AUDIO_OK:

        bursts = _int(sp.get("caller_bursts_before_agent"), 0)

        if bursts:

            add(answered, "AUDIO", "Caller speech bursts before agent spoke: " + str(bursts))

        first = sp.get("first_agent_speech_seconds")

        if first is None:

            add(answered, "AUDIO", "No agent speech detected on the agent channel")

        else:

            from datetime import timedelta

            add(answered + timedelta(seconds=_num(first)), "AUDIO",

                         "First agent speech (+" + str(first) + " s after answer)")

        if sp.get("mapping_warning"):

            add(answered, "AUDIO", "Speaker mapping warning: channels may be swapped")

    elif status:

        add(answered or p.get("answered_et"), "AUDIO", "Audio evidence status: " + status)

    if answered:

        from datetime import timedelta

        for hold in p.get("holds_offsets") or []:

            if isinstance(hold, (list, tuple)) and len(hold) == 2:

                add(answered + timedelta(seconds=_num(hold[0])), "GENESYS", "Hold started")

                add(answered + timedelta(seconds=_num(hold[1])), "GENESYS", "Hold ended")

    for a in p.get("snow_activity_during_call") or []:

        detail = "not linked to this call"

        if a.get("related_to_call"):

            detail = "related (" + str(a.get("related_reason") or "linked") + ")"

        extras = []

        if a.get("within_grace"):

            extras.append("within grace")

        if a.get("during_hold"):

            extras.append("during hold")

        text = (str(a.get("type", "")) + " on " + str(a.get("ticket", "")) + " ("

                                 + str(a.get("company", "")) + ") - " + detail)

        if extras:

            text = text + "; " + ", ".join(extras)

        add(_parse_et(a.get("time_et")) or a.get("time_et"), "SNOW", text)

    for o in p.get("genesys_email_overlap") or []:

        add(_parse_et(o.get("email_start_et")) or o.get("email_start_et"), "GENESYS_EMAIL",

                 "Email interaction " + str(o.get("email_conversation_id", "")) + " started (overlap "

                 + str(o.get("overlap_seconds", "")) + " s)")

        add(_parse_et(o.get("email_end_et")) or o.get("email_end_et"), "GENESYS_EMAIL",

                 "Email interaction " + str(o.get("email_conversation_id", "")) + " ended")

    add(_parse_et(p.get("ended_et")) or p.get("ended_et"), "GENESYS",

             "Call ended (disconnect type: " + str(p.get("disconnect_type") or "unknown")

             + ", raw: " + str(p.get("disconnect_raw") or "-") + ")")

    events.sort(key=lambda e: e[0])

    out = []

    for e in events:

        out.append({"Time (ET)": e[1], "Source": e[2], "Event": e[3], "Origin": "Code"})

    return out

class CustomComponent(Component):

    display_name = "Work Avoidance Reviewer & Tracker"

    description = "Sends each call packet to the Work Avoidance Review mission, validates the output and builds the tracker."

    inputs = [

        DataInput(name="packets", display_name="Work Avoidance Packets", required=True),

        StrInput(name="matcha_base", display_name="Matcha API Base",

            value="https://matcha.harriscomputer.com/rest/api/v1"),

        SecretStrInput(name="matcha_key", display_name="Matcha API Key"),

        IntInput(name="mission_id", display_name="Work Avoidance Review Mission ID", value=0),

        IntInput(name="output_folder_id", display_name="Matcha Copy Folder ID (0 = none)", value=0),

        IntInput(name="max_reviews", display_name="Max AI Reviews per Run (0 = no limit)", value=0),

        BoolInput(name="use_icl", display_name="Include ICL Policy Folder", value=False),

        BoolInput(name="upload_when_empty", display_name="Build Tracker When Nothing Flagged", value=True),

        IntInput(name="parallel_requests", display_name="Parallel AI Requests (1-8)", value=3, advanced=True),

        IntInput(name="time_budget_sec", display_name="Time Budget (s, 0 = none)", value=0, advanced=True),

        IntInput(name="cache_minutes", display_name="Reuse Verdicts Within (minutes, 0 = off)", value=0,

            advanced=True),

        IntInput(name="request_timeout", display_name="Request Timeout (s)", value=240, advanced=True),

        IntInput(name="ticket_mission_id", display_name="SNOW Ticket Identification Mission ID (0 = skip)",

            value=0, advanced=True),

    ]

    outputs = [

        Output(display_name="Run Summary", name="summary", method="run_summary", group_outputs=True),

        Output(display_name="Tracker Data", name="tracker_data", method="tracker_data", group_outputs=True),

    ]

    def _key(self):

        key = (self.matcha_key or "").strip()

        if not key:

            raise ValueError("Matcha API Key is empty. Map it to the MATCHA_API_KEY global variable.")

        return key

    def _complete(self, mission_id, text, options=None):

        base = (self.matcha_base or "").rstrip("/")

        body = {"mission_id": mission_id, "input": text, "stream": False}

        if options:

            body["options"] = options

        headers = {"MATCHA-API-KEY": self._key(), "Content-Type": "application/json",

                                                 "Accept": "application/json"}

        last = ""

        for attempt in range(4):

            if self._stop.is_set():

                raise _Stop(self._stop_reason)

            try:

                r = requests.post(base + "/completions", json=body, headers=headers,

                                                                                         timeout=max(30, _int(self.request_timeout, 240)))

            except requests.RequestException as e:

                last = "network error: " + str(e)[:150]

                time.sleep(5 * (attempt + 1))

                continue

            if r.status_code == 429:

                wait = _int(r.headers.get("Retry-After"), 60)

                if wait > 120:

                    self._stop_reason = "Matcha request limit reached (Retry-After " + str(wait) + " s)."

                    self._stop.set()

                    raise _Stop(self._stop_reason)

                time.sleep(wait)

                continue

            if r.status_code in (401, 403):

                self._stop_reason = ("Matcha HTTP " + str(r.status_code)

                                                                                                 + ": check API key scope, IP whitelist and mission access.")

                self._stop.set()

                raise _Stop(self._stop_reason)

            if r.status_code >= 500:

                last = "Matcha HTTP " + str(r.status_code)

                time.sleep(5 * (attempt + 1))

                continue

            if r.status_code >= 400:

                raise RuntimeError("Matcha HTTP " + str(r.status_code) + ": " + (r.text or "")[:200])

            try:

                out = r.json()

            except ValueError:

                last = "non-JSON response from Matcha"

                continue

            if isinstance(out, dict) and out.get("status") not in (None, "success"):

                raise RuntimeError("Matcha error: " + str(out.get("error"))[:200])

            result = _extract_json(_response_text(out))

            if result:

                return result

            last = "model did not return JSON"

            body["input"] = text + "\n\nReturn ONLY the JSON object defined in the Output section."

        raise RuntimeError(last or "no response from Matcha")

    def _review(self, p):

        payload = {}

        for k, v in p.items():

            if not str(k).startswith("_"):

                payload[k] = v

        return self._complete(_int(self.mission_id, 0),

                                                                                                 "INTERACTION PACKET:\n" + json.dumps(payload, ensure_ascii=False, default=str),

                                                                                                 self._opts)

    def _row(self, p, res, err, snow_ok, today):

        sp = p.get("speech") or {}

        acts = p.get("snow_activity_during_call") or []

        unrelated = _unrelated(p)

        email_rule = _email_rule(p, snow_ok)

        unspoken_rule = _unspoken_rule(p)

        res = res or {}

        if res:

            verdict = _choice(res.get("verdict"), set(VERDICTS), "REVIEW_FAILED")

            if verdict == "REVIEW_FAILED" and not err:

                err = "Invalid verdict from AI: " + str(res.get("verdict"))[:80]

        else:

            verdict = "NOT_REVIEWED" if err.startswith("NOT_REVIEWED") else "REVIEW_FAILED"

        confidence = _choice(res.get("confidence"), {"HIGH", "MEDIUM", "LOW"}, "")

        risk = _risk(res.get("risk_score")) if res else ""

        ai_email = _choice(res.get("email_actioned_during_call"), {"YES", "NO", "UNKNOWN"}, "")

        ai_unspoken = _choice(res.get("call_left_unspoken"), {"YES", "PARTIAL", "NO", "UNKNOWN"}, "")

        qa = ""

        if verdict in VERDICTS:

            qa = _qa_check(p, verdict, confidence, risk, ai_email, ai_unspoken, email_rule, unspoken_rule)

        tickets = set()

        for a in acts:

            if a.get("ticket"):

                tickets.add(str(a.get("ticket")))

        detail = []

        for a in unrelated:

            detail.append(str(a.get("time_et", ""))[-8:] + " " + str(a.get("type", ""))

                                                                 + " " + str(a.get("ticket", "")) + " (" + str(a.get("company", "")) + ")")

        overlap_total = 0.0

        for o in p.get("genesys_email_overlap") or []:

            overlap_total += _num(o.get("overlap_seconds"))

        return {

            "Review Date": today, "Manager": p.get("manager", ""), "Agent": p.get("agent_name", ""),

            "Agent Email": p.get("agent_email", ""), "Conversation ID": p.get("conversation_id", ""),

            "Direction": p.get("direction", ""), "Queue ID": p.get("queue_id", ""),

            "Answered (ET)": p.get("answered_et", ""), "Ended (ET)": p.get("ended_et", ""),

            "Connected (s)": p.get("connected_seconds", ""), "Hold (s)": p.get("hold_seconds", ""),

            "Disconnect Type": p.get("disconnect_type", ""),

            "Disconnect (raw segment)": p.get("disconnect_raw", ""),

            "Audited Agent Released": bool(p.get("agent_released_own_session")),

            "Wrap-up Code": p.get("wrap_up_code", ""),

            "Audit Reasons": ", ".join(p.get("audit_reasons") or []),

            "Audio Evidence": sp.get("transcript_status", ""), "Audio Layout": sp.get("audio_layout", ""),

            "Agent Speech (s)": sp.get("agent_speech_seconds", ""),

            "Caller Speech (s)": sp.get("caller_speech_seconds", ""),

            "First Agent Speech (s)": sp.get("first_agent_speech_seconds", ""),

            "Longest Agent Silence excl. Hold (s)": sp.get("longest_agent_silence_excl_hold_seconds", ""),

            "Caller Speech Bursts Before Agent": sp.get("caller_bursts_before_agent", ""),

            "Agent Words (opening)": sp.get("agent_words", ""),

            "Caller Words (opening)": sp.get("caller_words", ""),

            "Caller Hello Attempts": sp.get("caller_engagement_attempts_before_agent_spoke", ""),

            "Speaker Mapping Warning": bool(sp.get("mapping_warning", False)),

            "Call Left Unspoken": unspoken_rule,

            "SNOW Actions During Call": len(acts), "Unrelated SNOW Actions": len(unrelated),

            "Unrelated SNOW Detail": "; ".join(detail),

            "SNOW Tickets Touched": ", ".join(sorted(tickets)),

            "Genesys Email Overlap (s)": round(overlap_total, 1),

            "Email vs Call": _email_vs_call(p),

            "Email Actioned During Call": email_rule,

            "Call's Own Ticket": p.get("matched_call_ticket", ""),

            "Rule Flags": ", ".join(p.get("flags") or []), "Pre-Score": p.get("pre_score", ""),

            "AI Verdict": verdict, "AI Confidence": confidence, "AI Risk Score": risk,

            "AI Review": str(res.get("ai_review") or "")[:4000],

            "Behaviours Observed": "; ".join(_list(res.get("behaviours_observed"))),

            "Legitimate Explanations": "; ".join(_list(res.get("legitimate_explanations"))),

            "Recommended Action": _choice(res.get("recommended_action"), ACTIONS, ""),

            "Coaching Points": "; ".join(_list(res.get("coaching_points"))),

            "AI Call Left Unspoken": ai_unspoken, "AI Email Actioned": ai_email,

            "QA Check": qa, "Review Error": err,

            "Transcript (opening)": str(sp.get("transcript_excerpt") or "")[:4000],

            "Reviewer": "", "Reviewer Decision": "", "Reviewer Comments": "", "Status": "Open",

        }

    def _execute(self):

        if getattr(self, "_wa_result", None) is not None:

            return self._wa_result

        self._stop = threading.Event()

        self._stop_reason = ""

        self._warnings = []

        d = _unwrap(self.packets)

        if d.get("error"):

            raise ValueError("Upstream error: " + json.dumps(d, default=str)[:500])

        if _int(self.mission_id, 0) <= 0:

            raise ValueError("Set Work Avoidance Review Mission ID.")

        self._key()

        self._opts = {"shouldIncludeICLFolder": True} if self.use_icl else None

        packets = []

        for p in d.get("packets") or []:

            if isinstance(p, dict):

                packets.append(copy.deepcopy(p))

        calls = []

        for c in d.get("calls") or []:

            if isinstance(c, dict):

                calls.append(c)

        snow_ok = bool(d.get("snow_file"))

        packets.sort(key=lambda p: (-_num(p.get("pre_score")), str(p.get("answered_utc") or "")))

        cap = _int(self.max_reviews, 0)

        if cap <= 0 or cap >= len(packets):

            to_review = packets

            capped = []

        else:

            to_review = packets[:cap]

            capped = packets[cap:]

        budget = max(0, _int(self.time_budget_sec, 0))

        deadline = time.time() + budget if budget else None

        workers = max(1, min(8, _int(self.parallel_requests, 3)))

        def work(i, p):

            if self._stop.is_set():

                return i, None, "NOT_REVIEWED: " + self._stop_reason

            if deadline and time.time() > deadline:

                return i, None, "NOT_REVIEWED: time budget reached"

            try:

                return i, self._review(p), ""

            except _Stop as e:

                return i, None, "NOT_REVIEWED: " + str(e)

            except Exception as e:

                return i, None, "AI review failed: " + str(e)[:250]

        results = {}

        if to_review:

            with ThreadPoolExecutor(max_workers=workers) as pool:

                futures = []

                for i, p in enumerate(to_review):

                    futures.append(pool.submit(work, i, p))

                for f in as_completed(futures):

                    i, res, err = f.result()

                    results[i] = (res, err)

        today = datetime.now(ET).strftime("%m/%d/%Y")

        rows = []

        timeline = []

        for i, p in enumerate(to_review):

            res, err = results.get(i, (None, "NOT_REVIEWED: not processed"))

            rows.append(self._row(p, res, err, snow_ok, today))

            base = {"Conversation ID": p.get("conversation_id", ""), "Manager": p.get("manager", ""),

                                         "Agent": p.get("agent_name", "")}

            for e in _evidence(p):

                item = dict(base)

                item.update(e)

                timeline.append(item)

            for ev in (res or {}).get("timeline") or []:

                if isinstance(ev, dict):

                    item = dict(base)

                    item.update({"Time (ET)": str(ev.get("time_et", "")), "Source": str(ev.get("source", "")),

                                                                         "Event": str(ev.get("event", "")), "Origin": "AI"})

                    timeline.append(item)

        for p in capped:

            rows.append(self._row(p, None, "NOT_REVIEWED: Max AI Reviews per Run reached", snow_ok, today))

        rows.sort(key=lambda r: (str(r["Manager"]), str(r["Agent"]),

                                                                                                         _parse_et(r["Answered (ET)"]) or datetime.max))

        summary = {}

        def entry(email, name, manager):

            key = (email or name or "unknown").lower()

            if key not in summary:

                s = {"Manager": manager, "Agent": name, "Agent Email": email,

                                 "Calls Analysed": 0, "Flagged Calls": 0, "Flag Rate %": 0.0}

                for label, _f in SUMMARY_FLAGS:

                    s[label] = 0

                for v in ALL_VERDICTS:

                    s[v] = 0

                summary[key] = s

            return summary[key]

        for c in calls:

            s = entry(c.get("agent_email", ""), c.get("agent_name", ""), c.get("manager", ""))

            flags = set(c.get("flags") or [])

            s["Calls Analysed"] += 1

            if flags & ADVERSE:

                s["Flagged Calls"] += 1

            for label, flag in SUMMARY_FLAGS:

                if flag in flags:

                    s[label] += 1

        for r in rows:

            s = entry(r["Agent Email"], r["Agent"], r["Manager"])

            s[r["AI Verdict"]] = s.get(r["AI Verdict"], 0) + 1

        for s in summary.values():

            if s["Calls Analysed"]:

                s["Flag Rate %"] = round(100.0 * s["Flagged Calls"] / s["Calls Analysed"], 1)

        summary_rows = sorted(summary.values(), key=lambda s: (str(s["Manager"]), str(s["Agent"])))

        summary_df = pd.DataFrame(summary_rows)

        verdicts = {}

        for v in ALL_VERDICTS:

            n = 0

            for r in rows:

                if r["AI Verdict"] == v:

                    n += 1

            verdicts[v] = n

        qa_issues = 0

        for r in rows:

            if r["QA Check"] not in ("", "OK"):

                qa_issues += 1

        reviewed = 0

        for v in VERDICTS:

            reviewed += verdicts[v]

        settings = [

            ("Window", d.get("interval", "")),

            ("Duration band", d.get("duration_band", "")),

            ("SNOW file", d.get("snow_file", "") or "NOT LOADED"),

            ("Inbound calls analysed", len(calls)),

            ("Calls audio-checked", d.get("audio_checked", "")),

            ("Audio checks skipped (cap)", d.get("audio_check_skipped", "")),

            ("Agent track", d.get("agent_track", "")),

            ("Whisper model", d.get("whisper_model", "")),

            ("Speaker mapping warnings", d.get("mapping_warnings", 0)),

            ("Packets received", len(packets)),

            ("AI reviews completed", reviewed),

            ("Not reviewed (cap)", len(capped)),

            ("Mission ID", self.mission_id),

            ("ICL folder used", bool(self.use_icl)),

            ("QA checks raised", qa_issues),

            ("Stop reason", self._stop_reason),

            ("Generated (ET)", datetime.now(ET).strftime(ET_FMT)),

        ]

        for k in ALL_VERDICTS:

            settings.append(("Verdict " + k, verdicts[k]))

        settings_df = pd.DataFrame([{"Setting": k, "Value": v} for k, v in settings])

        agents = set()

        for c in calls:

            if c.get("agent_name"):

                agents.add(str(c.get("agent_name")))

        for r in rows:

            if r["Agent"]:

                agents.add(str(r["Agent"]))

        agents = sorted(agents)

        if len(agents) == 1:

            agent_part = re.sub(r"[^A-Za-z0-9]+", "_", agents[0]).strip("_")

        elif agents:

            agent_part = str(len(agents)) + "_agents"

        else:

            agent_part = "no_agents"

        dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(d.get("interval") or ""))

        date_part = ""

        if dates:

            date_part = dates[0]

            if dates[-1] != dates[0]:

                date_part = dates[0] + "_to_" + dates[-1]

        parts = ["Work_Avoidance_Tracker", agent_part, date_part, datetime.now(ET).strftime("%Y%m%d_%H%M")]

        filename = "_".join(x for x in parts if x) + ".xlsx"

        tracker_df = pd.DataFrame(rows, columns=TRACKER_COLUMNS)

        timeline_df = pd.DataFrame(timeline, columns=TIMELINE_COLUMNS)

        sheets = [("Tracker", tracker_df), ("Evidence Timeline", timeline_df),

                                             ("Agent Summary", summary_df), ("Settings", settings_df)]

        file_id = ""

        uploaded = False

        folder = _int(self.output_folder_id, 0)

        if folder > 0 and (rows or calls or self.upload_when_empty):

            buf = io.BytesIO()

            with pd.ExcelWriter(buf, engine="openpyxl") as xw:

                for name, df in sheets:

                    clean = df

                    if not df.empty:

                        clean = df.apply(lambda col: col.map(_cell))

                    clean.to_excel(xw, sheet_name=name, index=False)

            r = requests.post((self.matcha_base or "").rstrip("/") + "/file",

                                                                                 params={"folder_id": folder},

                                                                                 headers={"MATCHA-API-KEY": self._key(), "Accept": "application/json"},

                                                                                 files={"file": (filename, buf.getvalue(), XLSX_MIME)}, timeout=180)

            if r.status_code >= 400:

                self._warnings.append("Matcha upload failed: HTTP " + str(r.status_code))

            else:

                uploaded = True

                try:

                    j = r.json()

                    if isinstance(j, dict):

                        inner = j.get("data") if isinstance(j.get("data"), dict) else {}

                        file_id = j.get("id") or inner.get("id") or ""

                except ValueError:

                    pass

        def plain(v):

            if hasattr(v, "item"):

                v = v.item()

            if isinstance(v, (list, tuple, set)):

                return "; ".join(str(x) for x in v if x not in (None, ""))

            try:

                if v is None or pd.isna(v):

                    return ""

            except (TypeError, ValueError):

                pass

            if isinstance(v, (str, int, float, bool)):

                return v

            return str(v)

        sheet_data = {}

        for name, df in sheets:

            cols = [str(c) for c in df.columns]

            out_rows = []

            for rec in df.itertuples(index=False, name=None):

                item = {}

                for c, v in zip(cols, rec):

                    item[c] = plain(v)

                out_rows.append(item)

            sheet_data[name] = {"columns": cols, "rows": out_rows}

        warn = list(self._warnings)

        if not snow_ok:

            warn.append("SNOW data not loaded: email/SNOW columns show UNKNOWN")

        if d.get("mapping_warnings"):

            warn.append(str(d.get("mapping_warnings")) + " speaker-mapping warnings: recalibrate Agent Track")

        if self._stop_reason:

            warn.append(self._stop_reason)

        if uploaded:

            head = "Work Avoidance Tracker uploaded to Matcha: " + filename

        else:

            head = "Work Avoidance Tracker built: " + filename

        verdict_bits = []

        for k in ALL_VERDICTS:

            if verdicts[k]:

                verdict_bits.append(k + " " + str(verdicts[k]))

        lines = [

            head,

            "Window: " + str(d.get("interval", "")) + " | duration band " + str(d.get("duration_band", "")),

            "Agents: " + (", ".join(agents) if agents else "none"),

            "Calls analysed: " + str(len(calls)) + " | packets: " + str(len(packets))

                + " | AI-reviewed: " + str(reviewed),

            "Verdicts: " + (", ".join(verdict_bits) or "none"),

            "QA checks raised: " + str(qa_issues),

        ]

        if warn:

            lines.append("WARNINGS: " + " | ".join(warn))

        message = "\n".join(lines)

        self.status = lines[0] + " | " + lines[3]

        self._wa_result = {

            "message": message, "rows": rows, "file_name": filename, "file_id": file_id,

            "uploaded_to_matcha": uploaded, "verdicts": verdicts, "interval": d.get("interval", ""),

            "agents": agents, "warnings": warn, "answered_calls": len(calls),

            "flagged_calls": len(packets), "ai_reviewed": reviewed, "qa_issues": qa_issues,

            "sheets": sheet_data,

        }

        return self._wa_result

    def run_summary(self) -> Message:

        return Message(text=self._execute()["message"])

    def tracker_data(self) -> Data:

        return Data(data=dict(self._execute()))
