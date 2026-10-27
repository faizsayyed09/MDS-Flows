import os

import re

import json

import time

import tempfile

import requests

from lfx.custom.custom_component.component import Component

from lfx.io import (

 MessageTextInput,

 SecretStrInput,

 StrInput,

 MultilineInput,

 IntInput,

 BoolInput,

 DropdownInput,

 Output,

)

from lfx.schema.data import Data

from lfx.schema.message import Message

UA = (

 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "

 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

)

_TRANSIENT_WORDS = (

 "INTERNAL", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED",

 "overloaded", "Overloaded", "temporarily", "try again", "rate limit",

)

_TIMEOUT_CODES = (408, 504, 522, 524)

_MISSION_DEFAULT = "Use Mission LLM"

# ================= PHI protection (patient health information only) =================

PHI_STRONG_RE = re.compile(

 r"\b(?:MRN|medical record(?:\s+number)?|date of birth|D\.?O\.?B\.?|social security|SSN|"

 r"health plan|member (?:id|number)|encounter (?:number|id)|FIN number|patient (?:id|number|account))\b",

 re.IGNORECASE,

)

PHI_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

PHI_NAME_RE = re.compile(

 r"(?i:\bpatient(?:'s)?)(?:\s+(?i:name|named|called|is|was))?\s*[:\-]?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"

)

PHI_CLINICAL_RE = re.compile(

 r"\b(?:diagnos\w*|prescri\w*|medication\w*|dosage|lab results?|test results?|allerg\w*|surgery|"

 r"admitted|discharg\w*|treatment plan|insulin|chemo\w*|pregnan\w*|HIV|cancer|overdose|psychiatric)\b",

 re.IGNORECASE,

)

PHI_PATIENT_RE = re.compile(r"\bpatient'?s?\b", re.IGNORECASE)

PHI_NOT_NAMES = set("Care Safety Health Impact Impacting Portal Room Rooms Monitor Monitors Data Chart Charts Records".split())

LINE_PREFIX_RE = re.compile(r"^\s*\[\d{1,3}:\d{2}\]\s+(?:AGENT|CALLER|IVR):\s*")

PHI_MARKER = "[PHI REMOVED]"

def _phi_scan(s):

 hits = []

 if PHI_STRONG_RE.search(s):

  hits.append("patient_identifier")

 if PHI_SSN_RE.search(s):

  hits.append("ssn")

 for m in PHI_NAME_RE.finditer(s):

  if m.group(1).split()[0] not in PHI_NOT_NAMES:

   hits.append("patient_name")

   break

 if PHI_PATIENT_RE.search(s) and PHI_CLINICAL_RE.search(s):

  hits.append("patient_clinical_detail")

 return hits

def _phi_clean(text):

 found = []

 lines_out = []

 for line in str(text or "").split("\n"):

  parts = re.split(r"(?<=[.!?])\s+", line)

  new = []

  for i, part in enumerate(parts):

   hits = _phi_scan(part)

   if hits:

    found.extend(hits)

    prefix = LINE_PREFIX_RE.match(part) if i == 0 else None

    new.append((prefix.group(0) if prefix else "") + PHI_MARKER)

   else:

    new.append(part)

  lines_out.append(" ".join(new))

 return "\n".join(lines_out), found

# ================= helpers =================

def _state_path():

 base = os.environ.get("LANGFLOW_CONFIG_DIR") or ""

 if base and os.path.isdir(base) and os.access(base, os.W_OK):

  folder = os.path.join(base, "ceq_audit")

 else:

  folder = os.path.join(tempfile.gettempdir(), "ceq_audit")

 os.makedirs(folder, exist_ok=True)

 return os.path.join(folder, "llm_circuit.json")

def _load_circuit():

 try:

  with open(_state_path(), "r", encoding="utf-8") as f:

   return json.load(f)

 except Exception:

  return {}

def _save_circuit(state):

 path = _state_path()

 tmp = path + "." + str(os.getpid()) + ".tmp"

 try:

  with open(tmp, "w", encoding="utf-8") as f:

   json.dump(state, f)

  os.replace(tmp, path)

 except Exception:

  pass

