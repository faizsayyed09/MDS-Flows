import re
import json

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, DataInput, MultilineInput, Output
from lfx.schema.message import Message


ADVISORY_KEYS = (
 "constraints",
 "escalationPath",
 "expectedResolution",
 "troubleshootingSteps",
 "verificationRequirements",
)

DEFAULT_TEMPLATE = (
 "=== TICKET MATCH ===\n{ticket_json}\n\n"
 "=== EXPECTED PROCEDURE ===\n{kb_json}\n\n"
 "{facts}\n\n"
 "=== INTERACTION ===\n{call_payload}"
)

LINE_RE = re.compile(r"^\[(\d{1,3}):(\d{2})\]\s+(AGENT|CALLER|IVR):\s*(.*)$")
AGENT_LINE_RE = re.compile(r"^Agent:\s*(.+)$", re.MULTILINE)
THANK_RE = re.compile(r"thank(?:s| you) for (?:calling|contacting|reaching)", re.IGNORECASE)
ASK_NAME_RE = re.compile(
 r"(?:first|full)\s+(?:and\s+(?:the\s+|your\s+)?last\s+)?name|last name|your name|who am i speaking",
 re.IGNORECASE,
)
ASK_CONTACT_RE = re.compile(r"call\s?back|extension|phone number|contact number|best number|cell", re.IGNORECASE)
OFFER_RE = re.compile(r"\b(?:how|can|may|what)\b.{0,25}\b(?:help|assist)", re.IGNORECASE)
HOLD_TAG = "[during hold"


def _text(v):
 if hasattr(v, "text"):
  v = v.text
 return str(v or "")


def _json(raw):
 s, e = raw.find("{"), raw.rfind("}")
 if s == -1 or e <= s:
  return {}
 try:
  return json.loads(raw[s:e + 1])
 except ValueError:
  return {}


def _data(v):
 if isinstance(v, list) and v:
  v = v[0]
 if hasattr(v, "data"):
  v = v.data or {}
 return v if isinstance(v, dict) else {}


def _mmss(sec):
 m, s = divmod(int(float(sec)), 60)
 return "%02d:%02d" % (m, s)


def _clip(text, limit):
 text = str(text or "").strip()
 return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def _filter_kb(kb):
 if not kb:
  return {}
 kb = dict(kb)
 conf = str(kb.get("confidence") or "").strip().lower()
 if conf == "high" and not kb.get("kb_gap"):
  kb["enforceable"] = True
  return kb
 advisory = {k: kb.pop(k) for k in ADVISORY_KEYS if kb.get(k)}
 kb["enforceable"] = False
 if advisory:
  kb["advisory_only"] = advisory
 return kb


def _promote_kb(kb, ticket):
 if not kb or kb.get("enforceable") or kb.get("kb_gap"):
  return kb
 facts = (ticket.get("verification") or {}).get("facts") or {}
 attached = set(str(k).upper() for k in facts.get("kb_articles_in_ticket") or [])
 returned = set()
 for a in kb.get("relevantArticles") or []:
  if isinstance(a, dict) and a.get("id"):
   returned.add(str(a.get("id")).upper())
 hit = sorted(attached.intersection(returned))
 if not hit:
  return kb
 kb = dict(kb)
 kb.update(kb.pop("advisory_only", None) or {})
 kb["enforceable"] = True
 kb["enforceable_reason"] = "agent cited " + ", ".join(hit) + " in the ticket"
 return kb


def _parse_lines(text):
 out = []
 for raw in str(text or "").splitlines():
  m = LINE_RE.match(raw.strip())
  if m:
   body = m.group(4).replace("(?)", "").strip()
   out.append((m.group(3), int(m.group(1)) * 60 + int(m.group(2)), body))
 return out


def _first(turns, rx, after=-1):
 for s, t in turns:
  if s >= after and rx.search(t):
   return s
 return None


def _opening_facts(lines, agent_names, window=150):
 agent = [(s, t) for sp, s, t in lines if sp == "AGENT" and s <= window]
 caller = [(s, t) for sp, s, t in lines if sp == "CALLER" and s <= window]
 if not agent:
  return {}
 firsts = [n.split()[0].lower() for n in agent_names if n.split()]
 name_at = None
 for s, t in agent[:3]:
  low = t.lower()
  if any(f in low for f in firsts):
   name_at = s
   break
 asked = _first(agent, ASK_NAME_RE)
 gave = None
 if asked is not None:
  for s, t in caller:
   if s >= asked and len(t.strip()) >= 3:
    gave = s
    break
 return {
  "thanked": _first(agent, THANK_RE),
  "agent_name": name_at,
  "asked_name": asked,
  "gave_name": gave,
  "asked_contact": _first(agent, ASK_CONTACT_RE),
  "offered_help": _first(agent, OFFER_RE),
 }


