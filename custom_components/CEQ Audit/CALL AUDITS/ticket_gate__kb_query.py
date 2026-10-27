import re
import json

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, DataInput, Output
from lfx.schema.message import Message


LOC_RE = re.compile(
    r"^\s*(Department|Dept|Location|Device Location|Site calling from|Site|Ward calling from|Ward|Unit)"
    r"\s*[:\-]+\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _text(v):
    if hasattr(v, "text"):
        v = v.text
    return str(v or "")


def _json(raw):
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        return json.loads(raw[s:e + 1])
    except ValueError:
        return None


def _data(v):
    if isinstance(v, list) and v:
        v = v[0]
    if hasattr(v, "data"):
        v = v.data or {}
    return v if isinstance(v, dict) else {}


def _get(row, *keys):
    low = {str(k).strip().lower(): v for k, v in (row or {}).items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def _from_row(row):
    return {
        "ticket": {
            "number": _get(row, "Number"),
            "type": _get(row, "Type"),
            "opened_et": _get(row, "Opened"),
            "opened_by": _get(row, "Opened by"),
            "updated_et": _get(row, "Updated", "Updated(u_updated_on)"),
            "updated_by": _get(row, "Updated by", "Updated by(u_updated_by)"),
            "account": _get(row, "Company"),
            "short_description": _get(row, "Short description"),
            "contact_type": _get(row, "Contact type"),
            "assignment_group": _get(row, "Assignment group"),
            "priority": _get(row, "Priority"),
            "state": _get(row, "State"),
        },
        "ticket_notes": {
            "description_excerpt": _get(row, "Description")[:1500],
            "work_notes_excerpt": _get(row, "Work notes")[:1500],
            "additional_comments_excerpt": _get(row, "Additional comments")[:1000],
        },
    }


class CustomComponent(Component):
    display_name = "Ticket Gate + KB Query"
    description = "Accepts or overrides the SNOW mission using the deterministic pre-match, then builds a scoped KB query."

    inputs = [
        MessageTextInput(name="ticket_response", display_name="SNOW Mission Response", required=True),
        DataInput(name="prematch", display_name="Pre-Match Data", required=True),
    ]

    outputs = [
        Output(display_name="Ticket JSON", name="ticket_json", method="build_ticket", group_outputs=True),
        Output(display_name="KB Query", name="kb_query", method="build_kb_query", group_outputs=True),
    ]

    def _run(self):
        if getattr(self, "_gate", None) is not None:
            return self._gate

        pm = _data(self.prematch)
        mission = _json(_text(self.ticket_response)) or {}
        m_num = str(mission.get("matched_ticket") or "").strip().upper()
        status = pm.get("status") or "NO_EXPORT"
        best = str(pm.get("best_ticket") or "").upper()
        rows = pm.get("candidate_rows") or {}
        notes = []

        if status in ("NO_EXPORT", "SKIPPED"):
            final, basis = m_num, "mission only - export not available to code (unverified)"
        elif status == "MATCHED":
            final, basis = best, "deterministic HIGH match"
            if m_num and m_num != best:
                notes.append("Mission proposed " + m_num + "; overridden by " + best)
        elif m_num and m_num in rows:
            final, basis = m_num, "mission chose among code candidates"
        else:
            final, basis = "", "no verifiable ticket"
            if m_num:
                notes.append("Mission proposed " + m_num + " - not a code candidate for this agent/window; rejected")

        out = dict(mission)
        check_keys = list((mission.get("documentation_checks") or {}).keys())

        if final and final != m_num and final in rows:
            out.update(_from_row(rows[final]))
            out["documentation_checks"] = {k: "UNKNOWN" for k in check_keys}
            out["documentation_findings"] = []
            out["issueSummary"] = ""
            out["product"] = ""

        if final:
            out["matched_ticket"] = final
            out["match_status"] = "MATCHED"
            out["match_confidence"] = "HIGH" if status == "MATCHED" else (mission.get("match_confidence") or "MEDIUM")
        else:
            out.update({
                "match_status": "NOT_FOUND",
                "match_confidence": "LOW",
                "matched_ticket": "",
                "ticket": {},
                "ticket_notes": {},
                "documentation_findings": [],
                "issueSummary": "",
            })
            out["documentation_checks"] = {k: "UNKNOWN" for k in check_keys}

        facts = {}
        for c in pm.get("candidates") or []:
            if c.get("number") == final:
                facts = c.get("facts") or {}

        checks = out.get("documentation_checks") if isinstance(out.get("documentation_checks"), dict) else {}
        if facts.get("caller_phone_in_ticket") is True:
            checks["contact_info_documented"] = "YES"
        if "contact_type_matches_channel" in facts:
            checks["contact_type_matches_channel"] = "YES" if facts["contact_type_matches_channel"] else "NO"
        out["documentation_checks"] = checks

        out["verification"] = {
            "basis": basis,
            "notes": notes,
            "facts": facts,
            "candidates": [{"number": c.get("number"), "score": c.get("score")} for c in pm.get("candidates") or []],
            "prematch_status": pm.get("status"),
            "export_error": pm.get("export_error"),
        }

        self._gate = {"ticket": out, "pm": pm, "row": rows.get(final, {})}
        self.status = (final or "NOT_FOUND") + " | " + basis
        return self._gate

    def build_ticket(self) -> Message:
        return Message(text=json.dumps(self._run()["ticket"], ensure_ascii=False))

    def build_kb_query(self) -> Message:
        g = self._run()
        t = g["ticket"].get("ticket") or {}
        pm, row = g["pm"], g["row"]

        locs = []
        for m in LOC_RE.finditer(_get(row, "Description")):
            v = m.group(2).strip()
            if v and v.upper() not in ("NA", "N/A", "-"):
                locs.append(m.group(1).title() + ": " + v[:80])
        locs += pm.get("location_statements") or []

        parts = []
        if t.get("short_description"):
            parts.append("Ticket: " + t["short_description"])
        if pm.get("issue_excerpt"):
            parts.append("Interaction: " + pm["issue_excerpt"])

        query = {
            "issueSummary": " | ".join(parts) or "UNKNOWN - no verified ticket or issue text. Return confidence Low and kb_gap true.",
            "client": t.get("account") or g["ticket"].get("client") or "",
            "product": g["ticket"].get("product") or "",
            "location": " ; ".join(locs[:5]) or "location not stated",
            "scope_rule": "Site/clinic/department-specific routing applies only if the location above names that site.",
        }
        return Message(text=json.dumps(query, ensure_ascii=False))