def _parse_chain(text):

 chain = []

 for raw in re.split(r"[\n,]", text or ""):

  item = raw.strip()

  if not item:

   continue

  if item.lower().startswith("use mission"):

   item = _MISSION_DEFAULT

  elif " - " in item:

   item = item.split(" - ")[-1].strip()

  if item not in chain:

   chain.append(item)

 return chain or [_MISSION_DEFAULT]

def _extract_text(data):

 if not isinstance(data, dict) or data.get("status") != "success":

  return ""

 for message in data.get("output") or []:

  for block in message.get("content") or []:

   if block.get("type") == "output_text" and block.get("text"):

    return str(block["text"])

 return ""

def _usage(data):

 raw = (data or {}).get("usage_metadata") if isinstance(data, dict) else None

 if not isinstance(raw, dict):

  return None

 totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

 for op in raw.values():

  if isinstance(op, dict):

   totals["input_tokens"] += int(op.get("input_tokens") or 0)

   totals["output_tokens"] += int(op.get("output_tokens") or 0)

   totals["cost_usd"] += float(op.get("cost_usd") or 0.0)

 totals["cost_usd"] = round(totals["cost_usd"], 6)

 return totals

def _parse_json(text):

 text = (text or "").strip()

 fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)

 if fence:

  text = fence.group(1)

 start = text.find("{")

 end = text.rfind("}")

 if start == -1 or end <= start:

  return None

 try:

  obj = json.loads(text[start:end + 1])

 except ValueError:

  return None

 return obj if isinstance(obj, dict) else None

def _int(value, default):

 try:

  return int(value)

 except (TypeError, ValueError):

  return default

def _short(body):

 body = re.sub(r"<[^>]+>", " ", str(body or ""))

 body = re.sub(r"\s+", " ", body).strip()

 return body[:160]