def _silence_facts(call, lines, pauses):
 out = []
 agent = [(s, t) for sp, s, t in lines if sp == "AGENT" and HOLD_TAG not in t]
 for h in call.get("hold_events") or []:
  try:
   st = float(h.get("start"))
   en = float(h.get("end"))
   dur = float(h.get("duration"))
  except (TypeError, ValueError):
   continue
  gap = float(h.get("longest_without_update_sec") or dur)
  if gap < 120:
   rule = "longest stretch without an agent update " + str(int(round(gap))) + "s, within the 2-minute on-hold update rule"
  else:
   rule = "caller went " + str(int(round(gap))) + "s without an agent update, exceeding the 2-minute on-hold update rule"
  out.append(
   "Genesys hold " + _mmss(st) + "-" + _mmss(en) + " (" + str(int(round(dur))) + "s): "
   + ("announced" if h.get("announced_phrase") else "NOT announced") + ", "
   + ("caller thanked on return" if h.get("acknowledged_phrase") else "no thanks on return")
   + "; " + rule + "."
  )
 skip = set(str(p.get("silence_start") or "") for p in pauses or [])
 for ev in call.get("long_silence_events") or []:
  try:
   st = float(ev.get("start"))
   en = float(ev.get("end"))
   dur = float(ev.get("duration"))
  except (TypeError, ValueError):
   continue
  if _mmss(st) in skip:
   continue
  before = [(s, t) for s, t in agent if s <= st and len(t.split()) >= 3]
  last = "none"
  if before:
   last = "[" + _mmss(before[-1][0]) + "] '" + _clip(before[-1][1], 80) + "'"
  out.append(
   "Agent-side silence, not on hold, " + _mmss(st) + "-" + _mmss(en) + ": " + str(int(round(dur)))
   + "s measured. Last meaningful agent statement before it: " + last
   + ". Use these exact figures; the dead-air update rule is about every 30 seconds."
  )
 return out


