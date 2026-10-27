import os
import glob
import json
import tempfile
import requests
from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, StrInput, SecretStrInput, BoolInput, Output
from lfx.schema.message import Message

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "qa_transcript_cache")


def _data(v):
 if isinstance(v, list) and v:
  v = v[0]
 if hasattr(v, "data"):
  v = v.data or {}
 return v if isinstance(v, dict) else {}


def _text(v):
 if hasattr(v, "text"):
  v = v.text
 return str(v or "")


def _remove(path, done, failed):
 try:
  if path and os.path.exists(path):
   os.remove(path)
   done.append(os.path.basename(path))
 except Exception as e:
  failed.append(os.path.basename(str(path)) + ": " + str(e)[:80])


class CustomComponent(Component):
 display_name = "Post-Audit Purge"
 description = "Deletes call audio, transcript cache and (optionally) Langflow run messages after the audit completes."

 inputs = [
  DataInput(name="call_data", display_name="Call Data", required=True,
   info="Structured Data output of the QA Transcriber."),
  MessageTextInput(name="audit_done", display_name="Audit Result (trigger)", required=True,
   info="Connect Audit Result Parser Row JSON so this runs only after the audit."),
  BoolInput(name="purge_files", display_name="Delete Audio + Transcript Cache", value=True),
  BoolInput(name="purge_messages", display_name="Delete Langflow Run Messages", value=False),
  StrInput(name="flow_base_url", display_name="Langflow Base URL",
   value="https://matchaflow.harriscomputer.com", advanced=True),
  SecretStrInput(name="flow_api_key", display_name="Langflow API Key (x-api-key)", required=False),
  StrInput(name="session_id", display_name="Session ID", value="", advanced=True,
   info="Blank = this run's session."),
 ]

 outputs = [Output(display_name="Purge Report", name="report", method="purge")]

 def purge(self) -> Message:
  call = _data(self.call_data)
  done, failed, notes = [], [], []
  if not _text(self.audit_done).strip():
   return Message(text="PURGE SKIPPED: no audit result received")
  if self.purge_files:
   for p in call.get("audio_paths") or []:
    _remove(p, done, failed)
    for extra in glob.glob(str(p) + ".*"):
     _remove(extra, done, failed)
   for key in call.get("cache_keys") or []:
    for suffix in (".json", ".status", ".json.tmp", ".status.tmp"):
     _remove(os.path.join(_CACHE_DIR, str(key) + suffix), done, failed)
   if not call.get("cache_keys"):
    notes.append("no cache_keys in Call Data - add the Transcriber patch")
  if self.purge_messages:
   session = (self.session_id or "").strip() or str(getattr(self.graph, "session_id", "") or "")
   if not session:
    notes.append("no session id - messages not deleted")
   else:
    url = (self.flow_base_url or "").rstrip("/") + "/api/v1/monitor/messages/session/" + session
    try:
     r = requests.delete(url, headers={"x-api-key": str(self.flow_api_key or "")}, timeout=30)
     notes.append("messages delete HTTP " + str(r.status_code))
    except Exception as e:
     notes.append("messages delete failed: " + str(e)[:120])
  report = {
   "conversation_id": call.get("conversation_id"),
   "deleted": done,
   "failed": failed,
   "notes": notes,
  }
  self.status = "deleted " + str(len(done)) + " | failed " + str(len(failed)) + (" | " + "; ".join(notes) if notes else "")
  return Message(text=json.dumps(report, ensure_ascii=False))