class CustomComponent(Component):

 display_name = "MCH CX Scorer"

 description = (

  "Calls a Matcha mission with a cross-provider model chain, retries, timeout detection, "

  "JSON validation, circuit breaker, fallback JSON and built-in PHI protection."

 )

 inputs = [

  MessageTextInput(name="input_value", display_name="Input", required=True),

  StrInput(name="mission_id", display_name="Mission ID", required=True),

  SecretStrInput(name="api_key", display_name="Matcha API Key", required=True),

  StrInput(name="base_url", display_name="Matcha Base URL",

   value="https://matcha.harriscomputer.com", advanced=True),

  MultilineInput(name="model_chain", display_name="Model Fallback Chain", value="103\n84\n104",

   info="One model ID per line. 103 Gemini 3.1 Flash Lite, 93 Gemini 3 Flash, 111 Gemini 3.8 Flash, "

    "104 GPT-5.4 Nano, 101 GPT 4.1 Mini, 99 GPT-5.4, 84 Claude Haiku 4.5, 114 Claude Haiku 5.5, 108 Claude Sonnet 5."),

  DropdownInput(name="phi_mode", display_name="PHI Protection",

   options=["redact sentence", "block call (manual QA)", "off"], value="redact sentence",

   info="Removes sentences with patient identifiers or patient clinical details before sending to the LLM. "

    "Staff names and numbers are kept."),

  StrInput(name="required_keys", display_name="Required JSON Keys", value="",

   info="SNOW: match_status   KB: confidence   CEQ: attributes,verdict"),

  MultilineInput(name="fallback_json", display_name="Fallback JSON (optional)", value="",

   info="Returned if every model fails. Blank = raise an error."),

  BoolInput(name="require_json", display_name="Require Valid JSON", value=True),

  BoolInput(name="clean_json", display_name="Return Clean JSON", value=True),

  IntInput(name="retries_per_model", display_name="Retries per Model", value=1),

  IntInput(name="request_timeout_sec", display_name="Request Timeout (seconds)", value=180, advanced=True),

  IntInput(name="total_budget_sec", display_name="Total Time Budget (seconds)", value=450, advanced=True),

  IntInput(name="circuit_failures", display_name="Circuit Breaker: Failures to Trip", value=3, advanced=True),

  IntInput(name="circuit_minutes", display_name="Circuit Breaker: Skip Model For (minutes)", value=10, advanced=True),

  BoolInput(name="reset_circuit", display_name="Reset Circuit Breaker", value=False, advanced=True),

 ]

 outputs = [

  Output(display_name="Model Response", name="response", method="build_response", group_outputs=True),

  Output(display_name="Routing Log", name="routing_log", method="build_routing_log", group_outputs=True),

 ]

 # ---------------- circuit breaker ----------------

 def _is_open(self, state, model, now):

  entry = state.get(model) or {}

  return float(entry.get("open_until") or 0) > now

 def _record(self, model, success, now):

  state = _load_circuit()

  entry = state.get(model) or {"failures": 0, "open_until": 0}

  if success:

   entry = {"failures": 0, "open_until": 0, "last_ok": now}

  else:

   entry["failures"] = int(entry.get("failures") or 0) + 1

   entry["last_fail"] = now

   if entry["failures"] >= max(1, _int(self.circuit_failures, 3)):

    entry["open_until"] = now + max(1, _int(self.circuit_minutes, 10)) * 60

    entry["failures"] = 0

  state[model] = entry

  _save_circuit(state)

 # ---------------- single call ----------------

 def _call(self, mission, model, text):

  url = (self.base_url or "").rstrip("/") + "/rest/api/v1/completions"

  options = {"includeUsageMetadata": True}

  if model != _MISSION_DEFAULT:

   options["responseLLM"] = model

  payload = {"mission_id": mission, "messages": [{"role": "user", "content": text}], "options": options}

  headers = {

   "Accept": "application/json",

   "Content-Type": "application/json",

   "matcha-api-key": (self.api_key or "").strip(),

   "User-Agent": UA,

  }

  try:

   resp = requests.post(url, headers=headers, json=payload,

    timeout=(15, max(30, _int(self.request_timeout_sec, 180))))

  except requests.exceptions.Timeout as e:

   return "timeout", "", None, "request timed out: " + str(e)[:120]

  except requests.exceptions.RequestException as e:

   return "transient", "", None, "network: " + str(e)[:150]

  body = resp.text or ""

  try:

   data = resp.json()

  except ValueError:

   data = {}

  code = resp.status_code

  if code in (401, 403):

   return "fatal", "", None, "HTTP " + str(code) + ": " + _short(body)

  if code == 404 and "mission" in body.lower():

   return "fatal", "", None, "HTTP 404: " + _short(body)

  if code in _TIMEOUT_CODES:

   return "timeout", "", None, "HTTP " + str(code) + " gateway timeout"

  transient = code == 429 or code >= 500 or any(w in body[:800] for w in _TRANSIENT_WORDS)

  if not resp.ok:

   return ("transient" if transient else "error"), "", None, "HTTP " + str(code) + ": " + _short(body)

  output = _extract_text(data)

  usage = _usage(data)

  if not output:

   detail = str(data.get("debug_message") or data.get("error") or "empty response")

   return ("transient" if transient else "invalid"), "", usage, _short(detail)

  obj = _parse_json(output)

  if self.require_json and obj is None:

   return "invalid", output, usage, "response was not valid JSON"

  keys = [k.strip() for k in (self.required_keys or "").split(",") if k.strip()]

  missing = [k for k in keys if obj is None or k not in obj]

  if missing:

   return "invalid", output, usage, "missing JSON keys: " + ", ".join(missing)

  if self.clean_json and obj is not None:

   output = json.dumps(obj, ensure_ascii=False)

  return "ok", output, usage, ""

 # ---------------- routing ----------------

 def _route(self):

  if getattr(self, "_route_result", None) is not None:

   return self._route_result

  text = self.input_value

  if hasattr(text, "text"):

   text = text.text

  text = str(text or "").strip()

  mission = str(self.mission_id or "").split(" - ")[-1].strip()

  log = {

   "mission_id": mission,

   "input_chars": len(text),

   "phi_mode": str(self.phi_mode or "redact sentence"),

   "phi_hits": [],

   "phi_blocked": False,

   "attempts": [],

   "served_by": None,

   "fallback_used": False,

   "fallback_json_used": False,

   "total_cost_usd": 0.0,

   "error": None,

  }

  if not text:

   log["error"] = "Empty input"

   self._route_result = ("", log)

   return self._route_result

  if not mission:

   log["error"] = "Mission ID is blank"

   self._route_result = ("", log)

   return self._route_result

  if not log["phi_mode"].startswith("off"):

   text, found = _phi_clean(text)

   log["phi_hits"] = sorted(set(found))

   if found and log["phi_mode"].startswith("block"):

    log["phi_blocked"] = True

    log["error"] = "PHI_BLOCKED (" + ", ".join(log["phi_hits"]) + ") - route to manual QA"

    self._route_result = ("", log)

    return self._route_result

  if self.reset_circuit:

   _save_circuit({})

  chain = _parse_chain(self.model_chain)

  state = _load_circuit()

  now = time.time()

  available = [m for m in chain if not self._is_open(state, m, now)]

  skipped = [m for m in chain if m not in available]

  if skipped:

   log["skipped_open_circuit"] = skipped

  if not available:

   available = list(chain)

  deadline = time.time() + max(60, _int(self.total_budget_sec, 450))

  retries = max(0, _int(self.retries_per_model, 1))

  best_invalid = ""

  for index, model in enumerate(available):

   for attempt in range(retries + 1):

    if time.time() > deadline:

     break

    started = time.time()

    kind, output, usage, detail = self._call(mission, model, text)

    cost = (usage or {}).get("cost_usd") or 0.0

    log["total_cost_usd"] = round(log["total_cost_usd"] + cost, 6)

    log["attempts"].append({

     "model": model,

     "attempt": attempt + 1,

     "result": kind,

     "seconds": round(time.time() - started, 1),

     "cost_usd": cost,

     "tokens_in": (usage or {}).get("input_tokens"),

     "detail": detail,

    })

    if kind == "ok":

     self._record(model, True, time.time())

     log["served_by"] = model

     log["fallback_used"] = index > 0

     self._route_result = (output, log)

     return self._route_result

    if kind == "fatal":

     log["error"] = detail

     self._route_result = ("", log)

     return self._route_result

    if kind in ("invalid", "timeout"):

     if kind == "invalid":

      best_invalid = best_invalid or output

     break

    if kind == "transient":

     self._record(model, False, time.time())

     if attempt < retries:

      time.sleep(3 * (attempt + 1))

      continue

    break

  summary = []

  for a in log["attempts"]:

   summary.append(a["model"] + "#" + str(a["attempt"]) + " " + a["result"] + " " + str(a["seconds"]) + "s"

    + ((" (" + a["detail"] + ")") if a["detail"] else ""))

  log["error"] = "All models failed: " + "; ".join(summary)

  fallback = (self.fallback_json or "").strip()

  if not best_invalid and fallback:

   obj = _parse_json(fallback)

   if obj is not None:

    obj["mission_error"] = log["error"][:500]

    log["fallback_json_used"] = True

    self._route_result = (json.dumps(obj, ensure_ascii=False), log)

    return self._route_result

  self._route_result = (best_invalid, log)

  return self._route_result

 # ---------------- outputs ----------------

 def build_response(self) -> Message:

  output, log = self._route()

  phi = (" | PHI removed: " + ", ".join(log["phi_hits"])) if log.get("phi_hits") else ""

  if log.get("phi_blocked"):

   self.status = str(log.get("error"))

   try:

    self.stop("response")

   except Exception:

    pass

   return Message(text="PHI_BLOCKED")

  if log.get("served_by"):

   self.status = (

    "Served by " + str(log["served_by"]) + (" (fallback)" if log["fallback_used"] else "")

    + " | attempts " + str(len(log["attempts"])) + " | $" + str(log["total_cost_usd"]) + phi

   )

   return Message(text=output)

  if log.get("fallback_json_used"):

   self.status = "FALLBACK JSON used | " + str(log.get("error"))[:300] + phi

   return Message(text=output)

  self.status = "FAILED: " + str(log.get("error"))

  if output:

   return Message(text=output)

  raise ValueError("Resilient Mission Caller failed. " + str(log.get("error")))

 def build_routing_log(self) -> Data:

  _, log = self._route()

  return Data(data=log)
