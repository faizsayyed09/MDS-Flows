import os
import re
import json
import time
import fcntl
import hashlib
import tempfile
import threading
from datetime import datetime, timezone

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import (
    DataInput,
    StrInput,
    SecretStrInput,
    IntInput,
    BoolInput,
    DropdownInput,
    MultilineInput,
    Output,
)
from lfx.schema.message import Message

_WORKER_LOCK = threading.Lock()
_WORKER = {"thread": None}
_CFG = {}
_CFG_LOCK = threading.Lock()

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID_FULL = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_BG_MODE = "background (UI-safe)"
_MAX_ERRORS = 5
_KEEP_SECONDS = 3 * 86400
_FINISHED = ("done", "failed", "skipped_cap", "skipped_excluded")
_ACTIVE = ("queued", "running")
_MATCHA_DEFAULT = "https://matcha.harriscomputer.com"

ACTIONS = [
    "Poll and dispatch",
    "Show status only",
    "SNOW health check",
    "Run SNOW cleanup now",
    "Stop worker",
    "Reset state",
]
CHANNEL_MODES = ["Calls and Emails", "Calls only", "Emails only"]
ENVIRONMENTS = ["DEV", "TEST", "PROD"]

_CONFIG_FIELDS = [
    "environment", "channel_mode", "dispatch_enabled", "call_run_url", "email_run_url",
    "downloader_id", "transcriber_id", "fetcher_id", "call_marker", "email_marker",
    "excluded_agents", "excluded_queues", "inbound_only", "max_calls_per_agent",
    "max_emails_per_agent", "ticket_grace_min", "require_fresh_snow", "snow_folder_id",
    "snow_max_age_min", "snow_processing_grace_min", "snow_hold_max_hours", "cleanup_enabled",
    "snow_keep_latest", "cleanup_min_age_hours", "min_duration_sec", "lookback_hours",
    "recheck_min", "max_wait_min", "ui_wait_sec", "request_timeout_sec",
]


# ---------------- storage ----------------

def _data_dir():
    base = os.environ.get("LANGFLOW_CONFIG_DIR") or ""
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        path = os.path.join(base, "ceq_audit")
    else:
        path = os.path.join(tempfile.gettempdir(), "ceq_audit")
    os.makedirs(path, exist_ok=True)
    return path


def _p(name):
    return os.path.join(_data_dir(), name)


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, default=str)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def _log_event(kind, **fields):
    record = {"ts": time.time(), "event": kind}
    record.update(fields)
    _append_jsonl(_p("audit_log.jsonl"), record)


