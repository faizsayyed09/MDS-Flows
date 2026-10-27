import re
import json
import html
from datetime import datetime, timezone

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, StrInput, MultilineInput, IntInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TICKET_RE = re.compile(r"\b(?:INC|RITM|REQ|TASK|CHG|PRB)\s?\d{5,}\b", re.IGNORECASE)
_AUTO_ACK_RE = re.compile(
    r"(we have received your (?:request|email|message)|this is an automated|do not reply)",
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(
    r"\n\s*(?:From:\s|-{2,}\s*Original Message|On .{5,120} wrote:)",
    re.IGNORECASE,
)


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _et(dt, fmt):
    if not dt:
        return "-"
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime(fmt)
    except Exception:
        return dt.strftime(fmt)


def _html_to_text(value):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", value or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _clean_body(text, max_chars):
    text = "\n" + (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cut = _QUOTE_RE.search(text)
    if cut and cut.start() > 40:
        text = text[: cut.start()]
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\s*\n\s*", " / ", text.strip())
    text = re.sub(r"(?:\s*/\s*){2,}", " / ", text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + " ...[truncated]"
    return text.strip(" /")


class CustomComponent(Component):
    display_name = "Genesys Email Fetcher"
    description = "Fetches a Genesys email interaction thread and produces an audit-ready payload with the same structure as the call transcriber."

    inputs = [
        DataInput(
            name="auth",
            display_name="Genesys Auth",
            required=True,
            info="Output of the Genesys Cloud Auth component.",
        ),
        MessageTextInput(
            name="conversation_id",
            display_name="Conversation ID",
            required=False,
        ),
        DataInput(
            name="trigger_data",
            display_name="Trigger Data (optional)",
            required=False,
        ),
        StrInput(
            name="support_domains",
            display_name="Support Mailbox Domains",
            value="alterahealth.com",
            info="Comma-separated. Senders from these domains are treated as AGENT.",
        ),
        MultilineInput(
            name="audit_instructions",
            display_name="Audit Mission Instructions",
            value=(
                "Audit the email interaction below using your CEQ policy (CHANNEL: EMAIL), together "
                "with the TICKET MATCH and EXPECTED PROCEDURE context. Return JSON only, in your defined schema."
            ),
        ),
        IntInput(
            name="max_body_chars",
            display_name="Max Characters per Message",
            value=3000,
            advanced=True,
        ),
        IntInput(
            name="max_thread_chars",
            display_name="Max Thread Characters",
            value=40000,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Mission Payload", name="mission_payload", method="build_email_payload", group_outputs=True),
        Output(display_name="Structured Data", name="structured", method="build_email_structured", group_outputs=True),
        Output(display_name="Email Report", name="email_report", method="build_email_report", group_outputs=True),
    ]

    # ---------------- helpers ----------------

    def _auth_data(self):
        auth = self.auth
        if isinstance(auth, list) and auth:
            auth = auth[0]
        if hasattr(auth, "data"):
            return auth.data or {}
        return auth if isinstance(auth, dict) else {}

    def _conversation_id(self):
        candidates = []
        td = self.trigger_data
        if isinstance(td, list) and td:
            td = td[0]
        if hasattr(td, "data"):
            td = td.data or {}
        if isinstance(td, dict) and td:
            candidates.append(json.dumps(td))
        raw = self.conversation_id
        if hasattr(raw, "text"):
            raw = raw.text
        if raw:
            candidates.append(str(raw))
        for c in candidates:
            m = _UUID_RE.search(c)
            if m:
                return m.group(0).lower()
        return ""

    def _get(self, base, headers, path, params=None):
        resp = requests.get(base + path, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            raise RuntimeError("HTTP 401 - Genesys token expired or invalid")
        if resp.status_code == 403:
            raise RuntimeError("HTTP 403 - missing permission for " + path)
        if resp.status_code == 404:
            raise RuntimeError("HTTP 404 - not found: " + path)
        if resp.status_code >= 400:
            raise RuntimeError("HTTP " + str(resp.status_code) + " - " + (resp.text or "")[:200])
        return resp.json() if (resp.text or "").strip() else {}

    def _halt(self, output_name):
        try:
            self.stop(output_name)
        except Exception:
            pass

    # ---------------- main ----------------

    def _run(self):
        if getattr(self, "_result", None) is not None:
            return self._result

        auth = self._auth_data()
        token = auth.get("access_token")
        if auth.get("error") or not token:
            raise RuntimeError("Genesys auth missing or failed: " + str(auth.get("error") or "no token"))
        base = "https://api." + auth.get("region", "mypurecloud.com")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

        conv_id = self._conversation_id()
        if not conv_id:
            raise RuntimeError("No conversation ID. Type one or connect Trigger Data.")

        domains = [
            d.strip().lower().lstrip("@") for d in (self.support_domains or "").split(",") if d.strip()
        ]
        max_body = int(self.max_body_chars or 3000)

        listing = []
        page = 1
        while page <= 10:
            data = self._get(
                base, headers,
                "/api/v2/conversations/emails/" + conv_id + "/messages",
                {"pageSize": 100, "pageNumber": page},
            )
            batch = data.get("entities") or []
            listing.extend(batch)
            if not batch or page >= int(data.get("pageCount") or 1):
                break
            page += 1
        if not listing:
            raise RuntimeError("No email messages found for " + conv_id + ". Is this an email conversation?")

        messages = []
        sources = {"text": 0, "html": 0, "preview": 0}
        for item in listing:
            merged = dict(item)
            try:
                merged.update(
                    self._get(base, headers, "/api/v2/conversations/emails/" + conv_id + "/messages/" + str(item.get("id")))
                    or {}
                )
            except Exception:
                pass
            if merged.get("textBody"):
                body, source = merged["textBody"], "text"
            elif merged.get("htmlBody"):
                body, source = _html_to_text(merged["htmlBody"]), "html"
            else:
                body, source = merged.get("textBodyPreview") or "", "preview"
            sources[source] += 1

            sender = merged.get("from") or {}
            address = str(sender.get("email") or "").lower()
            domain = address.split("@")[-1] if "@" in address else ""
            is_agent = any(domain == d or domain.endswith("." + d) for d in domains)
            attachments = merged.get("attachments") or []
            messages.append({
                "time": _parse_ts(merged.get("time")),
                "from_name": sender.get("name") or address,
                "role": "AGENT" if is_agent else "CALLER",
                "subject": merged.get("subject") or "",
                "body": _clean_body(body, max_body),
                "source": source,
                "attachments": [a.get("name") or "file" for a in attachments],
                "inline_images": sum(1 for a in attachments if a.get("inlineImage")),
            })

        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        messages.sort(key=lambda m: m["time"] or epoch)

        last_customer = None
        for m in messages:
            m["auto_ack"] = False
            if m["role"] == "CALLER":
                last_customer = m["time"]
                continue
            quick = bool(
                last_customer and m["time"] and (m["time"] - last_customer).total_seconds() <= 120
            )
            if quick and _AUTO_ACK_RE.search(m["body"]):
                m["auto_ack"] = True

        agents = []
        queue_ids = []
        wrapups = []
        conv_start = None
        conv_end = None
        analytics_error = ""
        try:
            details = self._get(base, headers, "/api/v2/analytics/conversations/" + conv_id + "/details")
            conv_start = details.get("conversationStart")
            conv_end = details.get("conversationEnd")
            for p in details.get("participants") or []:
                uid = p.get("userId")
                if p.get("purpose") in ("agent", "user") and uid:
                    if not any(a["id"] == uid for a in agents):
                        agents.append({"id": uid, "name": p.get("participantName") or "", "email": ""})
                for s in p.get("sessions") or []:
                    for seg in s.get("segments") or []:
                        if seg.get("queueId") and seg["queueId"] not in queue_ids:
                            queue_ids.append(seg["queueId"])
                        if seg.get("wrapUpCode") and seg["wrapUpCode"] not in wrapups:
                            wrapups.append(seg["wrapUpCode"])
        except Exception as e:
            analytics_error = str(e)[:150]
        for a in agents:
            try:
                user = self._get(base, headers, "/api/v2/users/" + a["id"])
                a["name"] = a["name"] or user.get("name") or ""
                a["email"] = user.get("email") or ""
            except Exception:
                pass

        customer_msgs = [m for m in messages if m["role"] == "CALLER"]
        agent_msgs = [m for m in messages if m["role"] == "AGENT" and not m["auto_ack"]]
        auto_acks = sum(1 for m in messages if m["auto_ack"])
        first_customer = customer_msgs[0]["time"] if customer_msgs else None
        first_reply = None
        for m in agent_msgs:
            if m["time"] and (not first_customer or m["time"] >= first_customer):
                first_reply = m["time"]
                break
        first_response_min = None
        if first_customer and first_reply:
            first_response_min = round((first_reply - first_customer).total_seconds() / 60, 1)

        all_text = " ".join(m["subject"] + " " + m["body"] for m in messages)
        tickets = sorted(set(t.upper().replace(" ", "") for t in _TICKET_RE.findall(all_text)))
        screenshots = sum(m["inline_images"] for m in customer_msgs)
        times = [m["time"] for m in messages if m["time"]]
        start_dt = _parse_ts(conv_start) or (times[0] if times else None)
        end_dt = _parse_ts(conv_end) or (times[-1] if times else None)
        span = (end_dt - start_dt).total_seconds() if start_dt and end_dt else 0.0

        lines = []
        current_day = None
        for m in messages:
            day = _et(m["time"], "%Y-%m-%d")
            if day != current_day:
                lines.append("----- " + day + " (ET) -----")
                current_day = day
            role = "AGENT (AUTO-ACK)" if m["auto_ack"] else m["role"]
            extras = []
            if m["attachments"]:
                extras.append("[attachments: " + ", ".join(m["attachments"][:5]) + "]")
            if m["source"] == "preview":
                extras.append("[preview only - full text unavailable]")
            lines.append(
                "[" + _et(m["time"], "%H:%M") + "] " + role + ": " + str(m["from_name"])
                + " | Subject: " + m["subject"] + " | " + m["body"]
                + ((" " + " ".join(extras)) if extras else "")
            )
        thread = "\n".join(lines)
        limit = int(self.max_thread_chars or 0)
        if limit and len(thread) > limit:
            thread = thread[:limit] + "\n[NOTE: thread truncated]"

        agent_names = ", ".join(a["name"] for a in agents if a["name"])
        signals = [
            "[i] Messages: " + str(len(messages)) + " (" + str(len(customer_msgs)) + " from client, "
            + str(len(agent_msgs)) + " agent replies, " + str(auto_acks) + " automated acknowledgements)",
        ]
        if first_response_min is not None:
            signals.append("[i] First agent reply " + str(first_response_min) + " min after the first client email")
        elif customer_msgs:
            signals.append("[!] No non-automated agent reply found after the first client email")
        signals.append("[i] Ticket numbers in thread: " + (", ".join(tickets) or "none"))
        if screenshots:
            signals.append("[i] Client attached " + str(screenshots) + " inline image(s); content not visible to the auditor")
        if sources["preview"]:
            signals.append(
                "[!] " + str(sources["preview"]) + " message(s) available only as a short preview; "
                "do not penalise the agent for content you cannot see"
            )
        signals.append(
            "[i] Replies are sent from a shared mailbox; the author of each reply is not identified. "
            "Agents on this interaction: " + (agent_names or "unknown")
        )
        if analytics_error:
            signals.append("[!] Conversation details unavailable: " + analytics_error)

        metadata = [
            "CHANNEL: EMAIL",
            "Conversation ID: " + conv_id,
            "Agents on interaction: " + (agent_names or "unknown"),
            "Queue ID(s): " + (", ".join(queue_ids) or "unknown"),
            "Wrap-up code(s): " + (", ".join(wrapups) or "none"),
            "Subject: " + (messages[0]["subject"] if messages else ""),
            "First message (ET): " + _et(start_dt, "%Y-%m-%d %H:%M"),
            "Last message (ET): " + _et(end_dt, "%Y-%m-%d %H:%M"),
            "Thread span: " + str(round(span / 3600, 1)) + " hours",
        ]

        self._result = {
            "status": "DONE",
            "channel": "EMAIL",
            "conversation_id": conv_id,
            "call_start": start_dt.isoformat() if start_dt else None,
            "call_end": end_dt.isoformat() if end_dt else None,
            "agent_names": agent_names,
            "agent_emails": [a["email"] for a in agents if a["email"]],
            "queue_ids": queue_ids,
            "wrapup_code_ids": wrapups,
            "clean_transcript": thread,
            "turn_count": len(messages),
            "audio_duration_sec": round(span, 1),
            "total_hold_sec": 0,
            "hold_percentage": 0,
            "hold_event_count": 0,
            "total_dead_air_sec": 0,
            "agent_talk_ratio": "-",
            "overtalk_sec": 0,
            "hold_source": "n/a (email)",
            "model_used": "n/a (email)",
            "message_count": len(messages),
            "client_message_count": len(customer_msgs),
            "agent_reply_count": len(agent_msgs),
            "auto_ack_count": auto_acks,
            "first_response_min": first_response_min,
            "ticket_numbers": tickets,
            "client_screenshots": screenshots,
            "body_sources": sources,
            "metadata_text": "\n".join(metadata),
            "signals_text": "\n".join(signals),
        }
        return self._result

    # ---------------- outputs ----------------

    def build_email_payload(self) -> Message:
        try:
            r = self._run()
        except Exception as e:
            self._halt("mission_payload")
            return Message(text="EMAIL_FETCH_ERROR: " + str(e))
        return Message(text="\n".join([
            self.audit_instructions,
            "",
            "=== EMAIL METADATA ===",
            r["metadata_text"],
            "",
            "=== AUTOMATED SIGNALS (measured - treat as evidence) ===",
            r["signals_text"],
            "",
            "=== EMAIL THREAD (times HH:MM US Eastern) ===",
            r["clean_transcript"],
            "",
            "=== END OF INTERACTION ===",
            "Produce your audit now using the EMAIL rubric. Cite a time or ticket field for every justification.",
        ]))

    def build_email_structured(self) -> Data:
        try:
            return Data(data=self._run())
        except Exception as e:
            return Data(data={"error": str(e)})

    def build_email_report(self) -> Message:
        try:
            r = self._run()
        except Exception as e:
            return Message(text="EMAIL_FETCH_ERROR: " + str(e))
        return Message(text="\n".join([
            "EMAIL FETCH REPORT",
            r["metadata_text"],
            "Body sources: " + json.dumps(r["body_sources"]),
            "",
            r["signals_text"],
            "",
            r["clean_transcript"],
        ]))
