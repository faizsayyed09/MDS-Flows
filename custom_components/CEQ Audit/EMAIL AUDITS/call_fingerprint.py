import re
from datetime import datetime, timedelta

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, IntInput, Output
from lfx.schema.message import Message


TICKET_RE = re.compile(r"\b(?:INC|RITM|REQ|TASK|CHG|PRB)\s?\d{5,}\b", re.IGNORECASE)
LINE_RE = re.compile(r"^\[\d{2}:\d{2}\]\s+(AGENT|CALLER|IVR):\s*(.*)$")


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "unknown"


def _eastern(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "n/a"


class CustomComponent(Component):
    display_name = "Call Fingerprint"
    description = "Builds a short search query for the SNOW Ticket Identification mission."

    inputs = [
        DataInput(
            name="call_data",
            display_name="Call Data",
            required=True,
            info="Connect the Structured Data output of the QA Transcriber.",
        ),
        IntInput(
            name="window_minutes",
            display_name="Match Window (minutes)",
            value=60,
        ),
        IntInput(
            name="excerpt_chars",
            display_name="Excerpt Characters",
            value=600,
            info="Keep small. Long queries cause the too_many_clauses error.",
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Fingerprint", name="fingerprint", method="build_fingerprint"),
    ]

    def _call(self):
        cd = self.call_data
        if isinstance(cd, list) and cd:
            cd = cd[0]
        if hasattr(cd, "data"):
            cd = cd.data or {}
        return cd if isinstance(cd, dict) else {}

    def build_fingerprint(self) -> Message:
        call = self._call()
        if call.get("status") != "DONE":
            try:
                self.stop("fingerprint")
            except Exception:
                pass
            return Message(text="FINGERPRINT_SKIPPED: transcription not finished")

        start = _parse_ts(call.get("call_start"))
        end = _parse_ts(call.get("call_end"))
        window = timedelta(minutes=int(self.window_minutes or 60))
        win_from = (start - window) if start else None
        win_to = ((end or start) + window) if start else None

        transcript = call.get("clean_transcript") or ""
        tickets = sorted(set(t.upper().replace(" ", "") for t in TICKET_RE.findall(transcript)))

        turns = []
        for line in transcript.splitlines():
            m = LINE_RE.match(line.strip())
            if m and m.group(1) != "IVR":
                turns.append(line.strip())

        budget = int(self.excerpt_chars or 600)
        opening = []
        used = 0
        for t in turns:
            if used + len(t) > budget * 0.7:
                break
            opening.append(t)
            used += len(t)

        closing = []
        used = 0
        for t in reversed(turns[len(opening):]):
            if used + len(t) > budget * 0.3:
                break
            closing.insert(0, t)
            used += len(t)

        text = "\n".join([
            "CALL FINGERPRINT",
            "Agent (Opened by): " + str(call.get("agent_names") or "unknown"),
            "Agent email: " + (", ".join(call.get("agent_emails") or []) or "unknown"),
            "Call start (UTC): " + _fmt(start),
            "Call end (UTC): " + _fmt(end),
            "Search window (UTC): " + _fmt(win_from) + " to " + _fmt(win_to),
            "Local call start America/New_York: " + (_eastern(start) if start else "n/a"),
            "Wrap-up code(s): " + (", ".join(call.get("wrapup_code_ids") or []) or "none"),
            "Ticket numbers spoken: " + (", ".join(tickets) or "none"),
            "",
            "Opening of conversation:",
            "\n".join(opening) or "(none)",
            "",
            "Closing of conversation:",
            "\n".join(closing) or "(none)",
            "",
            "Identify the ServiceNow record for this call. Return JSON only.",
        ])
        return Message(text=text)