class _FileLock:
    def __init__(self, name):
        self.path = _p(name)
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _try_lease():
    fh = open(_p("worker.lease"), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


# ---------------- state ----------------

def _empty_state():
    return {
        "started_at": None,
        "calls": {},
        "daily": {},
        "queue_cache": {},
        "paused": "",
        "last_poll": None,
        "snow": {},
        "cleanup": [],
    }


def _load_state():
    state = _read_json(_p("dispatcher_state.json"), None) or _empty_state()
    for key, value in _empty_state().items():
        state.setdefault(key, value)
    return state


def _save_state(state):
    _write_json(_p("dispatcher_state.json"), state)


def _update_item(conv_id, **changes):
    with _FileLock("state.lock"):
        state = _load_state()
        entry = state["calls"].get(conv_id)
        if entry is not None:
            entry.update(changes)
            _save_state(state)
        return entry


def _set_paused(reason):
    with _FileLock("state.lock"):
        state = _load_state()
        state["paused"] = reason
        _save_state(state)
    _log_event("paused", reason=reason)


# ---------------- time ----------------

def _epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _fmt(ts):
    if not ts:
        return "-"
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York")).strftime("%m-%d %H:%M ET")
    except Exception:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%m-%d %H:%M UTC")


def _et_date(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


# ---------------- Genesys ----------------

def _genesys_headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _agents_from_roster(roster_value):
    agents = {}
    value = roster_value
    if isinstance(value, list) and value:
        value = value[0]
    if hasattr(value, "data"):
        value = value.data or {}
    if isinstance(value, dict):
        for uid, info in (value.get("agents") or {}).items():
            if isinstance(info, dict):
                agents[str(uid).lower()] = {
                    "name": info.get("name") or info.get("roster_name") or str(uid)[:8],
                    "manager": info.get("manager") or "",
                }
    return agents


def _apply_exclusions(agents, excluded_text):
    tokens = [t.strip().lower() for t in re.split(r"[\n,;]", excluded_text or "") if t.strip()]
    if not tokens:
        return agents, 0
    kept = {}
    removed = 0
    for uid, info in agents.items():
        if uid in tokens or str(info.get("name") or "").lower() in tokens:
            removed += 1
            continue
        kept[uid] = info
    return kept, removed


def _resolve_queues(token, region, entries, cache):
    ids = []
    missing = []
    url = "https://api." + region + "/api/v2/routing/queues"
    for entry in entries:
        if _UUID_FULL.match(entry):
            ids.append(entry.lower())
            continue
        key = entry.lower()
        if cache.get(key):
            ids.append(cache[key])
            continue
        try:
            resp = requests.get(
                url,
                headers=_genesys_headers(token),
                params={"name": entry, "pageSize": 25},
                timeout=20,
            )
        except Exception as e:
            missing.append(entry + " (" + str(e)[:60] + ")")
            continue
        if resp.status_code >= 400:
            missing.append(entry + " (HTTP " + str(resp.status_code) + ")")
            continue
        match = None
        for q in (resp.json() or {}).get("entities") or []:
            if str(q.get("name", "")).strip().lower() == key:
                match = str(q.get("id")).lower()
                break
        if match:
            cache[key] = match
            ids.append(match)
        else:
            missing.append(entry + " (not found)")
    return ids, missing


def _query_by_agents(token, region, start_ts, end_ts, agent_ids, inbound_only, media_type):
    url = "https://api." + region + "/api/v2/analytics/conversations/details/query"
    found = {}
    ids = sorted(agent_ids)
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        segment_filters = [
            {"type": "and", "predicates": [{"dimension": "mediaType", "value": media_type}]},
            {"type": "or", "predicates": [{"dimension": "userId", "value": u} for u in chunk]},
        ]
        page = 1
        while page <= 5:
            body = {
                "interval": _iso(start_ts) + "/" + _iso(end_ts),
                "order": "asc",
                "orderBy": "conversationStart",
                "paging": {"pageSize": 100, "pageNumber": page},
                "segmentFilters": segment_filters,
            }
            if inbound_only:
                body["conversationFilters"] = [
                    {
                        "type": "and",
                        "predicates": [{"dimension": "originatingDirection", "value": "inbound"}],
                    }
                ]
            resp = requests.post(url, headers=_genesys_headers(token), json=body, timeout=45)
            if resp.status_code >= 400:
                raise RuntimeError("HTTP " + str(resp.status_code) + ": " + (resp.text or "")[:200])
            data = resp.json() if (resp.text or "").strip() else {}
            batch = data.get("conversations") or []
            for c in batch:
                if c.get("conversationId"):
                    found[c["conversationId"]] = c
            if len(batch) < 100:
                break
            page += 1
    return list(found.values())


# ---------------- SNOW folder ----------------

def _matcha_headers(key):
    return {"MATCHA-API-KEY": key, "Accept": "application/json"}


def _snow_files(base, key, folder_id):
    url = base.rstrip("/") + "/rest/api/v1/files"
    resp = requests.get(url, headers=_matcha_headers(key), params={"folder_id": folder_id}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError("HTTP " + str(resp.status_code) + ": " + (resp.text or "")[:150])
    data = resp.json() if (resp.text or "").strip() else []
    if isinstance(data, dict):
        for k in ("data", "items", "files", "entities", "results"):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    return data if isinstance(data, list) else []


def _snow_health(files, now, max_age_min, processing_grace_min):
    ready = []
    failed = []
    processing = []
    for f in files:
        status = str(f.get("status") or "").lower()
        ingest = str(f.get("ingestion_status") or "").lower()
        item = {
            "id": f.get("id"),
            "filename": f.get("filename"),
            "status": f.get("status"),
            "ingestion_status": f.get("ingestion_status"),
            "updated": _epoch(f.get("updated_at")),
        }
        if "fail" in status or "error" in status or "fail" in ingest or "error" in ingest:
            failed.append(item)
        elif status == "uploaded" and ingest == "completed":
            ready.append(item)
        else:
            processing.append(item)
    ready.sort(key=lambda x: x["updated"] or 0, reverse=True)
    latest = ready[0]["updated"] if ready else None
    age_min = round((now - latest) / 60, 1) if latest else None
    stuck = [
        x for x in processing
        if x["updated"] and (now - x["updated"]) / 60 > processing_grace_min
    ]
    if not files:
        status = "MISSING"
    elif not ready:
        status = "FAILED" if (failed or stuck) else "PROCESSING"
    elif age_min is not None and age_min > max_age_min:
        status = "STALE"
    elif failed or stuck:
        status = "DEGRADED"
    else:
        status = "FRESH"
    return {
        "status": status,
        "checked": now,
        "files_total": len(files),
        "ready_count": len(ready),
        "processing_count": len(processing),
        "failed_count": len(failed),
        "stuck_count": len(stuck),
        "latest_ready": latest,
        "latest_file": ready[0]["filename"] if ready else "",
        "age_min": age_min,
        "ready": ready,
        "failed": failed,
        "stuck": stuck,
    }


def _snow_cleanup(base, key, folder_id, health, keep_latest, min_age_hours, now):
    deleted = []
    errors = []
    if not health.get("ready"):
        return deleted, ["no ready export, nothing deleted"]
    candidates = health["ready"][max(1, keep_latest):] + health["failed"]
    for f in candidates:
        if not f.get("updated") or now - f["updated"] < min_age_hours * 3600:
            continue
        url = base.rstrip("/") + "/rest/api/v1/file/" + str(f["id"])
        try:
            resp = requests.delete(url, headers=_matcha_headers(key), params={"folder_id": folder_id}, timeout=30)
            if resp.status_code < 300:
                deleted.append(str(f.get("filename")))
            else:
                errors.append(str(f.get("filename")) + " HTTP " + str(resp.status_code))
        except Exception as e:
            errors.append(str(f.get("filename")) + " " + str(e)[:60])
    return deleted, errors


# ---------------- worker ----------------

def _classify(status, text, marker):
    if status in (401, 403, 404):
        return "fatal"
    if status is None or status == 429 or status >= 500:
        return "retry"
    if status >= 400:
        return "error"
    if "TRANSCRIPTION_PENDING" in text or "NOT_READY" in text:
        return "pending"
    if marker and marker in text:
        return "done"
    if "EMAIL_FETCH_ERROR" in text or "TRANSCRIPTION_ERROR" in text:
        return "error"
    if "CALL QA REPORT" in text or "EMAIL FETCH REPORT" in text:
        return "not_sent"
    return "error"


def _subject(text, marker):
    if not marker:
        return "sent"
    m = re.search(re.escape(marker) + r"[^\"\\]{0,160}", text)
    return m.group(0).strip() if m else "sent"


def _sleep_with_stop(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(_p("stop")):
            return False
        time.sleep(5)
    return True


def _process_item(conv_id):
    with _FileLock("state.lock"):
        state = _load_state()
        entry = state["calls"].get(conv_id)
        if entry is None:
            return "missing"
        channel = entry.get("channel") or "CALL"
        first_try = entry.get("first_try") or time.time()
        errors = int(entry.get("errors") or 0)
        attempts = int(entry.get("attempts") or 0)
        entry.update({"state": "running", "first_try": first_try})
        _save_state(state)

    while True:
        if os.path.exists(_p("stop")):
            _update_item(conv_id, state="queued")
            return "stopped"

        with _CFG_LOCK:
            cfg = dict(_CFG)

        if channel == "EMAIL":
            run_url = cfg.get("email_run_url")
            marker = cfg.get("email_marker", "")
            tweaks = {cfg.get("fetcher_id"): {"conversation_id": conv_id}}
            auth_id = cfg.get("email_auth_id")
        else:
            run_url = cfg.get("call_run_url")
            marker = cfg.get("call_marker", "")
            tweaks = {
                cfg.get("downloader_id"): {"conversation_id": conv_id},
                cfg.get("transcriber_id"): {"run_mode": _BG_MODE, "ui_wait_sec": cfg.get("ui_wait", 45)},
            }
            auth_id = cfg.get("auth_id")
        if auth_id and cfg.get("token"):
            tweaks[auth_id] = {"manual_token": cfg["token"]}

        if not run_url:
            _update_item(conv_id, state="failed", finished=time.time(), last_error="No run URL for " + channel)
            return "failed"

        body = {
            "output_type": "chat",
            "input_type": "text",
            "input_value": "audit",
            "session_id": "audit-" + conv_id,
            "tweaks": tweaks,
        }
        status = None
        text = ""
        try:
            resp = requests.post(
                run_url,
                headers={"Content-Type": "application/json", "x-api-key": cfg.get("api_key", "")},
                json=body,
                timeout=cfg.get("request_timeout", 300),
            )
            status = resp.status_code
            text = resp.text or ""
        except Exception as e:
            text = str(e)[:300]

        attempts += 1
        verdict = _classify(status, text, marker)

        if verdict == "done":
            result = _subject(text, marker)
            _update_item(conv_id, state="done", attempts=attempts, finished=time.time(), result=result, last_error="")
            _log_event("audit_done", conversation_id=conv_id, channel=channel, attempts=attempts, result=result)
            return "done"

        if verdict == "fatal":
            _update_item(conv_id, state="queued", attempts=attempts, last_error="HTTP " + str(status))
            _set_paused(
                channel + " flow returned HTTP " + str(status)
                + ". Check the API key, run URL and component IDs. " + text[:150]
            )
            return "fatal"

        if verdict == "not_sent":
            _update_item(
                conv_id,
                state="failed",
                attempts=attempts,
                finished=time.time(),
                last_error="Audit ran but no subject with the completion marker was returned.",
            )
            _log_event("audit_failed", conversation_id=conv_id, channel=channel, reason="not_sent")
            return "failed"

        if verdict == "error":
            errors += 1
            if errors >= _MAX_ERRORS:
                _update_item(
                    conv_id, state="failed", attempts=attempts, errors=errors,
                    finished=time.time(), last_error=text[:250],
                )
                _log_event("audit_failed", conversation_id=conv_id, channel=channel, reason=text[:150])
                return "failed"

        if time.time() - first_try > cfg.get("max_wait_min", 90) * 60:
            _update_item(
                conv_id, state="failed", attempts=attempts, errors=errors, finished=time.time(),
                last_error="Not finished within " + str(cfg.get("max_wait_min", 90)) + " min. Last: " + text[:150],
            )
            _log_event("audit_failed", conversation_id=conv_id, channel=channel, reason="timeout")
            return "failed"

        _update_item(
            conv_id, state="running", attempts=attempts, errors=errors, last_try=time.time(),
            last_error=verdict + (": " + text[:120] if verdict != "pending" else ""),
        )
        if not _sleep_with_stop(cfg.get("recheck_min", 2) * 60):
            _update_item(conv_id, state="queued")
            return "stopped"


def _worker():
    lease = _try_lease()
    if lease is None:
        with _WORKER_LOCK:
            _WORKER["thread"] = None
        return
    try:
        while not os.path.exists(_p("stop")):
            with _FileLock("state.lock"):
                state = _load_state()
                paused = state.get("paused")
                ready = sorted(
                    (v.get("due_at") or 0, k)
                    for k, v in state["calls"].items()
                    if v.get("state") in _ACTIVE
                )
            if paused or not ready:
                break
            result = _process_item(ready[0][1])
            if result in ("fatal", "stopped"):
                break
    except Exception as e:
        _set_paused("Worker crashed: " + str(e)[:200])
    finally:
        try:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()
        except Exception:
            pass
        with _WORKER_LOCK:
            _WORKER["thread"] = None


def _worker_alive():
    lease = _try_lease()
    if lease is None:
        return True
    try:
        fcntl.flock(lease, fcntl.LOCK_UN)
    finally:
        lease.close()
    return False


def _start_worker():
    if _worker_alive():
        return False
    with _WORKER_LOCK:
        thread = _WORKER.get("thread")
        if thread and thread.is_alive():
            return False
        thread = threading.Thread(target=_worker, daemon=True)
        _WORKER["thread"] = thread
        thread.start()
    return True


# ---------------- component ----------------

class CustomComponent(Component):
    display_name = "New Calls Dispatcher"
    description = (
        "Control plane for CEQ audits: finds calls and emails for rostered agents, waits for a fresh "
        "SNOW export, audits one item at a time, and keeps the SNOW folder clean."
    )

    inputs = [
        # ---- Operations ----
        DropdownInput(name="action", display_name="Action", options=ACTIONS, value="Poll and dispatch"),
        BoolInput(
            name="dispatch_enabled", display_name="Dispatch Enabled", value=False,
            info="Off = dry run: finds and schedules items but runs no audits.",
        ),
        DropdownInput(name="environment", display_name="Environment", options=ENVIRONMENTS, value="DEV"),
        DropdownInput(
            name="channel_mode", display_name="Channels to Audit", options=CHANNEL_MODES, value="Calls only",
        ),
        StrInput(
            name="confirm_reset", display_name="Confirm Reset", value="",
            info="Type RESET only when running 'Reset state'. Clear it afterwards.",
        ),
        DataInput(name="auth", display_name="Genesys Auth", required=False),
        DataInput(name="roster", display_name="Roster", required=False, info="Roster Data from Audit Roster."),

        # ---- Audit flows ----
        StrInput(
            name="call_run_url", display_name="Call Audit Run URL",
            value="https://matchaflow.harriscomputer.com/api/v1/run/cb5bb3e4-5e73-4306-a885-74491d4e3c96?stream=false",
        ),
        StrInput(name="email_run_url", display_name="Email Audit Run URL", value=""),
        SecretStrInput(name="api_key", display_name="MDS API Key", value="", required=False),
        StrInput(name="downloader_id", display_name="Call: Downloader ID", value="CustomComponent-fhO4w", advanced=True),
        StrInput(name="transcriber_id", display_name="Call: Transcriber ID", value="CustomComponent-KG8gw", advanced=True),
        StrInput(name="fetcher_id", display_name="Email: Fetcher ID", value="CustomComponent-dD0nT", advanced=True),
        StrInput(
            name="auth_component_id", display_name="Call: Auth ID (manual token only)", value="", advanced=True,
            info="Leave blank when the audit flows use client credentials.",
        ),
        StrInput(name="email_auth_component_id", display_name="Email: Auth ID (manual token only)", value="", advanced=True),
        StrInput(name="call_marker", display_name="Call Completion Marker", value="[Call CEQ]", advanced=True),
        StrInput(name="email_marker", display_name="Email Completion Marker", value="[Email CEQ]", advanced=True),

        # ---- Scope ----
        MultilineInput(
            name="excluded_agents", display_name="Analyst Exclusion List", value="",
            info="Names or user IDs, one per line or comma-separated. Excluded analysts are never audited.",
        ),
        MultilineInput(
            name="excluded_queues", display_name="Queue Exclusion List", value="",
            info="Queue names or IDs. Interactions touching these queues are skipped.",
        ),
        BoolInput(name="inbound_only", display_name="Inbound Only", value=True),
        IntInput(name="max_calls_per_agent", display_name="Max Calls per Analyst per Day", value=2, info="0 = no limit."),
        IntInput(name="max_emails_per_agent", display_name="Max Emails per Analyst per Day", value=3, info="0 = no limit."),
        IntInput(
            name="ticket_grace_min", display_name="Ticket Grace (minutes)", value=10,
            info="Time analysts need after the interaction to create or update the ticket.",
        ),

        # ---- SNOW freshness and cleanup ----
        BoolInput(
            name="require_fresh_snow", display_name="Require Fresh SNOW Export", value=True,
            info="Hold each item until an export newer than interaction end + grace is ingested.",
        ),
        StrInput(name="matcha_base_url", display_name="Matcha Base URL", value=_MATCHA_DEFAULT, advanced=True),
        SecretStrInput(name="matcha_api_key", display_name="Matcha API Key", value="", required=False),
        StrInput(name="snow_folder_id", display_name="SNOW Folder ID", value="31536"),
        IntInput(name="snow_max_age_min", display_name="SNOW Stale After (minutes)", value=60),
        IntInput(name="snow_processing_grace_min", display_name="SNOW Ingestion Timeout (minutes)", value=30, advanced=True),
        IntInput(
            name="snow_hold_max_hours", display_name="Max SNOW Hold (hours)", value=4,
            info="After this, items are audited anyway and flagged.",
        ),
        BoolInput(
            name="cleanup_enabled", display_name="Auto-Clean SNOW Folder", value=False,
            info="Delete superseded and failed exports on each poll, only when no audit is in progress.",
        ),
        IntInput(name="snow_keep_latest", display_name="Keep Newest Exports", value=2),
        IntInput(name="cleanup_min_age_hours", display_name="Delete Only Files Older Than (hours)", value=2),

        # ---- Tuning ----
        IntInput(name="min_duration_sec", display_name="Min Call Duration (seconds)", value=60, advanced=True),
        IntInput(name="lookback_hours", display_name="Lookback (hours)", value=4, advanced=True),
        IntInput(name="recheck_min", display_name="Recheck Interval (minutes)", value=2, advanced=True),
        IntInput(name="max_wait_min", display_name="Max Minutes per Item", value=90, advanced=True),
        IntInput(name="ui_wait_sec", display_name="Transcriber Wait per Request (seconds)", value=45, advanced=True),
        IntInput(name="request_timeout_sec", display_name="Request Timeout (seconds)", value=300, advanced=True),
    ]

    outputs = [
        Output(display_name="Report", name="report", method="build_dispatch_report"),
    ]

    # ---------------- config ----------------

    def _auth_data(self):
        auth = self.auth
        if isinstance(auth, list) and auth:
            auth = auth[0]
        if hasattr(auth, "data"):
            return auth.data or {}
        return auth if isinstance(auth, dict) else {}

    def _track_config(self):
        snap = {f: getattr(self, f, None) for f in _CONFIG_FIELDS}
        digest = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
        path = _p("config_history.jsonl")
        last = None
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [line for line in f.read().splitlines() if line.strip()]
            if lines:
                try:
                    last = json.loads(lines[-1])
                except Exception:
                    last = None
        if last and last.get("version") == digest:
            return "Config version " + digest + " (active since " + _fmt(last.get("ts")) + ")"
        previous = (last or {}).get("config") or {}
        changed = sorted(k for k in snap if previous.get(k) != snap[k])
        _append_jsonl(path, {
            "ts": time.time(),
            "version": digest,
            "environment": self.environment,
            "changed": changed,
            "config": snap,
        })
        return "Config version " + digest + " (NEW, changed: " + ", ".join(changed[:8]) + ")"

    def _validate(self):
        errors = []
        warnings = []
        mode = self.channel_mode or "Calls only"
        calls = "Calls" in mode
        emails = "Emails" in mode
        if self.dispatch_enabled:
            if not (self.api_key or "").strip():
                errors.append("MDS API Key is required to dispatch.")
            if calls and not ((self.call_run_url or "").strip() and self.downloader_id and self.transcriber_id):
                errors.append("Calls enabled: set Call Audit Run URL, Downloader ID and Transcriber ID.")
            if emails and not ((self.email_run_url or "").strip() and self.fetcher_id):
                errors.append("Emails enabled: set Email Audit Run URL and Fetcher ID.")
        if (self.call_run_url or "").strip() and (self.call_run_url or "").strip() == (self.email_run_url or "").strip():
            errors.append("Call and Email run URLs must point to different flows.")
        if self.require_fresh_snow and not ((self.matcha_api_key or "").strip() and (self.snow_folder_id or "").strip()):
            errors.append("Require Fresh SNOW Export needs the Matcha API Key and SNOW Folder ID.")
        if int(self.snow_max_age_min or 0) < 15:
            warnings.append("SNOW Stale After below 15 minutes will flag normal exports as stale.")
        if self.cleanup_enabled and int(self.snow_keep_latest or 0) < 1:
            errors.append("Keep Newest Exports must be at least 1.")
        if int(self.max_calls_per_agent or 0) < 0 or int(self.max_emails_per_agent or 0) < 0:
            errors.append("Daily caps cannot be negative.")
        if self.environment == "PROD" and (self.auth_component_id or self.email_auth_component_id):
            warnings.append("PROD is passing a manual Genesys token. Switch the audit flows to client credentials.")
        return errors, warnings

    def _publish_cfg(self):
        auth = self._auth_data()
        with _CFG_LOCK:
            _CFG.update({
                "call_run_url": (self.call_run_url or "").strip(),
                "email_run_url": (self.email_run_url or "").strip(),
                "api_key": (self.api_key or "").strip(),
                "downloader_id": (self.downloader_id or "").strip(),
                "transcriber_id": (self.transcriber_id or "").strip(),
                "fetcher_id": (self.fetcher_id or "").strip(),
                "auth_id": (self.auth_component_id or "").strip(),
                "email_auth_id": (self.email_auth_component_id or "").strip(),
                "call_marker": (self.call_marker or "").strip(),
                "email_marker": (self.email_marker or "").strip(),
                "token": auth.get("access_token") or "",
                "ui_wait": max(10, int(self.ui_wait_sec or 45)),
                "recheck_min": max(1, int(self.recheck_min or 2)),
                "max_wait_min": max(10, int(self.max_wait_min or 90)),
                "request_timeout": max(60, int(self.request_timeout_sec or 300)),
            })

    # ---------------- SNOW ----------------

    def _snow_check(self, cleanup):
        key = (self.matcha_api_key or "").strip()
        folder = (self.snow_folder_id or "").strip()
        base = (self.matcha_base_url or _MATCHA_DEFAULT).strip()
        if not key or not folder:
            return None, ["SNOW check skipped: set Matcha API Key and SNOW Folder ID."]
        now = time.time()
        notes = []
        try:
            files = _snow_files(base, key, folder)
        except Exception as e:
            health = {"status": "UNREACHABLE", "checked": now, "error": str(e)[:150]}
            _write_json(_p("snow_freshness.json"), health)
            _log_event("snow_unreachable", error=str(e)[:150])
            return health, ["SNOW check failed: " + str(e)[:150]]

        health = _snow_health(
            files, now, int(self.snow_max_age_min or 60), int(self.snow_processing_grace_min or 30)
        )

        if cleanup:
            with _FileLock("state.lock"):
                busy = any(v.get("state") in _ACTIVE for v in _load_state()["calls"].values())
            if busy:
                notes.append("SNOW cleanup postponed: an audit is queued or running.")
            else:
                deleted, errs = _snow_cleanup(
                    base, key, folder, health,
                    int(self.snow_keep_latest or 2), float(self.cleanup_min_age_hours or 2), now,
                )
                if deleted or errs:
                    _log_event("snow_cleanup", deleted=deleted, errors=errs)
                    with _FileLock("state.lock"):
                        state = _load_state()
                        state["cleanup"] = (state.get("cleanup") or [])[-9:] + [
                            {"ts": now, "deleted": deleted, "errors": errs}
                        ]
                        _save_state(state)
                if deleted:
                    notes.append("SNOW cleanup deleted " + str(len(deleted)) + " file(s): " + ", ".join(deleted[:5]))
                if errs:
                    notes.append("SNOW cleanup issues: " + "; ".join(errs[:3]))

        summary = {k: v for k, v in health.items() if k not in ("ready", "failed", "stuck")}
        _write_json(_p("snow_freshness.json"), summary)
        with _FileLock("state.lock"):
            state = _load_state()
            state["snow"] = summary
            _save_state(state)
        if health["status"] in ("STALE", "FAILED", "MISSING", "DEGRADED"):
            _log_event("snow_" + health["status"].lower(), latest=health.get("latest_file"), age_min=health.get("age_min"))
        return health, notes

    # ---------------- poll ----------------

    def _poll(self, snow):
        notes = []
        agents = _agents_from_roster(self.roster)
        if not agents:
            return ["ERROR: no agents. Connect the Audit Roster's Roster Data output."]
        agents, removed = _apply_exclusions(agents, self.excluded_agents)
        if removed:
            notes.append(str(removed) + " analyst(s) excluded by the Analyst Exclusion List.")

        auth = self._auth_data()
        token = auth.get("access_token")
        if auth.get("error") or not token:
            return notes + ["ERROR: Genesys token missing or failed. Check the Genesys Cloud Auth component."]
        region = auth.get("region", "mypurecloud.com")

        with _FileLock("state.lock"):
            state = _load_state()
            if not state.get("started_at"):
                state["started_at"] = time.time()
                notes.append("First poll: only interactions ending after " + _fmt(state["started_at"]) + " are audited.")
            state["paused"] = ""
            started_at = state["started_at"]
            cache = dict(state.get("queue_cache") or {})
            _save_state(state)

        excluded_queue_ids = set()
        entries = [q.strip() for q in re.split(r"[\n,;]", self.excluded_queues or "") if q.strip()]
        if entries:
            ids, missing = _resolve_queues(token, region, entries, cache)
            excluded_queue_ids = set(ids)
            if missing:
                notes.append("Excluded queues not resolved: " + "; ".join(missing))

        mode = self.channel_mode or "Calls only"
        medias = []
        if "Calls" in mode:
            medias.append(("CALL", "voice"))
        if "Emails" in mode:
            medias.append(("EMAIL", "email"))

        now = time.time()
        start_ts = max(now - int(self.lookback_hours or 4) * 3600, started_at - 3600)
        found = []
        for channel, media in medias:
            try:
                convs = _query_by_agents(
                    token, region, start_ts, now, list(agents.keys()), bool(self.inbound_only), media
                )
            except Exception as e:
                message = str(e)
                if "401" in message:
                    return notes + ["ERROR: Genesys token expired (HTTP 401)."]
                notes.append("ERROR: " + channel.lower() + " search failed: " + message[:200])
                continue
            for c in convs:
                found.append((channel, c))

        grace = int(self.ticket_grace_min or 10) * 60
        caps = {"CALL": int(self.max_calls_per_agent or 0), "EMAIL": int(self.max_emails_per_agent or 0)}
        min_sec = int(self.min_duration_sec or 60)
        require = bool(self.require_fresh_snow)
        latest_ready = (snow or {}).get("latest_ready")
        hold_max = int(self.snow_hold_max_hours or 4) * 3600
        counts = {"CALL": 0, "EMAIL": 0, "skipped_cap": 0, "skipped_excluded": 0, "promoted": 0, "override": 0}

        with _FileLock("state.lock"):
            state = _load_state()
            state["queue_cache"] = cache
            items = state["calls"]
            daily = state["daily"]
            for channel, c in found:
                conv_id = c.get("conversationId")
                if not conv_id or conv_id in items:
                    continue
                start = _epoch(c.get("conversationStart"))
                end = _epoch(c.get("conversationEnd"))
                if not start or not end or end < started_at:
                    continue
                if channel == "CALL" and end - start < min_sec:
                    continue
                agent_id = None
                queues = set()
                for p in c.get("participants") or []:
                    uid = str(p.get("userId") or "").lower()
                    if p.get("purpose") in ("agent", "user") and uid in agents and not agent_id:
                        agent_id = uid
                    for s in p.get("sessions") or []:
                        for seg in s.get("segments") or []:
                            if seg.get("queueId"):
                                queues.add(str(seg["queueId"]).lower())
                if not agent_id:
                    continue
                entry = {
                    "channel": channel,
                    "agent_id": agent_id,
                    "agent": agents[agent_id]["name"],
                    "manager": agents[agent_id]["manager"],
                    "ended": end,
                    "duration_sec": int(end - start),
                    "due_at": end + grace,
                    "attempts": 0,
                }
                if excluded_queue_ids and queues & excluded_queue_ids:
                    entry["state"] = "skipped_excluded"
                    entry["finished"] = now
                    counts["skipped_excluded"] += 1
                else:
                    day_key = _et_date(end) + "|" + agent_id + "|" + channel
                    cap = caps[channel]
                    if cap and daily.get(day_key, 0) >= cap:
                        entry["state"] = "skipped_cap"
                        entry["finished"] = now
                        counts["skipped_cap"] += 1
                    else:
                        entry["state"] = "waiting"
                        daily[day_key] = daily.get(day_key, 0) + 1
                        counts[channel] += 1
                items[conv_id] = entry

            for entry in items.values():
                if entry.get("state") != "waiting":
                    continue
                due = entry.get("due_at") or 0
                if now < due:
                    continue
                if not require or (latest_ready and latest_ready >= due):
                    entry["state"] = "queued"
                    counts["promoted"] += 1
                elif now - due > hold_max:
                    entry["state"] = "queued"
                    entry["snow_override"] = True
                    counts["promoted"] += 1
                    counts["override"] += 1

            for conv_id in list(items):
                entry = items[conv_id]
                if entry.get("state") in _FINISHED and now - (entry.get("finished") or now) > _KEEP_SECONDS:
                    del items[conv_id]
            cutoff = _et_date(now - 7 * 86400)
            for key in list(daily):
                if key.split("|")[0] < cutoff:
                    del daily[key]
            state["last_poll"] = now
            _save_state(state)

        notes.append(
            "Poll: " + str(len(found)) + " interactions found, " + str(len(agents)) + " analysts | new: "
            + str(counts["CALL"]) + " calls, " + str(counts["EMAIL"]) + " emails | released: "
            + str(counts["promoted"]) + " | over cap: " + str(counts["skipped_cap"])
            + " | excluded queue: " + str(counts["skipped_excluded"])
        )
        if counts["override"]:
            notes.append(
                "WARNING: " + str(counts["override"]) + " item(s) released after the "
                + str(self.snow_hold_max_hours) + " h SNOW hold without a fresh export; ticket matching may fail."
            )
            _log_event("snow_hold_override", count=counts["override"])
        return notes

    # ---------------- status ----------------

    def _status_text(self, notes):
        with _FileLock("state.lock"):
            state = _load_state()
        items = state["calls"]
        lines = list(notes)
        lines.append("")
        lines.append(
            "DISPATCHER [" + str(self.environment or "DEV") + "] | " + str(self.channel_mode)
            + " | dispatch " + ("ON" if self.dispatch_enabled else "OFF (dry run)")
            + " | worker " + ("running" if _worker_alive() else "idle")
            + " | last poll " + _fmt(state.get("last_poll"))
        )
        if state.get("paused"):
            lines.append("PAUSED: " + state["paused"])

        snow = state.get("snow") or {}
        if snow:
            age = snow.get("age_min")
            lines.append(
                "SNOW export: " + str(snow.get("status")) + " | newest ready: " + str(snow.get("latest_file") or "-")
                + ((" (" + str(age) + " min old)") if age is not None else "")
                + " | ready/processing/failed: " + str(snow.get("ready_count", 0)) + "/"
                + str(snow.get("processing_count", 0)) + "/" + str(snow.get("failed_count", 0))
                + " | checked " + _fmt(snow.get("checked"))
            )

        for channel, label in (("CALL", "Calls"), ("EMAIL", "Emails")):
            counts = {}
            for e in items.values():
                if (e.get("channel") or "CALL") == channel:
                    counts[e.get("state")] = counts.get(e.get("state"), 0) + 1
            if not counts:
                continue
            lines.append(
                label + ": waiting " + str(counts.get("waiting", 0))
                + " | queued " + str(counts.get("queued", 0))
                + " | running " + str(counts.get("running", 0))
                + " | done " + str(counts.get("done", 0))
                + " | failed " + str(counts.get("failed", 0))
                + " | skipped " + str(counts.get("skipped_cap", 0) + counts.get("skipped_excluded", 0))
            )
            durations = [
                (e.get("finished") or 0) - (e.get("first_try") or 0)
                for e in items.values()
                if (e.get("channel") or "CALL") == channel and e.get("state") == "done" and e.get("first_try")
            ]
            if durations:
                lines.append("  average processing time: " + str(int(sum(durations) / len(durations) / 60) + 1) + " min")

        today = _et_date(time.time())
        per_manager = {}
        for e in items.values():
            if e.get("state") in ("waiting", "queued", "running", "done") and _et_date(e.get("ended") or 0) == today:
                manager = e.get("manager") or "-"
                per_manager[manager] = per_manager.get(manager, 0) + 1
        if per_manager:
            lines.append("Today by manager: " + ", ".join(m + " " + str(n) for m, n in sorted(per_manager.items())))

        for k, v in items.items():
            if v.get("state") == "running":
                lines.append(
                    "Now auditing: " + str(v.get("channel")) + " " + k + " | " + str(v.get("agent"))
                    + " | attempt " + str(v.get("attempts")) + " | " + str(v.get("last_error") or "")
                )

        waiting = sorted(
            [(v.get("due_at"), k, v) for k, v in items.items() if v.get("state") == "waiting"]
        )[:5]
        if waiting:
            lines.append("")
            lines.append("Waiting (grace or SNOW export):")
            for due, k, v in waiting:
                lines.append(
                    "  " + str(v.get("channel")) + " | " + str(v.get("agent")) + " | ended " + _fmt(v.get("ended"))
                    + " | needs export after " + _fmt(due)
                )

        done = sorted(
            [(v.get("finished"), k, v) for k, v in items.items() if v.get("state") == "done"], reverse=True
        )[:5]
        if done:
            lines.append("")
            lines.append("Recently audited:")
            for fin, k, v in done:
                flag = " [SNOW override]" if v.get("snow_override") else ""
                lines.append("  " + _fmt(fin) + " | " + str(v.get("result")) + flag)

        failed = sorted(
            [(v.get("finished"), k, v) for k, v in items.items() if v.get("state") == "failed"], reverse=True
        )[:5]
        if failed:
            lines.append("")
            lines.append("Recent failures:")
            for fin, k, v in failed:
                lines.append("  " + str(v.get("channel")) + " " + k + " | " + str(v.get("agent")) + " | "
                             + str(v.get("last_error"))[:200])

        cleanup = state.get("cleanup") or []
        if cleanup:
            last = cleanup[-1]
            lines.append("")
            lines.append(
                "Last SNOW cleanup " + _fmt(last.get("ts")) + ": deleted " + str(len(last.get("deleted") or []))
                + (", issues " + str(len(last.get("errors") or [])) if last.get("errors") else "")
            )
        lines.append("Data folder: " + _data_dir())
        return "\n".join(lines)

    # ---------------- output ----------------

    def build_dispatch_report(self) -> Message:
        action = self.action or ACTIONS[0]
        notes = [self._track_config()]
        errors, warnings = self._validate()
        for w in warnings:
            notes.append("CONFIG WARNING: " + w)
        for e in errors:
            notes.append("CONFIG ERROR: " + e)

        if action == "Stop worker":
            with open(_p("stop"), "w", encoding="utf-8") as f:
                f.write("stop")
            _log_event("stop_requested", environment=self.environment)
            notes.append("Stop requested. The current item returns to the queue.")
            return Message(text=self._status_text(notes))

        if action == "Reset state":
            if (self.confirm_reset or "").strip() != "RESET":
                notes.append("Reset blocked: type RESET in 'Confirm Reset' to clear state.")
                return Message(text=self._status_text(notes))
            with open(_p("stop"), "w", encoding="utf-8") as f:
                f.write("stop")
            with _FileLock("state.lock"):
                _save_state(_empty_state())
            _log_event("state_reset", environment=self.environment)
            return Message(text="State cleared. Clear the 'Confirm Reset' field and set Action back to Poll and dispatch.")

        snow = None
        if action in ("Poll and dispatch", "SNOW health check", "Run SNOW cleanup now"):
            cleanup = action == "Run SNOW cleanup now" or (action == "Poll and dispatch" and bool(self.cleanup_enabled))
            snow, snow_notes = self._snow_check(cleanup)
            notes += snow_notes

        if action == "Poll and dispatch":
            notes += self._poll(snow)
            if not self.dispatch_enabled:
                notes.append("DRY RUN: Dispatch Enabled is off, so no audits are started.")
            elif errors:
                notes.append("Dispatch blocked by the configuration errors above.")
            else:
                self._publish_cfg()
                try:
                    os.remove(_p("stop"))
                except Exception:
                    pass
                if _start_worker():
                    notes.append("Worker started.")
                    _log_event("worker_started", environment=self.environment)

        return Message(text=self._status_text(notes))
