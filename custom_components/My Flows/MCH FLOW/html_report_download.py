import re

import html as htmlmod

import base64

import asyncio

import urllib.parse

from pathlib import Path

from datetime import datetime, timedelta

import requests

from lfx.custom.custom_component.component import Component

from lfx.io import (

 MessageTextInput,

 DataInput,

 StrInput,

 SecretStrInput,

 DropdownInput,

 IntInput,

 BoolInput,

 Output,

)

from lfx.schema.data import Data

from lfx.schema.message import Message

STAMP_RE = re.compile(r"(\d{8}_\d{6})\.html$")

def _text(v):

 if hasattr(v, "text"):

  v = v.text

 return str(v or "")

def _data(v):

 if isinstance(v, list) and v:

  v = v[0]

 if hasattr(v, "data"):

  v = v.data or {}

 return v if isinstance(v, dict) else {}

def _safe(s, limit=40):

 s = re.sub(r"[^A-Za-z0-9]+", "_", str(s or "")).strip("_")

 return s[:limit]

def _int(value, default):

 try:

  return int(value)

 except (TypeError, ValueError):

  return default

def _expired(name, keep_days):

 m = STAMP_RE.search(name)

 if not m or keep_days <= 0:

  return False

 try:

  made = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")

 except ValueError:

  return False

 return datetime.now() - made > timedelta(days=keep_days)

def _wrap(body, title):

 head = body.lstrip()[:200].lower()

 if head.startswith("<!doctype") or head.startswith("<html"):

  return body

 return (

  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"

  "<meta charset=\"utf-8\">\n"

  "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"

  "<title>" + htmlmod.escape(title) + "</title>\n"

  "<style>"

  "body{font-family:Segoe UI,Arial,sans-serif;margin:24px auto;max-width:900px;color:#222;padding:0 16px}"

  "table{border-collapse:collapse}"

  "@media print{body{margin:0}a{text-decoration:none;color:#222}}"

  "</style>\n</head>\n<body>\n" + body + "\n</body>\n</html>\n"

 )

