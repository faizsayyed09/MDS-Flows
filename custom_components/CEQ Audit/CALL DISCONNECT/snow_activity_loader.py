import io

import re

from collections import Counter

from datetime import datetime

import pandas as pd

import requests

from lfx.custom.custom_component.component import Component

from lfx.io import BoolInput, DropdownInput, IntInput, MultilineInput, Output, SecretStrInput, StrInput

from lfx.schema.data import Data

SKIP_STATUS = {"deleted", "archived", "error", "failed"}

JOURNAL_HEADER = re.compile(

 r"^[ \t]*(?P<ts>\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}[ T]+\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*[AaPp]\.?[Mm]\.?)?)"

 r"[ \t]+-[ \t]+(?P<who>[^\n]*?)[ \t]*\((?P<kind>[^()\n]+)\)[ \t]*$",

 re.M,

)

INBOUND = re.compile(r"\b(reply from|received from)\s*:", re.I)

SYSTEM_AUTHORS = {"system", "guest", "admin", "system administrator", "integration user", "mid server"}

COLUMNS = {

 "number": (["number"], False),

 "type": (["type", "tasktype", "class"], False),

 "opened": (["opened", "openedat", "created", "createdon"], False),

 "opened_by": (["openedby", "createdby"], False),

 "opened_by_email": (["openedbyemail"], False),

 "updated": (["updateduupdatedon", "updated", "updatedon"], False),

 "company": (["company"], False),

 "short_desc": (["shortdescription"], False),

 "contact_type": (["contacttype"], False),

 "work_notes": (["worknotes"], False),

 "comments": (["additionalcomments", "comments"], False),

 "parent": (["parent"], False),

 "requested_for": (["requestedfor"], True),

 "requestor": (["requestor"], True),

 "state": (["state"], False),

 "assignment_group": (["assignmentgroup"], False),

 "assigned_to": (["assignedto"], False),

 "closed_by": (["closedby"], False),

 "closed": (["closed", "closedat"], False),

 "contact_incident": (["contactincident"], False),

 "contact_case": (["contactcase"], False),

 "contact": (["contact"], False),

}

def _key(s):

 return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

def _norm(s):

 s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())

 return re.sub(r"\s+", " ", s).strip()

def _fl(name):

 t = _norm(name).split()

 if len(t) >= 2:

  return t[0] + " " + t[-1]

 return t[0] if t else ""

def _text(v):

 if v is None:

  return ""

 try:

  if pd.isna(v):

   return ""

 except (TypeError, ValueError):

  pass

 if isinstance(v, (pd.Timestamp, datetime)):

  return v.strftime("%Y-%m-%d %H:%M:%S")

 return str(v).strip()

