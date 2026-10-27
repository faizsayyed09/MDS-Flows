import json

import re

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from lfx.custom.custom_component.component import Component

from lfx.io import DataInput, DropdownInput, IntInput, MultilineInput, Output

from lfx.schema.data import Data

ET = ZoneInfo("America/New_York")

ET_FMT = "%m/%d/%Y %H:%M:%S"

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

def _et(d):

    if not isinstance(d, datetime):

        return ""

    return d.astimezone(ET).strftime(ET_FMT)

def _digits(value):

    d = re.sub(r"\D", "", str(value or ""))

    if len(d) >= 10:

        return d[-10:]

    return ""

def _int(value, default):

    try:

        return int(float(value))

    except (TypeError, ValueError):

        return default

class CustomComponent(Component):

    display_name = "Timeline & Overlap Detector"

    description = "Overlaps each answered call with the agent's SNOW actions and Genesys email work, and selects calls for audio checks."

    inputs = [

        DataInput(name="interactions", display_name="Genesys Interactions", required=True),

        DataInput(name="snow", display_name="SNOW Activity", required=True),

        DropdownInput(name="audit_scope", display_name="Audio Check Scope",

            options=["candidates only", "all answered calls"], value="all answered calls"),

        DropdownInput(name="call_direction", display_name="Call Direction",

            options=["inbound only", "outbound only", "both"], value="inbound only"),

        IntInput(name="min_connected_sec", display_name="Only Calls Longer Than (seconds)", value=60,

            info="0 = no minimum. 60 keeps calls over 1 minute."),

        IntInput(name="max_connected_sec", display_name="Only Calls Shorter Than (seconds)", value=240,

            info="0 = no maximum. 240 keeps calls under 4 minutes."),

        IntInput(name="short_call_sec", display_name="Short Call Threshold (seconds)", value=120,

            info="Calls this short are treated as audio-check candidates."),

        IntInput(name="grace_seconds", display_name="Grace Seconds at Call Start/End", value=60),

        MultilineInput(name="call_ticket_map", display_name="Known Call to Ticket Map (JSON)", value="",

            advanced=True, info='{"<conversationId>": ["INC0012345"]}'),

    ]

    outputs = [Output(display_name="Detections", name="detections", method="detect")]

    def _snow_index(self, snow):

        out = {}

        for r in snow.get("rows") or []:

            if str(r.get("Activity Type", "")).upper() == "EMAIL_RECEIVED":

                continue

            t = _dt(r.get("activity_utc"))

            if t:

                out.setdefault(str(r.get("agent_email_norm") or ""), []).append((t, r))

        return out

    def _emails(self, items):

        out = []

        for e in items:

            if e.get("media") != "email":

                continue

            spans = []

            for a, b in e.get("interact") or []:

                spans.append((_dt(a), _dt(b)))

            rec = dict(e)

            rec["_inter"] = spans

            out.append(rec)

        return out

    def _acts(self, index, call, start, end, holds, grace, mapped, ani10):

        acts = []

        for t, r in index.get(str(call.get("agent_email") or ""), []):

            if t < start or t > end:

                continue

            num = str(r.get("Ticket Number", "")).strip().upper()

            reason = ""

            if num and num in mapped:

                reason = "mapped to call"

            elif ani10 and _digits(r.get("Caller Phone")) == ani10:

                reason = "caller phone = ANI"

            in_hold = False

            for a, b in holds:

                if a and b and a <= t <= b:

                    in_hold = True

            near_start = (t - start).total_seconds() < grace

            near_end = (end - t).total_seconds() < grace

            item = {

                "time_et": _et(t),

                "offset_s": round((t - start).total_seconds(), 1),

                "type": r.get("Activity Type", ""),

                "ticket": num,

                "company": r.get("Company", ""),

                "short_description": str(r.get("Short description", ""))[:150],

                "email_subject": str(r.get("Email Subject", ""))[:150],

                "related_to_call": bool(reason),

                "related_reason": reason,

                "within_grace": bool(near_start or near_end),

                "during_hold": in_hold,

            }

            acts.append(item)

        return acts

    def _overlaps(self, emails, call, inter, start, end):

        out = []

        for e in emails:

            if e.get("user_id") != call.get("user_id"):

                continue

            ov = 0.0

            for a1, b1 in inter:

                for a2, b2 in e["_inter"]:

                    if a1 and b1 and a2 and b2:

                        gap = (min(b1, b2) - max(a1, a2)).total_seconds()

                        if gap > 0:

                            ov += gap

            if ov <= 0:

                continue

            e_start = _dt(e.get("start"))

            e_end = _dt(e.get("end"))

            started_during = bool(e_start and start <= e_start <= end)

            ended_during = bool(e_end and start <= e_end <= end)

            if started_during and ended_during:

                rel = "opened and finished during the call"

            elif started_during:

                rel = "opened during the call"

            elif ended_during:

                rel = "finished during the call"

            else:

                rel = "already open before the call and still open after it"

            item = {

                "email_conversation_id": e.get("conversation_id"),

                "overlap_seconds": round(ov, 1),

                "email_start_et": _et(e_start),

                "email_end_et": _et(e_end),

                "email_started_during_call": started_during,

                "email_ended_during_call": ended_during,

                "relation": rel,

            }

            out.append(item)

        return out

    def detect(self) -> Data:

        g = _unwrap(self.interactions)

        s = _unwrap(self.snow)

        if g.get("error"):

            return Data(data=g)

        grace = max(0, _int(self.grace_seconds, 60))

        short = max(0, _int(self.short_call_sec, 120))

        lo = max(0, _int(self.min_connected_sec, 0))

        hi = max(0, _int(self.max_connected_sec, 0))

        want = str(self.call_direction or "inbound only").lower()

        tmap = {}

        if (self.call_ticket_map or "").strip():

            try:

                tmap = json.loads(self.call_ticket_map)

            except ValueError:

                tmap = {}

        index = self._snow_index(s)

        items = g.get("interactions") or []

        emails = self._emails(items)

        calls = []

        skipped_dir = 0

        skipped_len = 0

        for v in items:

            if v.get("media") != "voice":

                continue

            direction = str(v.get("direction") or "").lower()

            if want == "inbound only" and direction != "inbound":

                skipped_dir += 1

                continue

            if want == "outbound only" and direction != "outbound":

                skipped_dir += 1

                continue

            start = _dt(v.get("start"))

            end = _dt(v.get("end"))

            if start is None or end is None:

                continue

            inter = []

            for a, b in v.get("interact") or []:

                inter.append((_dt(a), _dt(b)))

            holds = []

            for a, b in v.get("holds") or []:

                holds.append((_dt(a), _dt(b)))

            connected = 0.0

            for a, b in inter:

                if a and b:

                    connected += (b - a).total_seconds()

            connected = round(connected, 1)

            if connected < lo:

                skipped_len += 1

                continue

            if hi and connected > hi:

                skipped_len += 1

                continue

            hold_s = 0.0

            for a, b in holds:

                if a and b:

                    hold_s += (b - a).total_seconds()

            ani10 = _digits(v.get("ani"))

            mapped = set()

            for x in tmap.get(v.get("conversation_id")) or []:

                mapped.add(str(x).upper())

            acts = self._acts(index, v, start, end, holds, grace, mapped, ani10)

            overlaps = self._overlaps(emails, v, inter, start, end)

            reasons = []

            for a in acts:

                if not a["related_to_call"] and not a["within_grace"]:

                    reasons.append("SNOW_ACTIVITY_DURING_CALL")

                    break

            for o in overlaps:

                if o["overlap_seconds"] > grace:

                    reasons.append("GENESYS_EMAIL_DURING_CALL")

                    break

            if connected <= short:

                reasons.append("SHORT_CALL")

            hold_offsets = []

            for a, b in holds:

                if a and b:

                    hold_offsets.append([round((a - start).total_seconds(), 1),

                                                                                                     round((b - start).total_seconds(), 1)])

            window = _et(start - timedelta(minutes=10)) + " to " + _et(end + timedelta(minutes=30))

            rec = {

                "conversation_id": v.get("conversation_id"),

                "user_id": v.get("user_id"),

                "agent_name": v.get("agent_name", ""),

                "agent_email": v.get("agent_email", ""),

                "manager": v.get("manager", ""),

                "queue_id": v.get("queue_id", ""),

                "direction": v.get("direction", ""),

                "caller_ani": v.get("ani", ""),

                "caller_name": v.get("caller_name", ""),

                "answered_utc": v.get("start"),

                "ended_utc": v.get("end"),

                "answered_et": _et(start),

                "ended_et": _et(end),

                "search_window_et": window,

                "connected_seconds": connected,

                "hold_seconds": round(hold_s, 1),

                "holds_offsets": hold_offsets,

                "disconnect_type": v.get("disconnect_type", ""),

                "disconnect_raw": v.get("disconnect_raw", ""),

                "agent_released_own_session": bool(v.get("agent_released_own_session")),

                "wrap_up_code": v.get("wrapup", ""),

                "snow_activity_during_call": acts,

                "genesys_email_overlap": overlaps,

                "audit_reasons": reasons,

                "audio_check": bool(reasons) or str(self.audit_scope or "").startswith("all"),

                "grace_seconds": grace,

            }

            calls.append(rec)

        n = 0

        for c in calls:

            if c["audio_check"]:

                n += 1

        band = str(lo) + "-" + (str(hi) if hi else "inf") + " s"

        self.status = (str(len(calls)) + " calls in " + band + " | " + str(n) + " selected for audio check"

                                                             + " | skipped: " + str(skipped_dir) + " by direction, "

                                                             + str(skipped_len) + " by duration")

        return Data(data={

            "calls": calls,

            "interval": g.get("interval", ""),

            "snow_file": s.get("source_file", ""),

            "duration_band": band,

            "skipped_direction": skipped_dir,

            "skipped_duration": skipped_len,

        })