class CustomComponent(Component):

 display_name = "HTML Report Download"

 description = "Saves the audit HTML as a complete page in the flow file store and returns a download link."

 icon = "file-text"

 name = "HtmlReportDownload"

 inputs = [

  MessageTextInput(name="html_content", display_name="HTML Content", required=True,

   info="Connect Audit Result Parser > Email Preview."),

  DataInput(name="call_data", display_name="Call Data (optional)", required=False,

   info="Transcriber Structured Data. Used only for the file name."),

  StrInput(name="file_prefix", display_name="File Prefix", value="ceq_audit"),

  StrInput(name="report_title", display_name="Page Title", value="CEQ Audit Report"),

  DropdownInput(name="storage", display_name="Storage",

   options=["flow file store (download link)", "flow file store + local copy", "local folder only"],

   value="flow file store (download link)"),

  StrInput(name="base_url", display_name="MDS Base URL",

   value="https://matchaflow.harriscomputer.com"),

  SecretStrInput(name="flow_api_key", display_name="MDS API Key (fallback upload)", required=False,

   info="Only used if the internal storage service is unavailable."),

  StrInput(name="local_dir", display_name="Local Folder", value="/app/generated_reports", advanced=True),

  IntInput(name="keep_days", display_name="Delete Reports Older Than (days)", value=7,

   info="Reports contain personal data. 0 = never delete."),

  BoolInput(name="inline_link", display_name="Add Inline Data Link", value=False, advanced=True,

   info="Embeds the file as a data: link. Some chat views block these."),

  IntInput(name="inline_max_kb", display_name="Inline Link Max Size (KB)", value=300, advanced=True),

 ]

 outputs = [

  Output(display_name="Report Output", name="html", method="build_report", group_outputs=True),

  Output(display_name="File Info", name="file_info", method="build_info", group_outputs=True),

 ]

 # ---------------- storage ----------------

 def _flow_id(self):

  fid = getattr(self, "flow_id", None)

  if not fid:

   fid = getattr(getattr(self, "graph", None), "flow_id", None)

  return str(fid or "")

 def _storage_service(self, errors):

  for path in ("lfx.services.deps", "langflow.services.deps"):

   try:

    mod = __import__(path, fromlist=["get_storage_service"])

    svc = mod.get_storage_service()

    if svc is not None:

     return svc

   except Exception as e:

    errors.append(path + ": " + str(e)[:100])

  return None

 async def _save_store(self, flow_id, name, data, errors):

  svc = self._storage_service(errors)

  if svc is not None:

   try:

    await svc.save_file(flow_id=flow_id, file_name=name, data=data)

    await self._cleanup_store(svc, flow_id, errors)

    return "storage service", name

   except Exception as e:

    errors.append("storage service save: " + str(e)[:150])

  key = str(self.flow_api_key or "").strip()

  if not key:

   errors.append("no API key for fallback upload")

   return "", ""

  url = (self.base_url or "").rstrip("/") + "/api/v1/files/upload/" + flow_id

  try:

   r = await asyncio.to_thread(

    requests.post, url,

    headers={"x-api-key": key},

    files={"file": (name, data, "text/html")},

    timeout=60,

   )

   if r.status_code in (200, 201):

    path = ""

    try:

     path = str((r.json() or {}).get("file_path") or "")

    except ValueError:

     pass

    return "upload API", (path.split("/")[-1] if path else name)

   errors.append("upload API HTTP " + str(r.status_code) + ": " + r.text[:120])

  except Exception as e:

   errors.append("upload API: " + str(e)[:150])

  return "", ""

 async def _cleanup_store(self, svc, flow_id, errors):

  keep = _int(self.keep_days, 7)

  prefix = _safe(self.file_prefix) or "report"

  if keep <= 0:

   return

  try:

   names = await svc.list_files(flow_id=flow_id)

  except Exception as e:

   errors.append("cleanup list: " + str(e)[:100])

   return

  for n in names or []:

   n = str(n)

   if prefix in n and n.endswith(".html") and _expired(n, keep):

    try:

     await svc.delete_file(flow_id=flow_id, file_name=n)

    except Exception:

     pass

 def _save_local(self, name, data, errors):

  folder = Path((self.local_dir or "").strip() or "/app/generated_reports")

  try:

   folder.mkdir(parents=True, exist_ok=True)

   path = folder / name

   path.write_bytes(data)

   keep = _int(self.keep_days, 7)

   prefix = _safe(self.file_prefix) or "report"

   for old in folder.glob("*" + prefix + "*.html"):

    if _expired(old.name, keep):

     try:

      old.unlink()

     except Exception:

      pass

   return str(path)

  except Exception as e:

   errors.append("local save: " + str(e)[:150])

   return ""

 # ---------------- main ----------------

 async def _build(self):

  if getattr(self, "_res", None) is not None:

   return self._res

  errors = []

  body = _text(self.html_content).strip()

  if not body:

   self._res = {"ok": False, "error": "No HTML content received"}

   return self._res

  call = _data(self.call_data) if self.call_data else {}

  stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

  parts = [

   _safe(self.file_prefix) or "report",

   _safe(call.get("agent_names"), 30),

   _safe(str(call.get("conversation_id") or "")[:8], 8),

   stamp,

  ]

  name = "_".join(p for p in parts if p) + ".html"

  doc = _wrap(body, self.report_title or "CEQ Audit Report")

  data = doc.encode("utf-8")

  info = {

   "ok": False,

   "file_name": name,

   "size_kb": round(len(data) / 1024, 1),

   "characters": len(doc),

   "conversation_id": call.get("conversation_id"),

   "download_url": "",

   "local_path": "",

   "stored_via": "",

   "keep_days": _int(self.keep_days, 7),

   "errors": errors,

  }

  mode = str(self.storage or "")

  if mode.startswith("flow"):

   flow_id = self._flow_id()

   if not flow_id:

    errors.append("flow id not available - save the flow and run it again")

   else:

    via, stored = await self._save_store(flow_id, name, data, errors)

    if via:

     info["stored_via"] = via

     info["file_name"] = stored

     info["download_url"] = (

      (self.base_url or "").rstrip("/") + "/api/v1/files/download/"

      + flow_id + "/" + urllib.parse.quote(stored)

     )

     info["ok"] = True

  if mode.endswith("local copy") or mode.startswith("local") or not info["ok"]:

   info["local_path"] = self._save_local(name, data, errors)

   if info["local_path"] and mode.startswith("local"):

    info["ok"] = True

  if self.inline_link and len(data) <= _int(self.inline_max_kb, 300) * 1024:

   info["inline_link"] = "data:text/html;charset=utf-8;base64," + base64.b64encode(data).decode("ascii")

  self._res = info

  return info

 async def build_report(self) -> Message:

  r = await self._build()

  if not r.get("file_name"):

   self.status = "FAILED: " + str(r.get("error"))

   return Message(text="Report not generated: " + str(r.get("error")))

  lines = []

  if r.get("download_url"):

   lines.append("**CEQ report ready**: " + r["file_name"] + " (" + str(r["size_kb"]) + " KB)")

   lines.append("")

   lines.append("[Download report](" + r["download_url"] + ")")

   lines.append("")

   lines.append("Open the downloaded file to view it in your browser. "

    "The link requires an active MDS login.")

  elif r.get("local_path"):

   lines.append("**Report saved on the server only**: " + r["local_path"] + " (" + str(r["size_kb"]) + " KB)")

   lines.append("No download link: the flow file store was not available.")

  else:

   lines.append("**Report could not be saved.**")

  if r.get("inline_link"):

   lines.append("")

   lines.append("[Download (inline copy)](" + r["inline_link"] + ")")

  if r.get("keep_days"):

   lines.append("")

   lines.append("Reports are deleted automatically after " + str(r["keep_days"]) + " days.")

  if r.get("errors"):

   lines.append("")

   lines.append("Notes: " + "; ".join(r["errors"])[:400])

  
  if r.get("ok"):

   self.status = "Saved " + r["file_name"] + " via " + (r.get("stored_via") or "local")

  else:

   self.status = "NOT SAVED: " + "; ".join(r.get("errors") or [])[:200]

  return Message(text="\n".join(lines))

 async def build_info(self) -> Data:

  r = await self._build()

  out = {k: v for k, v in r.items() if k != "inline_link"}

  return Data(data=out)