class CustomComponent(Component):

 display_name = "SNOW Activity Loader"

 description = (

  "Loads a ServiceNow task export (.xlsx/.csv) from a Matcha folder and splits Work notes, "

  "Additional comments, Opened and Closed into one timed row per agent action."

 )

 icon = "table"

 inputs = [

  StrInput(name="matcha_base", display_name="Matcha API Base", value="https://matcha.harriscomputer.com/rest/api/v1"),

  SecretStrInput(name="matcha_key", display_name="Matcha API Key"),

  IntInput(name="folder_id", display_name="SNOW Export Folder ID"),

  IntInput(name="file_id", display_name="Specific File ID (0 = newest)", value=0, advanced=True),

  MultilineInput(

   name="agent_filter",

   display_name="Agents (names or emails, optional)",

   value="",

   info="Keep only these agents' actions, e.g. Neha Santosh Zende. Blank = all agents.",

  ),

  StrInput(

   name="source_tz",

   display_name="Export Time Zone",

   value="America/New_York",

   info="Time zone of the ServiceNow account that exported the file.",

  ),

  DropdownInput(

   name="date_order",

   display_name="Date Order in Export",

   options=["auto", "month first (MM/DD)", "day first (DD/MM)"],

   value="auto",

  ),

  MultilineInput(

   name="name_email_map",

   display_name="Name = Email Map (optional)",

   value="",

   advanced=True,

   info="One per line, e.g. Neha Santosh Zende = neha.zende@company.com",

  ),

  BoolInput(name="include_closed", display_name="Include Ticket Closures", value=True, advanced=True),

 ]

 outputs = [Output(display_name="SNOW Activity", name="snow", method="load")]

 # ---------- Matcha ----------

 def _headers(self, accept_json=True):

  key = (self.matcha_key or "").strip()

  if not key:

   raise ValueError("Matcha API Key is empty. Map it to the MATCHA_API_KEY global variable.")

  h = {"MATCHA-API-KEY": key}

  if accept_json:

   h["Accept"] = "application/json"

  return h

 def _list_files(self, base):

  r = requests.get(

   base + "/files",

   headers=self._headers(),

   params={"folder_id": int(self.folder_id or 0), "select": "id,filename,status"},

   timeout=60,

  )

  if r.status_code in (401, 403):

   raise PermissionError("Matcha HTTP " + str(r.status_code) + ": check the API key scope and IP whitelist. " + r.text[:200])

  r.raise_for_status()

  body = r.json()

  if isinstance(body, dict):

   body = body.get("data") or body.get("files") or body.get("results") or body.get("entities") or []

  out = []

  for f in body:

   if not isinstance(f, dict) or f.get("id") is None:

    continue

   if str(f.get("status") or "").strip().lower() in SKIP_STATUS:

    continue

   out.append(f)

  return out

 def _download(self, base, file_id):

  r = requests.get(base + "/file/" + str(file_id), headers=self._headers(accept_json=False), timeout=180)

  r.raise_for_status()

  ctype = (r.headers.get("Content-Type") or "").lower()

  if "json" in ctype or "html" in ctype:

   raise ValueError("Matcha returned " + ctype + " instead of the file: " + r.text[:200])

  return r.content

 @staticmethod

 def _read(content, filename):

  name = (filename or "").lower()

  if content[:2] == b"PK" or name.endswith(".xlsx"):

   return pd.read_excel(io.BytesIO(content), engine="openpyxl", dtype=object)

  if content[:4] == b"\xd0\xcf\x11\xe0" or name.endswith(".xls"):

   return pd.read_excel(io.BytesIO(content), dtype=object)

  for enc in ("utf-8-sig", "latin-1"):

   try:

    return pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str, keep_default_na=False)

   except UnicodeDecodeError:

    continue

  raise ValueError("Could not read " + str(filename) + " as Excel or CSV.")

 @staticmethod

 def _columns(df):

  keys = {}

  for c in df.columns:

   keys.setdefault(_key(c), c)

  found = {}

  for field, (cands, prefix) in COLUMNS.items():

   col = None

   for c in cands:

    if c in keys:

     col = keys[c]

     break

   if col is None and prefix:

    for k, c in keys.items():

     if any(k.startswith(x) for x in cands):

      col = c

      break

   if col is not None:

    found[field] = col

  return found

 # ---------- time ----------

 @staticmethod

 def _dayfirst(samples, setting):

  if setting.startswith("day"):

   return True

  if setting.startswith("month"):

   return False

  for s in samples:

   m = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", str(s))

   if m:

    a, b = int(m.group(1)), int(m.group(2))

    if a > 12:

     return True

    if b > 12:

     return False

  return False

 @staticmethod

 def _to_local(raw, dayfirst, tz):

  try:

   if raw is None or str(raw).strip() in ("", "NaT", "nan"):

    return None

   if isinstance(raw, (pd.Timestamp, datetime)):

    t = pd.Timestamp(raw)

   elif isinstance(raw, (int, float)) and 20000 < float(raw) < 80000:

    t = pd.Timestamp("1899-12-30") + pd.to_timedelta(float(raw), unit="D")

   else:

    s = re.sub(r"(?i)\b([ap])\.m\.", r"\1m", str(raw).strip())

    use_dayfirst = dayfirst and not re.match(r"^\d{4}", s)

    t = pd.to_datetime(s, errors="coerce", dayfirst=use_dayfirst)

   if t is None or pd.isna(t):

    return None

   if t.tzinfo is None:

    t = t.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")

   if pd.isna(t):

    return None

   return t

  except (ValueError, TypeError, OverflowError):

   return None

 # ---------- main ----------

 def load(self) -> Data:

  base = (self.matcha_base or "").rstrip("/")

  files = self._list_files(base)

  if not files:

   raise ValueError("No usable files in Matcha folder " + str(self.folder_id) + ".")

  wanted_id = int(self.file_id or 0)

  if wanted_id:

   pick = next((f for f in files if int(f["id"]) == wanted_id), None)

   if not pick:

    raise ValueError("File ID " + str(wanted_id) + " not found in folder " + str(self.folder_id) + ".")

  else:

   pick = max(files, key=lambda f: int(f["id"]))

  filename = str(pick.get("filename") or "")

  df = self._read(self._download(base, pick["id"]), filename)

  df.columns = [str(c).strip() for c in df.columns]

  cols = self._columns(df)

  if "number" not in cols:

   raise ValueError("Column 'Number' not found in " + filename + ". Columns: " + ", ".join(df.columns)[:500])

  if not any(k in cols for k in ("work_notes", "comments", "opened")):

   raise ValueError("None of Work notes / Additional comments / Opened found in " + filename + ".")

  tz = (self.source_tz or "America/New_York").strip()

  # ---- name -> email (learned from Opened by / Opened by.Email, plus manual map) ----

  name_email = {}

  def learn(name, email, force=False):

   n = _norm(name)

   e = str(email or "").strip().lower().replace("mailto:", "")

   if not n or "@" not in e:

    return

   for k in (n, "fl:" + _fl(n)):

    if force:

     name_email[k] = e

    else:

     name_email.setdefault(k, e)

  if "opened_by" in cols and "opened_by_email" in cols:

   for a, b in zip(df[cols["opened_by"]], df[cols["opened_by_email"]]):

    learn(_text(a), _text(b))

  for line in re.split(r"[\n;]+", self.name_email_map or ""):

   if "=" in line:

    n, e = line.rsplit("=", 1)

    learn(n, e, force=True)

  def resolve(name, email):

   e = str(email or "").strip().lower()

   if "@" in e:

    return e

   return name_email.get(_norm(name)) or name_email.get("fl:" + _fl(name)) or ""

  tokens = [t.strip() for t in re.split(r"[,;\n]+", self.agent_filter or "") if t.strip()]

  f_emails = {t.lower() for t in tokens if "@" in t}

  f_names = {_fl(t) for t in tokens if "@" not in t and _fl(t)}

  def wanted(name, email):

   if not tokens:

    return True

   if email and email in f_emails:

    return True

   return _fl(name) in f_names

  def get(rec, field):

   if field not in cols:

    return None

   return rec.get(cols[field])

  # ---- pass 1: collect raw events ----

  events = []

  truncated = 0

  no_header = 0

  for rec in df.to_dict(orient="records"):

   ticket = _text(get(rec, "number")).upper()

   if not ticket:

    continue

   caller = ""

   for f in ("contact_incident", "contact_case", "contact", "requested_for", "requestor"):

    caller = _text(get(rec, f))

    if caller:

     break

   info = {

    "Ticket Number": ticket,

    "Ticket Type": _text(get(rec, "type")),

    "Parent": _text(get(rec, "parent")).upper(),

    "Company": _text(get(rec, "company")),

    "Short description": _text(get(rec, "short_desc"))[:150],

    "Contact type": _text(get(rec, "contact_type")),

    "State": _text(get(rec, "state")),

    "Assignment group": _text(get(rec, "assignment_group")),

    "Assigned to": _text(get(rec, "assigned_to")),

    "Caller": caller,

    "Last Updated": _text(get(rec, "updated")),

   }

   opened = get(rec, "opened")

   opened_by = _text(get(rec, "opened_by"))

   if _text(opened) and opened_by:

    events.append((opened, "TICKET_OPENED", opened_by, _text(get(rec, "opened_by_email")), "", info))

   for field, default in (("work_notes", "WORK_NOTE"), ("comments", "ADDITIONAL_COMMENT")):

    text = _text(get(rec, field))

    if not text:

     continue

    if len(text) >= 32000:

     truncated += 1

    heads = list(JOURNAL_HEADER.finditer(text))

    if not heads:

     no_header += 1

     continue

    for i, m in enumerate(heads):

     stop = heads[i + 1].start() if i + 1 < len(heads) else len(text)

     body = text[m.end():stop]

     kind = m.group("kind").lower()

     if "work" in kind:

      typ = "WORK_NOTE"

     elif "comment" in kind:

      typ = "ADDITIONAL_COMMENT"

     else:

      typ = default

     if typ == "ADDITIONAL_COMMENT" and INBOUND.search(body[:400]):

      typ = "EMAIL_RECEIVED"

     events.append((m.group("ts"), typ, m.group("who").strip(), "", "", info))

   closed = get(rec, "closed")

   closed_by = _text(get(rec, "closed_by"))

   if self.include_closed and _text(closed) and closed_by:

    events.append((closed, "FIELD_UPDATE", closed_by, "", "closed", info))

  if not events:

   raise ValueError(

    "No timed actions found in " + filename + ". Check that Work notes / Additional comments "

    "cells contain 'date time - Name (Work notes)' headers."

   )

  samples = [e[0] for e in events if isinstance(e[0], str)]

  dayfirst = self._dayfirst(samples, self.date_order or "auto")

  # ---- pass 2: parse time, resolve email, filter, de-duplicate ----

  rows = []

  seen = set()

  authors = Counter()

  unresolved = Counter()

  dropped_time = 0

  for raw_ts, typ, who, email, field, info in events:

   if _norm(who) in SYSTEM_AUTHORS and typ != "EMAIL_RECEIVED":

    continue

   t = self._to_local(raw_ts, dayfirst, tz)

   if t is None:

    dropped_time += 1

    continue

   email = resolve(who, email)

   if not wanted(who, email):

    continue

   utc = t.tz_convert("UTC")

   k = (info["Ticket Number"], typ, utc.isoformat(), _fl(who))

   if k in seen:

    continue

   seen.add(k)

   if typ != "EMAIL_RECEIVED":

    authors[who] += 1

    if not email:

     unresolved[who] += 1

   row = dict(info)

   row.update({

    "Activity Time": t.strftime("%m/%d/%Y %H:%M:%S"),

    "Agent": who,

    "Agent Email": email,

    "Activity Type": typ,

    "Field Changed": field,

    "Caller Phone": "",

    "Email Subject": "",

    "activity_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),

    "agent_email_norm": email,

    "agent_name_norm": _norm(who),

   })

   rows.append(row)

  rows.sort(key=lambda r: r["activity_utc"])

  types = Counter(r["Activity Type"] for r in rows)

  span = ""

  if rows:

   span = rows[0]["activity_utc"] + " to " + rows[-1]["activity_utc"] + " UTC"

  warnings = []

  if truncated:

   warnings.append(str(truncated) + " journal cells hit the Excel 32,767-character limit (oldest entries lost)")

  if no_header:

   warnings.append(str(no_header) + " journal cells had no 'date - Name (Work notes)' headers and were skipped")

  if dropped_time:

   warnings.append(str(dropped_time) + " actions with unreadable time dropped")

  if tokens and not rows:

   warnings.append("No actions found for: " + ", ".join(tokens) + ". Check the name spelling used in ServiceNow.")

  if unresolved:

   warnings.append("No email for " + ", ".join(list(unresolved)[:5]) + " (matched to Genesys by name instead)")

  self.status = (

   "Loaded " + str(len(rows)) + " actions from " + str(len(df)) + " tickets in " + filename

   + " | " + ", ".join(k + " " + str(v) for k, v in sorted(types.items()))

   + (" | " + span if span else "")

   + " | date order " + ("DD/MM" if dayfirst else "MM/DD")

   + (" | " + "; ".join(warnings) if warnings else "")

  )

  return Data(data={

   "rows": rows,

   "source_file": filename,

   "file_id": pick["id"],

   "row_count": len(rows),

   "tickets_in_file": len(df),

   "activity_span_utc": span,

   "activity_types": dict(types),

   "agents_found": [{"name": n, "actions": c, "email": resolve(n, "")} for n, c in authors.most_common()],

   "date_order": "DD/MM" if dayfirst else "MM/DD",

   "warnings": warnings,

  })