class CustomComponent(Component):
 display_name = "Audit Payload Builder"
 description = "Builds the CEQ payload with a VERIFIED FACTS block (ticket, KB, opening, holds, silences)."

 inputs = [
  MultilineInput(name="template", display_name="Template", value=DEFAULT_TEMPLATE),
  MessageTextInput(name="ticket_json", display_name="Ticket JSON (from Gate)", required=True),
  MessageTextInput(name="kb_response", display_name="KB Response", required=False),
  MessageTextInput(name="call_payload", display_name="Call Payload", required=True),
  DataInput(name="prematch", display_name="Pre-Match Data", required=False),
  DataInput(name="call_data", display_name="Call Data", required=False,
   info="Structured Data output of the QA Transcriber (hold events and silences)."),
 ]

 outputs = [
  Output(display_name="CEQ Payload", name="payload", method="build_payload", group_outputs=True),
  Output(display_name="KB JSON (filtered)", name="kb_json", method="build_kb", group_outputs=True),
 ]

 def _kb(self):
  kb = _filter_kb(_json(_text(self.kb_response)))
  ticket = _json(_text(self.ticket_json))
  return _promote_kb(kb, ticket)

 def _facts(self, ticket, kb, pm, call, lines, call_text):
  out = ["=== VERIFIED FACTS (computed by code from Genesys and ServiceNow; treat as true) ==="]
  v = ticket.get("verification") or {}
  f = v.get("facts") or {}
  num = ticket.get("matched_ticket")
  if num:
   out.append(
    "- ServiceNow record for this interaction: " + num + " ("
    + str(ticket.get("match_confidence")) + "; " + str(v.get("basis", "")) + ")."
   )
   out.append("- Ticket timing was checked by code in US Eastern and is consistent with this interaction. Never raise ticket timing as an error.")
  else:
   out.append(
    "- No ServiceNow record could be verified. Ticket documentation is unverifiable: "
    "case_documentation = NA with items listed in unverified. Do not raise 'no ticket created'."
   )
  given = pm.get("tickets_mentioned") or []
  if given:
   out.append("- Ticket/case numbers stated to the caller during the interaction: " + ", ".join(given) + ".")
  if f.get("caller_phone_in_ticket") is True:
   out.append("- The caller phone given in the interaction appears in the ticket.")
  elif f.get("caller_phone_in_ticket") is False:
   out.append(
    "- The caller phone given in the interaction (" + ", ".join(pm.get("caller_phones") or [])
    + ") does NOT appear in the exported ticket text."
   )
  if f.get("kb_articles_in_ticket"):
   out.append("- KB articles cited in the ticket by the agent: " + ", ".join(f["kb_articles_in_ticket"]) + ".")
  for p in pm.get("caller_requested_pauses") or []:
   out.append(
    "- At " + str(p.get("requested_at")) + " the caller asked the agent to wait ('" + str(p.get("caller_said"))
    + "'). The " + str(p.get("silence_sec")) + "s silence from " + str(p.get("silence_start"))
    + " was caller-initiated, not agent dead air; do not fail hold_wait for it."
   )

  names = str(call.get("agent_names") or "")
  if not names:
   m = AGENT_LINE_RE.search(call_text)
   names = m.group(1) if m else ""
  agent_names = [n.strip() for n in names.split(",") if n.strip()]
  op = _opening_facts(lines, agent_names)
  if op:
   labels = (
    ("thanked", "thanked the caller"),
    ("agent_name", "agent's own name heard"),
    ("asked_name", "asked for caller's first and last name"),
    ("gave_name", "caller gave name"),
    ("asked_contact", "asked for callback number/extension"),
    ("offered_help", "offered assistance"),
   )
   parts = []
   for key, label in labels:
    at = op.get(key)
    parts.append(label + ((" at " + _mmss(at)) if at is not None else ": not detected"))
   out.append("- Opening (detected by code): " + "; ".join(parts) + ".")
   out.append(
    "- Asking for a callback number satisfies 'verifies or updates contact information'. "
    "If the agent's own name is not detected but the greeting is garbled or marked (?), "
    "'states own name' is unverified, never missed."
   )
  for s in _silence_facts(call, lines, pm.get("caller_requested_pauses")):
   out.append("- " + s)
  out.append("- Caller lines marked [during hold - not agent-facing] were spoken while on hold; never score the agent on them.")

  if not kb:
   out.append("- No KB procedure is available: procedure_adherence = NO_KB.")
  elif kb.get("enforceable") is True:
   reason = kb.get("enforceable_reason") or "High confidence KB match"
   out.append("- EXPECTED PROCEDURE is enforceable (" + reason + ").")
  else:
   out.append(
    "- EXPECTED PROCEDURE is advisory only (KB confidence " + str(kb.get("confidence") or "unknown")
    + "): use it for coaching only, never as the basis for NOT_MET or a critical error."
   )
  return "\n".join(out)

 def build_payload(self) -> Message:
  ticket = _json(_text(self.ticket_json))
  kb = self._kb()
  pm = _data(self.prematch) if self.prematch else {}
  call = _data(self.call_data) if self.call_data else {}
  call_text = _text(self.call_payload)
  lines = _parse_lines(call.get("clean_transcript") or call_text)
  facts = self._facts(ticket, kb, pm, call, lines, call_text)
  tpl = _text(self.template)
  warn = ""
  if "{call_payload}" not in tpl or "{ticket_json}" not in tpl:
   warn = " | WARNING template missing placeholders - default used"
   tpl = DEFAULT_TEMPLATE
  if "{facts}" not in tpl:
   tpl = "{facts}\n\n" + tpl
  out = (
   tpl.replace("{facts}", facts)
   .replace("{ticket_json}", json.dumps(ticket, ensure_ascii=False))
   .replace("{kb_json}", json.dumps(kb, ensure_ascii=False) if kb else "{}")
   .replace("{call_payload}", call_text)
  )
  self.status = (
   "ticket " + (ticket.get("matched_ticket") or "NONE")
   + " | kb " + (str(kb.get("confidence")) if kb else "NONE")
   + (" enforceable" if kb.get("enforceable") else "")
   + " | prematch " + (str(pm.get("status")) if pm else "NOT CONNECTED")
   + " | call_data " + ("ok" if call else "NOT CONNECTED")
   + " | call " + str(len(call_text)) + " chars" + warn
  )
  return Message(text=out)

 def build_kb(self) -> Message:
  return Message(text=json.dumps(self._kb(), ensure_ascii=False))
