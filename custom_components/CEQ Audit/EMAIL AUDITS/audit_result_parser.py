import re
import json
import html
from datetime import datetime, timezone

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, DataInput, StrInput, BoolInput, DropdownInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


ATTRIBUTE_SETS = {
    "CALL": [
        ("opening", "Opening", 5),
        ("empathy_accountability_ownership", "Empathy, Accountability & Ownership", 10),
        ("etiquette_professionalism", "Etiquette & Professionalism", 8),
        ("issue_identification", "Issue Identification", 8),
        ("troubleshooting_resolution", "Troubleshooting & Resolution", 12),
        ("process_adherence", "Process Adherence", 14),
        ("hold_wait", "Hold & Wait", 8),
        ("ticket_classification_escalation", "Ticket Classification & Escalation", 12),
        ("case_documentation", "Case Documentation", 18),
        ("closing", "Closing", 5),
    ],
    "EMAIL": [
        ("case_documentation", "Case Documentation", 19),
        ("ticket_classification", "Ticket Classification", 16),
        ("issue_identification_troubleshooting", "Issue Identification & Troubleshooting", 16),
        ("ticket_escalation", "Ticket Escalation", 16),
        ("resource_system_utilization", "Resource & System Utilization", 14),
        ("resolution", "Resolution", 14),
        ("etiquette_professionalism", "Etiquette & Professionalism", 5),
    ],
}


def _extract_json(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the response.")
    return json.loads(text[start:end + 1])


def _text(value):
    if hasattr(value, "text"):
        value = value.text
    return str(value or "")


def _item_text(value):
    if isinstance(value, dict):
        parts = [str(value.get(k)) for k in ("error", "timestamp", "evidence", "reason", "number") if value.get(k)]
        return " - ".join(parts) if parts else ""
    return str(value)


def _as_list(value):
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        value = [value]
    out = []
    for v in value:
        t = _item_text(v).strip()
        if t:
            out.append(t)
    return out


def _result(value):
    s = re.sub(r"[^A-Z]", "", str(value or "").upper())
    if s in ("MET", "YES", "PASS", "TRUE", "ACHIEVED"):
        return "MET"
    if s in ("NOTMET", "NO", "FAIL", "FALSE", "MISSED", "NOTACHIEVED"):
        return "NOT_MET"
    if s in ("NA", "NOTAPPLICABLE"):
        return "NA"
    return "UNKNOWN"


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dur(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    m, s = divmod(max(seconds, 0), 60)
    return ("%dm %02ds" % (m, s)) if m else ("%ds" % s)


class CustomComponent(Component):
    display_name = "Audit Result Parser"
    description = "Scores the CEQ Call Audit output with the all-or-nothing weighted method and builds the email package and dashboard row."

    inputs = [
        MessageTextInput(
            name="mission_response",
            display_name="CEQ Audit Response",
            required=True,
        ),
        DataInput(
            name="call_data",
            display_name="Call Data",
            required=True,
            info="Structured Data output of the QA Transcriber.",
        ),
        MessageTextInput(
            name="ticket_response",
            display_name="Ticket Response (optional)",
            required=False,
        ),
        MessageTextInput(
            name="kb_response",
            display_name="KB Response (optional)",
            required=False,
        ),
        StrInput(
            name="qa_recipients",
            display_name="QA Recipients",
            value="",
            info="Comma-separated emails of QA reviewers / team leads.",
        ),
        BoolInput(
            name="include_agent",
            display_name="CC the Agent",
            value=False,
            info="Leave off until audit quality has been validated by the CEQ team.",
        ),
        StrInput(
            name="pass_threshold",
            display_name="Target Score (%)",
            value="96",
        ),
        DropdownInput(
            name="na_handling",
            display_name="N/A Attributes",
            options=["exclude from total", "award full points"],
            value="exclude from total",
            info="Confirm with the CEQ team how N/A attributes are scored.",
        ),
        StrInput(
            name="subject_prefix",
            display_name="Subject Prefix",
            value="[Call CEQ]",
            advanced=True,
        ),
        StrInput(
            name="review_form_url",
            display_name="Review Form Prefilled URL",
            value="",
            info="Microsoft Forms prefilled link with the Conversation ID answer set to the literal text CONVID.",
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Email Package", name="email_package", method="build_email_package", group_outputs=True),
        Output(display_name="Dashboard Row", name="dashboard_row", method="build_dashboard_row", group_outputs=True),
        Output(display_name="Email Preview", name="email_preview", method="build_email_preview", group_outputs=True),
        Output(display_name="Email Subject", name="email_subject", method="build_email_subject", group_outputs=True),
        Output(display_name="Email To", name="email_to", method="build_email_to", group_outputs=True),
        Output(display_name="Row JSON", name="row_json", method="build_row_json", group_outputs=True),
    ]

    # ---------------- parsing ----------------

    def _call(self):
        cd = self.call_data
        if isinstance(cd, list) and cd:
            cd = cd[0]
        if hasattr(cd, "data"):
            cd = cd.data or {}
        return cd if isinstance(cd, dict) else {}

    @staticmethod
    def _safe_parse(raw):
        raw = _text(raw).strip()
        if not raw:
            return {}, None, False
        try:
            return _extract_json(raw), None, True
        except Exception as e:
            return {}, str(e), True

    def _parse(self):
        if getattr(self, "_parsed", None) is not None:
            return self._parsed

        call = self._call()
        raw_ceq = _text(self.mission_response)
        audit, ceq_error, _ = self._safe_parse(raw_ceq)
        if not raw_ceq.strip():
            ceq_error = "Empty CEQ response."
        ticket, ticket_error, ticket_present = self._safe_parse(self.ticket_response)
        kb, kb_error, kb_present = self._safe_parse(self.kb_response)

        award_na = str(self.na_handling or "").startswith("award")
        attrs = audit.get("attributes") if isinstance(audit.get("attributes"), dict) else {}

        rows = []
        earned = 0.0
        applicable = 0.0
        unknown = []
        channel = str(audit.get("channel") or call.get("channel") or "CALL").upper()
        channel = "EMAIL" if "EMAIL" in channel else "CALL"
        for key, label, weight in ATTRIBUTE_SETS[channel]:
            a = attrs.get(key) if isinstance(attrs.get(key), dict) else {}
            res = _result(a.get("result")) if a else "UNKNOWN"
            if res == "UNKNOWN":
                unknown.append(label)
            points = None
            if res == "MET":
                points = weight
            elif res == "NOT_MET":
                points = 0
            elif award_na:
                points = weight
            if points is not None:
                applicable += weight
                earned += points
            rows.append({
                "key": key,
                "label": label,
                "weight": weight,
                "result": res,
                "points": points,
                "missed": _as_list(a.get("missed")),
                "unverified": _as_list(a.get("unverified")),
                "justification": str(a.get("justification") or ""),
                "evidence": _as_list(a.get("evidence")),
            })

        computed_pct = round(earned / applicable * 100, 1) if applicable else None
        critical = _as_list(audit.get("critical_failures"))
        potential = _as_list(audit.get("potential_critical_failures"))

        target = _to_float(self.pass_threshold) or 93.0
        if target <= 5:
            target = 96.0

        if ceq_error:
            verdict, score_pct = "PARSE_ERROR", None
        elif critical:
            verdict, score_pct = "CRITICAL_FAIL", 0.0
        elif computed_pct is None:
            verdict, score_pct = "INCOMPLETE", None
        elif computed_pct >= target:
            verdict, score_pct = "ACHIEVED", computed_pct
        else:
            verdict, score_pct = "NOT_ACHIEVED", computed_pct

        review_reasons = []
        if critical:
            review_reasons.append("Critical error")
        if potential:
            review_reasons.append("Potential critical error")
        if unknown and not ceq_error:
            review_reasons.append("Missing attribute results: " + ", ".join(unknown))
        if audit.get("manager_review_required"):
            review_reasons.append(str(audit.get("manager_review_reason") or "Flagged by audit"))
        if verdict in ("PARSE_ERROR", "INCOMPLETE"):
            review_reasons.append("Audit could not be scored")

        mission_verdict = str(audit.get("verdict") or "").strip().upper()
        ticket_info = ticket.get("ticket") if isinstance(ticket.get("ticket"), dict) else {}
        doc_checks = ticket.get("documentation_checks") if isinstance(ticket.get("documentation_checks"), dict) else {}

        articles = []
        for a in kb.get("relevantArticles") or []:
            if isinstance(a, dict):
                label = " - ".join([str(x) for x in (a.get("id"), a.get("title")) if x])
                if label:
                    articles.append(label)
            elif a:
                articles.append(str(a))

        self._parsed = {
            "channel": channel,
            "call": call,
            "rows": rows,
            "score_pct": score_pct,
            "target": target,
            "verdict": verdict,
            "mission_verdict": mission_verdict,
            "mission_score_pct": _to_float(audit.get("score_pct")),
            "verdict_overridden": bool(
                mission_verdict
                and verdict not in ("PARSE_ERROR", "INCOMPLETE")
                and mission_verdict != verdict
            ),
            "critical_failures": critical,
            "potential_critical_failures": potential,
            "manager_review_required": bool(review_reasons),
            "manager_review_reason": "; ".join(review_reasons),
            "insanely_great_service": bool(audit.get("insanely_great_service")),
            "insanely_great_service_reason": str(audit.get("insanely_great_service_reason") or ""),
            "fcr": str(audit.get("fcr") or ""),
            "procedure_adherence": str(audit.get("procedure_adherence") or ""),
            "strengths": _as_list(audit.get("strengths")),
            "coaching": _as_list(audit.get("coaching")),
            "flags": _as_list(audit.get("flags")),
            "summary": str(audit.get("summary") or ""),
            "ceq_error": ceq_error,
            "ceq_excerpt": raw_ceq[:500],
            "ticket_present": ticket_present,
            "ticket_error": ticket_error,
            "ticket_status": str(ticket.get("match_status") or ("NOT_RUN" if not ticket_present else "")),
            "ticket_confidence": str(ticket.get("match_confidence") or ""),
            "interaction_type": str(ticket.get("interaction_type") or ""),
            "ticket_number": str(ticket.get("matched_ticket") or ticket_info.get("number") or ""),
            "ticket": ticket_info,
            "doc_checks": doc_checks,
            "documentation_findings": _as_list(ticket.get("documentation_findings")),
            "repeat_contact": bool(ticket.get("repeat_contact")),
            "related_tickets": ticket.get("related_tickets") or [],
            "caller_name": str(ticket.get("caller_name") or ""),
            "issue_summary": str(ticket.get("issueSummary") or kb.get("issueSummary") or ""),
            "client": str(ticket.get("client") or kb.get("client") or ""),
            "kb_present": kb_present,
            "kb_error": kb_error,
            "kb_confidence": str(kb.get("confidence") or ""),
            "kb_articles": articles,
            "kb_gap": bool(kb.get("kb_gap")),
            "kb_gap_note": str(kb.get("kb_gap_note") or ""),
        }
        return self._parsed

    # ---------------- email ----------------

    def _recipients(self, call):
        to = [e.strip() for e in (self.qa_recipients or "").split(",") if e.strip()]
        cc = [e for e in (call.get("agent_emails") or []) if e] if self.include_agent else []
        return to, cc

    def _subject(self, p):
        call = p["call"]
        score = ("%.1f%%" % p["score_pct"]) if p["score_pct"] is not None else "n/a"
        parts = [
            self.subject_prefix or "[Call CEQ]",
            p["verdict"],
            "-",
            str(call.get("agent_names") or "Unknown agent"),
            "-",
            score,
        ]
        if p["manager_review_required"]:
            parts.append("- MANAGER REVIEW")
        parts += ["-", p["ticket_number"] or str(call.get("conversation_id") or "")[:8]]
        return " ".join(parts)

    def _html(self, p):
        call = p["call"]
        esc = html.escape
        colours = {"ACHIEVED": "#1e7e34", "NOT_ACHIEVED": "#b8860b", "CRITICAL_FAIL": "#c82333"}
        colour = colours.get(p["verdict"], "#6c757d")
        score = ("%.1f%%" % p["score_pct"]) if p["score_pct"] is not None else "n/a"
        cell = "padding:6px;border:1px solid #ddd;vertical-align:top"

        def bullets(items, empty="None."):
            if not items:
                return "<p>" + empty + "</p>"
            return "<ul>" + "".join("<li>" + esc(i) + "</li>" for i in items) + "</ul>"

        result_colour = {"MET": "#1e7e34", "NOT_MET": "#c82333"}
        rows = []
        for r in p["rows"]:
            pts = "-" if r["points"] is None else str(r["points"])
            detail = esc(r["justification"])
            if r["missed"]:
                detail += "<br><b>Missed:</b> " + esc("; ".join(r["missed"]))
            if r["unverified"]:
                detail += "<br><span style='color:#888'>Not verifiable: " + esc("; ".join(r["unverified"])) + "</span>"
            if r["evidence"]:
                detail += "<br><span style='color:#666;font-size:12px'>" + esc(" | ".join(r["evidence"])) + "</span>"
            rows.append(
                "<tr><td style='" + cell + "'>" + esc(r["label"]) + "</td>"
                + "<td style='" + cell + ";text-align:center'>" + str(r["weight"]) + "</td>"
                + "<td style='" + cell + ";text-align:center;color:" + result_colour.get(r["result"], "#6c757d")
                + "'><b>" + esc(r["result"]) + "</b></td>"
                + "<td style='" + cell + ";text-align:center'>" + pts + "</td>"
                + "<td style='" + cell + "'>" + detail + "</td></tr>"
            )

        table = (
            "<table style='border-collapse:collapse;width:100%;font-size:13px'>"
            + "<tr style='background:#f2f2f2'>"
            + "<th style='" + cell + "'>Attribute</th>"
            + "<th style='" + cell + "'>Weight</th>"
            + "<th style='" + cell + "'>Result</th>"
            + "<th style='" + cell + "'>Points</th>"
            + "<th style='" + cell + "'>Details</th></tr>"
            + "".join(rows)
            + "</table>"
        )

        banner = ""
        if p["manager_review_required"]:
            banner = (
                "<p style='background:#fff3cd;padding:8px;border-left:4px solid #b8860b'>"
                + "<b>Manager review required:</b> " + esc(p["manager_review_reason"]) + "</p>"
            )
        if p["ceq_error"]:
            banner += (
                "<p style='color:#c82333'><b>CEQ response could not be parsed:</b> " + esc(p["ceq_error"])
                + "</p><pre style='font-size:11px;white-space:pre-wrap'>" + esc(p["ceq_excerpt"]) + "</pre>"
            )
        if p["verdict_overridden"]:
            banner += (
                "<p style='font-size:12px;color:#555'>Mission suggested <b>" + esc(p["mission_verdict"])
                + "</b>; the calculated verdict was applied.</p>"
            )

        critical = ""
        if p["critical_failures"]:
            critical += "<h3 style='color:#c82333'>Critical Errors</h3>" + bullets(p["critical_failures"])
        if p["potential_critical_failures"]:
            critical += (
                "<h3 style='color:#b8860b'>Potential Critical Errors (verify)</h3>"
                + bullets(p["potential_critical_failures"])
            )

        if not p["ticket_present"]:
            ticket_block = "<p>Ticket matching not run.</p>"
        elif p["ticket_error"]:
            ticket_block = "<p>Ticket response could not be parsed: " + esc(p["ticket_error"]) + "</p>"
        else:
            t = p["ticket"]
            match_text = p["ticket_status"]
            if p["ticket_confidence"]:
                match_text += " (" + p["ticket_confidence"] + ")"
            info = [
                ("Match", match_text),
                ("Interaction", p["interaction_type"] or "-"),
                ("Ticket", p["ticket_number"] or "-"),
                ("Short description", t.get("short_description") or "-"),
                ("Priority / State", (t.get("priority") or "-") + " / " + (t.get("state") or "-")),
                ("Assignment group", t.get("assignment_group") or "-"),
                ("Repeat contact", "Yes" if p["repeat_contact"] else "No"),
            ]
            ticket_block = "<table style='font-size:13px'>" + "".join(
                "<tr><td style='padding:3px 8px;color:#555'>" + esc(k) + "</td>"
                + "<td style='padding:3px 8px'>" + esc(str(v)) + "</td></tr>"
                for k, v in info
            ) + "</table>"
            if p["documentation_findings"]:
                ticket_block += "<p><b>Documentation findings:</b></p>" + bullets(p["documentation_findings"])

        if not p["kb_present"]:
            kb_block = "<p>KB lookup not run.</p>"
        elif p["kb_error"]:
            kb_block = "<p>KB response could not be parsed: " + esc(p["kb_error"]) + "</p>"
        else:
            kb_block = (
                "<p>Procedure adherence: <b>" + esc(p["procedure_adherence"] or "-")
                + "</b> | KB confidence: <b>" + esc(p["kb_confidence"] or "-") + "</b></p>"
                + bullets(p["kb_articles"], "No articles returned.")
            )
            if p["kb_gap"]:
                kb_block += "<p style='color:#b8860b'><b>KB gap:</b> " + esc(p["kb_gap_note"]) + "</p>"

        if p["channel"] == "EMAIL":
            span_h = round(float(call.get("audio_duration_sec") or 0) / 3600, 1)
            metrics = [
                ("Conversation ID", call.get("conversation_id")),
                ("First message (UTC)", call.get("call_start")),
                ("Client", p["client"] or "-"),
                ("Requester", p["caller_name"] or "-"),
                ("Issue", p["issue_summary"] or "-"),
                ("FCR", p["fcr"] or "-"),
                ("Thread span", str(span_h) + " h"),
                ("Messages", str(call.get("message_count")) + " (" + str(call.get("agent_reply_count")) + " agent replies)"),
                ("First response", str(call.get("first_response_min")) + " min"),
                ("Tickets in thread", ", ".join(call.get("ticket_numbers") or []) or "-"),
            ]
        else:
            metrics = [
                ("Conversation ID", call.get("conversation_id")),
                ("Call start (UTC)", call.get("call_start")),
                ("Client", p["client"] or "-"),
                ("Caller", p["caller_name"] or "-"),
                ("Issue", p["issue_summary"] or "-"),
                ("FCR", p["fcr"] or "-"),
                ("Duration", _dur(call.get("audio_duration_sec"))),
                ("Hold time", _dur(call.get("total_hold_sec")) + " (" + str(call.get("hold_percentage")) + "%)"),
                ("Dead air", _dur(call.get("total_dead_air_sec"))),
                ("Agent talk ratio", str(call.get("agent_talk_ratio")) + "%"),
            ]
            
        metric_rows = "".join(
            "<tr><td style='padding:3px 8px;color:#555'>" + esc(str(k)) + "</td>"
            + "<td style='padding:3px 8px'>" + esc(str(v)) + "</td></tr>"
            for k, v in metrics
        )

        igs = ""
        if p["insanely_great_service"]:
            igs = (
                "<p style='color:#1e7e34'><b>Insanely Great Service:</b> "
                + esc(p["insanely_great_service_reason"]) + "</p>"
            )

        review = ""
        form = (self.review_form_url or "").strip()
        if form and call.get("conversation_id"):
            link = form.replace("CONVID", str(call.get("conversation_id")))
            review = (
                "<p style='margin-top:18px'><a href='" + esc(link) + "' "
                + "style='background:#0067b8;color:#fff;padding:8px 14px;text-decoration:none;border-radius:4px'>"
                + "Review this audit</a></p>"
            )

        return (
            "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:820px'>"
            + "<h2 style='margin-bottom:4px'>CEQ " + esc(p["channel"].title()) + " Audit - "
            + esc(str(call.get("agent_names") or "Unknown agent")) + "</h2>"
            + "<p style='font-size:18px;margin-top:0'>Verdict: <b style='color:" + colour + "'>"
            + esc(p["verdict"]) + "</b> | Score: <b>" + esc(score) + "</b> (target "
            + ("%g" % p["target"]) + "%)</p>"
            + banner
            + igs
            + "<h3>Summary</h3><p>" + (esc(p["summary"]) or "No summary returned.") + "</p>"
            + critical
            + "<h3>Attributes</h3>" + table
            + "<h3>Strengths</h3>" + bullets(p["strengths"])
            + "<h3>Coaching Points</h3>" + bullets(p["coaching"])
            + "<h3>Flags</h3>" + bullets(p["flags"])
            + "<h3>Ticket</h3>" + ticket_block
            + "<h3>Procedure</h3>" + kb_block
            + "<h3>" + ("Email" if p["channel"] == "EMAIL" else "Call") + " Details</h3><table style='font-size:13px'>" + metric_rows + "</table>"
            + review
            + "<p style='font-size:11px;color:#888;margin-top:20px'>Automated audit generated from "
            + ("the Genesys email thread and ServiceNow export. Verify against the ticket" if p["channel"] == "EMAIL"
               else "a machine transcript. Verify against the recording")
            + " before taking action. Disputes follow the standard CEQ dispute process.</p>"
            + "</div>"
        )

    # ---------------- outputs ----------------

    def build_email_package(self) -> Data:
        p = self._parse()
        to, cc = self._recipients(p["call"])
        return Data(data={
            "to": to,
            "cc": cc,
            "subject": self._subject(p),
            "html": self._html(p),
            "verdict": p["verdict"],
            "manager_review_required": p["manager_review_required"],
            "parse_error": p["ceq_error"],
        })

    def build_email_preview(self) -> Message:
        return Message(text=self._html(self._parse()))

    def build_email_subject(self) -> Message:
        return Message(text=self._subject(self._parse()))

    def build_email_to(self) -> Message:
        p = self._parse()
        to, cc = self._recipients(p["call"])
        return Message(text=", ".join(to + cc))

    def build_row_json(self) -> Message:
        row = self.build_dashboard_row().data
        return Message(text=json.dumps(row, ensure_ascii=False, default=str))

    def build_dashboard_row(self) -> Data:
        p = self._parse()
        call = p["call"]
        t = p["ticket"]
        row = {
            "channel": p["channel"],
            "audited_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conversation_id": call.get("conversation_id"),
            "recording_id": call.get("recording_id"),
            "agent_name": call.get("agent_names"),
            "agent_email": ", ".join(call.get("agent_emails") or []),
            "queue_id": ", ".join(call.get("queue_ids") or []),
            "wrapup_code": ", ".join(call.get("wrapup_code_ids") or []),
            "call_start_utc": call.get("call_start"),
            "client": p["client"],
            "caller_name": p["caller_name"],
            "issue_summary": p["issue_summary"],
            "duration_sec": call.get("audio_duration_sec"),
            "hold_sec": call.get("total_hold_sec"),
            "hold_count": call.get("hold_event_count"),
            "dead_air_sec": call.get("total_dead_air_sec"),
            "agent_talk_ratio": call.get("agent_talk_ratio"),
            "overtalk_sec": call.get("overtalk_sec"),
            "score_pct": p["score_pct"],
            "target_pct": p["target"],
            "verdict": p["verdict"],
            "mission_verdict": p["mission_verdict"],
            "mission_score_pct": p["mission_score_pct"],
            "verdict_overridden": p["verdict_overridden"],
            "critical_failure_count": len(p["critical_failures"]),
            "critical_failures": " | ".join(p["critical_failures"]),
            "potential_critical_count": len(p["potential_critical_failures"]),
            "potential_critical_failures": " | ".join(p["potential_critical_failures"]),
            "manager_review_required": p["manager_review_required"],
            "manager_review_reason": p["manager_review_reason"],
            "insanely_great_service": p["insanely_great_service"],
            "fcr": p["fcr"],
            "procedure_adherence": p["procedure_adherence"],
            "strengths": " | ".join(p["strengths"]),
            "coaching": " | ".join(p["coaching"]),
            "flags": " | ".join(p["flags"]),
            "summary": p["summary"],
            "ticket_match_status": p["ticket_status"],
            "ticket_confidence": p["ticket_confidence"],
            "interaction_type": p["interaction_type"],
            "ticket_number": p["ticket_number"],
            "ticket_type": t.get("type"),
            "ticket_priority": t.get("priority"),
            "ticket_state": t.get("state"),
            "ticket_assignment_group": t.get("assignment_group"),
            "repeat_contact": p["repeat_contact"],
            "documentation_findings": " | ".join(p["documentation_findings"]),
            "kb_confidence": p["kb_confidence"],
            "kb_articles": " | ".join(p["kb_articles"]),
            "kb_gap": p["kb_gap"],
            "hold_source": call.get("hold_source"),
            "transcription_model": call.get("model_used"),
            "ceq_parse_error": p["ceq_error"],
            "ticket_parse_error": p["ticket_error"],
            "kb_parse_error": p["kb_error"],
        }
        for r in p["rows"]:
            row["result_" + r["key"]] = r["result"]
            row["points_" + r["key"]] = r["points"]
        for k, v in (p["doc_checks"] or {}).items():
            row["doc_" + k] = v
        return Data(data=row)
