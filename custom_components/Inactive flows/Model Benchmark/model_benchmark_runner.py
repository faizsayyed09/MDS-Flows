import io
import os
import re
import csv
import json
import time
import random
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, StrInput, SecretStrInput, IntInput, DropdownInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


_BENCH_DIR = os.path.join(tempfile.gettempdir(), "qa_model_benchmark")
_RESULTS_FILE = os.path.join(_BENCH_DIR, "results.jsonl")
_PINNED_FILE = os.path.join(_BENCH_DIR, "pinned_calls.json")
_WAIT_MODE = "wait until done (webhook/production)"
_MAX_ATTEMPTS = 2
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()

RATE_CARD = {
    "Gemini 2.5 Flash-Lite": [0.10, 0.40],
    "Gemini 2.5 Flash": [0.30, 2.50],
    "Gemini 3 Flash": [0.50, 3.00],
    "Gemini 3.5 Flash": [0.50, 3.00],
    "Gemini 3.8 Flash": [1.50, 7.50],
    "Gemini 2.5 Pro": [1.25, 10.00],
    "Gemini 3.1 Pro": [2.00, 12.00],
    "Claude Haiku 4.5": [1.00, 5.00],
    "Claude Sonnet 4.5": [3.00, 15.00],
    "Claude Sonnet 4.6": [3.00, 15.00],
    "Claude Sonnet 5": [2.00, 10.00],
    "Claude Opus 4.5": [5.00, 25.00],
    "GPT-5.4 Nano": [0.20, 1.25],
    "GPT-5.1 Codex Mini": [0.25, 2.00],
    "GPT-5": [1.25, 10.00],
    "GPT-5.1": [1.25, 10.00],
    "GPT-5.2": [1.75, 14.00],
    "GPT-5.4": [1.75, 14.00],
    "GPT 4.1": [2.00, 8.00],
    "GPT 4.1 Mini": [2.00, 8.00],
    "OpenAI o3-mini": [1.10, 4.40],
    "OpenAI o3": [2.00, 8.00],
}
MODEL_OPTIONS = list(RATE_CARD.keys())

ACTIONS = [
    "Run / resume benchmark",
    "Show summary only",
    "Fetch new call set",
    "Stop current run",
    "Delete results for this run label",
]


# ---------------- file helpers ----------------

def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, default=str)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def _read_jsonl(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-") or "run"


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_genesys(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------- call selection ----------------

def _day_window(choice):
    now = datetime.now(timezone.utc)
    if choice == "last 24 hours":
        return now - timedelta(hours=24), now
    tz = timezone.utc
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = timezone.utc
    local_now = now.astimezone(tz)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if choice == "yesterday (ET)":
        start_local = start_local - timedelta(days=1)
        end_local = start_local + timedelta(days=1)
    else:
        end_local = local_now
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _genesys_headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


_QUEUE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _resolve_queue_ids(token, region, entries):
    ids = []
    missing = []
    url = "https://api." + region + "/api/v2/routing/queues"
    for entry in entries:
        if _QUEUE_UUID_RE.match(entry):
            ids.append(entry)
            continue
        try:
            resp = requests.get(
                url,
                headers=_genesys_headers(token),
                params={"name": entry, "pageSize": 25},
                timeout=20,
            )
        except Exception as e:
            missing.append(entry + " (" + str(e)[:80] + ")")
            continue
        if resp.status_code >= 400:
            missing.append(entry + " (HTTP " + str(resp.status_code) + ")")
            continue
        match = None
        for q in (resp.json() or {}).get("entities") or []:
            if str(q.get("name", "")).strip().lower() == entry.lower():
                match = q.get("id")
                break
        if match:
            ids.append(match)
        else:
            missing.append(entry + " (not found)")
    return ids, missing


def _query_conversations(token, region, start, end, queue_ids):
    url = "https://api." + region + "/api/v2/analytics/conversations/details/query"
    filters = [{"type": "and", "predicates": [{"dimension": "mediaType", "value": "voice"}]}]
    if queue_ids:
        filters.append({
            "type": "or",
            "predicates": [{"dimension": "queueId", "value": q} for q in queue_ids],
        })
    conversations = []
    page = 1
    while page <= 10:
        body = {
            "interval": _fmt_genesys(start) + "/" + _fmt_genesys(end),
            "order": "asc",
            "orderBy": "conversationStart",
            "paging": {"pageSize": 100, "pageNumber": page},
            "segmentFilters": filters,
        }
        resp = requests.post(url, headers=_genesys_headers(token), json=body, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError("HTTP " + str(resp.status_code) + ": " + (resp.text or "")[:300])
        data = resp.json() if (resp.text or "").strip() else {}
        batch = data.get("conversations") or []
        conversations.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return conversations


def _has_audio(token, region, conv_id):
    url = "https://api." + region + "/api/v2/conversations/" + conv_id + "/recordings"
    try:
        resp = requests.get(url, headers=_genesys_headers(token), params={"maxWaitMs": 1000}, timeout=15)
    except Exception:
        return False
    if resp.status_code != 200 or not (resp.text or "").strip():
        return False
    try:
        recs = resp.json()
    except ValueError:
        return False
    if isinstance(recs, dict):
        recs = recs.get("entities") or [recs]
    for r in recs:
        if r.get("media") == "audio" and r.get("mediaUris"):
            return True
    return False


def _candidates(conversations, min_sec, max_sec):
    out = []
    stats = {
        "total": len(conversations),
        "no_end": 0,
        "too_short": 0,
        "too_long": 0,
        "no_agent": 0,
    }
    for c in conversations:
        conv_id = c.get("conversationId")
        start = _parse_ts(c.get("conversationStart"))
        end = _parse_ts(c.get("conversationEnd"))
        if not conv_id or not start or not end:
            stats["no_end"] += 1
            continue
        duration = (end - start).total_seconds()
        if duration < min_sec:
            stats["too_short"] += 1
            continue
        if max_sec and duration > max_sec:
            stats["too_long"] += 1
            continue
        agent = ""
        agent_id = ""
        for p in c.get("participants") or []:
            if p.get("purpose") in ("agent", "user"):
                if not agent_id and p.get("userId"):
                    agent_id = p.get("userId")
                if p.get("participantName"):
                    agent = p.get("participantName")
                    break
        if not agent and not agent_id:
            stats["no_agent"] += 1
            continue
        out.append({
            "conversation_id": conv_id,
            "start": c.get("conversationStart"),
            "duration_sec": int(duration),
            "agent": agent or ("user " + agent_id[:8]),
        })
    return out, stats


def _select_calls(candidates, count, max_per_agent, seed, audio_check, time_budget_sec=60):
    if not candidates:
        return []
    rnd = random.Random(seed)
    ordered = sorted(candidates, key=lambda c: c["duration_sec"])
    step = len(ordered) / float(max(1, count))
    spread = [min(len(ordered) - 1, int(i * step)) for i in range(count)]
    spread_set = set(spread)
    rest = [i for i in range(len(ordered)) if i not in spread_set]
    rnd.shuffle(rest)

    shortlist = []
    seen = set()
    per_agent = {}
    for i in spread + rest:
        if len(shortlist) >= count * 2:
            break
        c = ordered[i]
        if c["conversation_id"] in seen:
            continue
        seen.add(c["conversation_id"])
        if per_agent.get(c["agent"], 0) >= max_per_agent:
            continue
        shortlist.append(c)
        per_agent[c["agent"]] = per_agent.get(c["agent"], 0) + 1

    deadline = time.time() + time_budget_sec
    ok = {}
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futures = [(pool.submit(audio_check, c["conversation_id"]), c) for c in shortlist]
        for fut, c in futures:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                ok[c["conversation_id"]] = bool(fut.result(timeout=remaining))
            except Exception:
                ok[c["conversation_id"]] = False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    picked = [c for c in shortlist if ok.get(c["conversation_id"])][:count]
    picked.sort(key=lambda c: c["duration_sec"])
    return picked


# ---------------- MDS / Langflow run API ----------------

def _mds_headers(key_header, key):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        key = key.strip()
        headers["x-api-key"] = key
        custom = (key_header or "").strip()
        if custom and custom.lower() != "x-api-key":
            headers[custom] = key
    return headers


def _build_run_url(base, flow, run_url):
    run_url = (run_url or "").strip()
    if run_url:
        return run_url
    return base.rstrip("/") + "/api/v1/run/" + flow.strip() + "?stream=false"


def _run_flow(url, headers, tweaks, timeout_sec, session_id):
    body = {
        "output_type": "chat",
        "input_type": "text",
        "input_value": "benchmark",
        "tweaks": tweaks,
        "session_id": session_id,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=timeout_sec)
    return resp.status_code, (resp.text or "")[:400]


def _has_result(label, conv_id, repeat, since):
    for r in _read_jsonl(_RESULTS_FILE):
        if r.get("type") != "result":
            continue
        if r.get("run_label") != label or r.get("conversation_id") != conv_id:
            continue
        if int(r.get("repeat") or 1) != int(repeat):
            continue
        if float(r.get("logged_epoch") or 0) >= since:
            return True
    return False


# ---------------- background worker ----------------

def _worker(slug, job_path, cfg):
    stop_path = job_path + ".stop"
    job = _read_json(job_path, {}) or {}
    try:
        job["state"] = "running"
        job["run_url_used"] = cfg["run_url"]
        _write_json(job_path, job)
        stopped = False
        while True:
            if os.path.exists(stop_path):
                stopped = True
                break
            item = None
            for candidate in job.get("items", []):
                if candidate["state"] in ("pending", "error") and candidate.get("attempts", 0) < _MAX_ATTEMPTS:
                    item = candidate
                    break
            if item is None:
                break

            item["state"] = "running"
            item["attempts"] = item.get("attempts", 0) + 1
            item["started"] = time.time()
            job["current"] = item["conversation_id"] + " (repeat " + str(item["repeat"]) + ")"
            _write_json(job_path, job)

            logger_cfg = dict(cfg["logger_cfg"])
            logger_cfg["repeat"] = item["repeat"]
            tweaks = {
                cfg["ids"]["downloader"]: {"conversation_id": item["conversation_id"]},
                cfg["ids"]["transcriber"]: {"run_mode": _WAIT_MODE, "max_wait_min": cfg["timeout_min"]},
                cfg["ids"]["logger"]: {"run_config": json.dumps(logger_cfg), "results_path": _RESULTS_FILE},
            }
            if cfg["ids"].get("sender"):
                tweaks[cfg["ids"]["sender"]] = {"send_enabled": False}

            session_id = "bench-" + cfg["label"][:40] + "-" + item["conversation_id"][:8] + "-r" + str(item["repeat"])

            status = None
            body = ""
            error = None
            try:
                status, body = _run_flow(
                    cfg["run_url"], cfg["headers"], tweaks, cfg["timeout_min"] * 60 + 120, session_id
                )
            except Exception as e:
                error = str(e)[:300]

            seconds = round(time.time() - item["started"], 1)
            if _has_result(cfg["label"], item["conversation_id"], item["repeat"], item["started"] - 5):
                item["state"] = "done"
                item["error"] = None
            else:
                item["state"] = "error"
                if error:
                    item["error"] = error
                elif status is not None and not (200 <= status < 300):
                    item["error"] = "HTTP " + str(status) + ": " + body[:200]
                else:
                    item["error"] = "Flow finished but the Test Run Logger wrote no row. " + body[:150]
            item["seconds"] = seconds

            _append_jsonl(_RESULTS_FILE, {
                "type": "run",
                "run_label": cfg["label"],
                "conversation_id": item["conversation_id"],
                "repeat": item["repeat"],
                "ok": item["state"] == "done",
                "seconds": seconds,
                "error": item.get("error"),
                "logged_epoch": time.time(),
            })
            _write_json(job_path, job)

        job["state"] = "stopped" if stopped else "done"
        job["current"] = ""
        job["finished"] = time.time()
        _write_json(job_path, job)
    except Exception as e:
        job["state"] = "crashed"
        job["crash_error"] = str(e)[:300]
        _write_json(job_path, job)
    finally:
        try:
            os.remove(stop_path)
        except Exception:
            pass
        with _WORKERS_LOCK:
            _WORKERS.pop(slug, None)


# ---------------- summary ----------------

def _norm_result(value):
    s = re.sub(r"[^A-Z]", "", str(value or "").upper())
    if s in ("MET", "YES", "PASS"):
        return "MET"
    if s in ("NOTMET", "NO", "MISSED", "FAIL"):
        return "NOT_MET"
    if s in ("NA", "NOTAPPLICABLE"):
        return "NA"
    return s


def _norm_verdict(value):
    s = re.sub(r"[^A-Z]", "", str(value or "").upper())
    if s in ("ACHIEVED", "PASS", "PASSED"):
        return "ACHIEVED"
    if s in ("NOTACHIEVED", "FAIL", "FAILED", "NOTPASSED"):
        return "NOT_ACHIEVED"
    if s.startswith("CRITICAL"):
        return "CRITICAL_FAIL"
    return s


def _attrs(row):
    return {k: _norm_result(v) for k, v in (row or {}).items() if str(k).startswith("result_")}


def _parse_analyst(text):
    out = {}
    text = (text or "").strip()
    if not text:
        return out
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        conv = (r.get("conversation_id") or "").strip().lower()
        if not conv:
            continue
        attrs = {}
        for k, v in r.items():
            if k and k.strip().startswith("result_") and v and v.strip():
                attrs[k.strip()] = _norm_result(v)
        out[conv] = {"verdict": _norm_verdict(r.get("verdict")), "attrs": attrs}
    return out


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _fmt_pct(value):
    return "-" if value is None else str(value) + "%"


def _compare(by_conv, other):
    calls = 0
    verdict_same = 0
    attr_same = 0
    attr_total = 0
    for conv, reps in by_conv.items():
        if conv not in other:
            continue
        mine = reps[min(reps)].get("row") or {}
        theirs = other[conv]
        calls += 1
        if _norm_verdict(mine.get("verdict")) == _norm_verdict(theirs.get("verdict")):
            verdict_same += 1
        mine_attrs = _attrs(mine)
        for key, value in (theirs.get("attrs") or {}).items():
            if key in mine_attrs:
                attr_total += 1
                if mine_attrs[key] == value:
                    attr_same += 1
    return {
        "calls": calls,
        "verdict_pct": _pct(verdict_same, calls),
        "attribute_pct": _pct(attr_same, attr_total),
    }


def _build_summary(reference, analyst_text, calls_per_day, agree_target, consistency_target):
    results = {}
    runs = {}
    for r in _read_jsonl(_RESULTS_FILE):
        key = (r.get("run_label"), r.get("conversation_id"), int(r.get("repeat") or 1))
        if r.get("type") == "result":
            results[key] = r
        elif r.get("type") == "run":
            runs[key] = r

    labels = sorted(set(k[0] for k in results) | set(k[0] for k in runs))
    if not labels:
        return "No benchmark results yet.", []

    by_label = {}
    for (lab, conv, rep), r in results.items():
        by_label.setdefault(lab, {}).setdefault(conv, {})[rep] = r

    reference_map = {}
    if reference and reference in by_label:
        for conv, reps in by_label[reference].items():
            row = reps[min(reps)].get("row") or {}
            reference_map[conv] = {"verdict": row.get("verdict"), "attrs": _attrs(row)}

    analyst = _parse_analyst(analyst_text)

    stats = []
    for lab in labels:
        convs = by_label.get(lab, {})
        records = [r for reps in convs.values() for r in reps.values()]
        failures = [k for k, r in runs.items() if k[0] == lab and not r.get("ok") and k not in results]
        ok_seconds = [r.get("seconds") for k, r in runs.items() if k[0] == lab and r.get("ok") and r.get("seconds")]

        costs = [float(r.get("total_cost_usd") or 0) for r in records]
        avg_cost = sum(costs) / len(costs) if costs else 0.0
        mission_costs = {}
        for m in ("snow", "kb", "ceq"):
            vals = [float(((r.get("missions") or {}).get(m) or {}).get("cost_usd") or 0) for r in records]
            mission_costs[m] = round(sum(vals) / len(vals), 5) if vals else 0.0

        verdicts = {}
        scores = []
        parse_errors = 0
        matched = 0
        confidences = []
        for r in records:
            row = r.get("row") or {}
            verdict = str(row.get("verdict") or "NONE")
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if row.get("score_pct") is not None:
                scores.append(_to_float(row.get("score_pct"), 0.0))
            if verdict == "PARSE_ERROR" or row.get("ceq_parse_error"):
                parse_errors += 1
            if str(row.get("ticket_match_status")) == "MATCHED":
                matched += 1
            if r.get("transcript_confidence") is not None:
                confidences.append(_to_float(r.get("transcript_confidence"), 0.0))

        cons_calls = 0
        cons_same = 0
        attr_same = 0
        attr_total = 0
        for reps in convs.values():
            if len(reps) < 2:
                continue
            cons_calls += 1
            verdict_set = set(_norm_verdict((x.get("row") or {}).get("verdict")) for x in reps.values())
            if len(verdict_set) == 1:
                cons_same += 1
            attr_sets = [_attrs(x.get("row")) for x in reps.values()]
            keys = set()
            for a in attr_sets:
                keys |= set(a)
            for k in keys:
                attr_total += 1
                if len(set(a.get(k) for a in attr_sets)) == 1:
                    attr_same += 1

        first = records[0] if records else {}
        stats.append({
            "label": lab,
            "models": first.get("models") or {},
            "calls": len(convs),
            "audits": len(records),
            "failures": len(failures),
            "avg_seconds": round(sum(ok_seconds) / len(ok_seconds), 1) if ok_seconds else None,
            "avg_cost": round(avg_cost, 5),
            "mission_costs": mission_costs,
            "monthly_cost": round(avg_cost * calls_per_day * 30, 2),
            "verdicts": verdicts,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "parse_errors": parse_errors,
            "ticket_match_pct": _pct(matched, len(records)),
            "transcript_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "consistency": {
                "calls": cons_calls,
                "verdict_pct": _pct(cons_same, cons_calls),
                "attribute_pct": _pct(attr_same, attr_total),
            },
            "reference": _compare(convs, reference_map) if reference_map and lab != reference else None,
            "analyst": _compare(convs, analyst) if analyst else None,
        })

    lines = ["=== RESULTS BY RUN LABEL ==="]
    for s in stats:
        models = s["models"]
        lines.append("")
        lines.append(
            "- " + s["label"] + "   (SNOW " + str(models.get("snow")) + " | KB " + str(models.get("kb"))
            + " | CEQ " + str(models.get("ceq")) + ")"
        )
        lines.append(
            "  Calls " + str(s["calls"]) + ", audits " + str(s["audits"]) + ", failures " + str(s["failures"])
            + ((", avg " + str(s["avg_seconds"]) + "s per audit") if s["avg_seconds"] else "")
        )
        mc = s["mission_costs"]
        lines.append(
            "  Cost: $" + str(s["avg_cost"]) + " per audit (SNOW $" + str(mc["snow"]) + ", KB $" + str(mc["kb"])
            + ", CEQ $" + str(mc["ceq"]) + ") -> ~$" + str(s["monthly_cost"]) + "/month at "
            + str(calls_per_day) + " calls/day"
        )
        verdict_text = ", ".join(k + " " + str(v) for k, v in sorted(s["verdicts"].items()))
        lines.append(
            "  Verdicts: " + verdict_text + " | avg score " + _fmt_pct(s["avg_score"])
            + " | parse errors " + str(s["parse_errors"])
        )
        lines.append(
            "  Ticket matched: " + _fmt_pct(s["ticket_match_pct"])
            + " | transcript confidence " + str(s["transcript_confidence"])
        )
        c = s["consistency"]
        if c["calls"]:
            lines.append(
                "  Consistency (same call repeated): verdict " + _fmt_pct(c["verdict_pct"])
                + " | attributes " + _fmt_pct(c["attribute_pct"]) + " (" + str(c["calls"]) + " calls)"
            )
        if s["reference"]:
            r = s["reference"]
            lines.append(
                "  vs reference '" + reference + "': verdict " + _fmt_pct(r["verdict_pct"])
                + " | attributes " + _fmt_pct(r["attribute_pct"]) + " (" + str(r["calls"]) + " calls)"
            )
        if s["analyst"]:
            a = s["analyst"]
            lines.append(
                "  vs CEQ analyst: verdict " + _fmt_pct(a["verdict_pct"])
                + " | attributes " + _fmt_pct(a["attribute_pct"]) + " (" + str(a["calls"]) + " calls)"
            )

    basis = None
    if any(s["analyst"] and s["analyst"]["calls"] >= 5 for s in stats):
        basis = "analyst"
    elif reference and reference in by_label:
        basis = "reference"

    lines.append("")
    lines.append("=== RECOMMENDATION ===")
    if basis is None:
        lines.append(
            "Set 'Reference Run Label' to your strongest setup, or paste analyst scores, to get a recommendation."
        )
    else:
        eligible = []
        for s in stats:
            if not s["audits"]:
                continue
            if basis == "analyst":
                agree = (s["analyst"] or {}).get("verdict_pct")
            elif s["label"] == reference:
                agree = 100.0
            else:
                agree = (s["reference"] or {}).get("verdict_pct")
            consistency = s["consistency"]["verdict_pct"]
            if agree is None or agree < agree_target:
                continue
            if consistency is not None and consistency < consistency_target:
                continue
            if s["parse_errors"] > 0:
                continue
            eligible.append((s["avg_cost"], s["label"], agree, consistency))
        if eligible:
            eligible.sort()
            cost, lab, agree, consistency = eligible[0]
            lines.append(
                "Cheapest setup meeting targets (" + basis + " agreement >= " + str(agree_target)
                + "%, consistency >= " + str(consistency_target) + "%, no parse errors): " + lab
                + " at $" + str(cost) + " per audit."
            )
        else:
            lines.append("No setup meets the targets yet. Check the per-label results above.")
        if basis == "reference":
            lines.append(
                "Note: agreement with a reference model measures closeness to that model, not true accuracy. "
                "Paste analyst scores for real accuracy."
            )
    return "\n".join(lines), stats


# ---------------- component ----------------

class CustomComponent(Component):
    display_name = "Model Benchmark Runner"
    description = "Picks a pinned set of calls, runs each through the test audit flow for the current model setup, and reports cost, consistency and agreement per run label."

    inputs = [
        DropdownInput(
            name="action",
            display_name="Action",
            options=ACTIONS,
            value="Run / resume benchmark",
        ),
        DropdownInput(
            name="snow_model",
            display_name="SNOW Mission LLM",
            options=MODEL_OPTIONS,
            value="Gemini 3.5 Flash",
            info="Must match the LLM currently selected in the SNOW mission.",
        ),
        DropdownInput(
            name="kb_model",
            display_name="KB Mission LLM",
            options=MODEL_OPTIONS,
            value="Gemini 3.5 Flash",
            info="Must match the LLM currently selected in the KB mission.",
        ),
        DropdownInput(
            name="ceq_model",
            display_name="CEQ Mission LLM",
            options=MODEL_OPTIONS,
            value="Gemini 3.8 Flash",
            info="Must match the LLM currently selected in the CEQ mission.",
        ),
        StrInput(
            name="run_label",
            display_name="Run Label (optional)",
            value="",
            info="Blank = built automatically from the three model names.",
        ),
        IntInput(
            name="repeats",
            display_name="Repeats per Call",
            value=2,
            info="2 = each call audited twice to measure consistency.",
        ),
        StrInput(
            name="reference_label",
            display_name="Reference Run Label",
            value="",
            info="Every run is compared against this one. Use your strongest setup's label.",
        ),
        DataInput(
            name="auth",
            display_name="Genesys Auth",
            required=False,
            info="Output of the Genesys Cloud Auth component. Needed to pick calls.",
        ),
        IntInput(
            name="call_count",
            display_name="Calls in Test Set",
            value=15,
        ),
        DropdownInput(
            name="call_window",
            display_name="Pick Calls From",
            options=["today (ET)", "yesterday (ET)", "last 24 hours"],
            value="today (ET)",
        ),
        StrInput(
            name="mds_base_url",
            display_name="MDS Base URL",
            value="https://matchaflow.harriscomputer.com",
            info="Base URL of the Langflow / MDS server.",
        ),
        StrInput(
            name="flow_id",
            display_name="Test Audit Flow ID or Name",
            value="",
        ),
        StrInput(
            name="run_url",
            display_name="Run URL (optional override)",
            value="",
            info="Exact run URL from the test flow's API access panel. Blank = <base>/api/v1/run/<flow_id>?stream=false",
            advanced=True,
        ),
        SecretStrInput(
            name="api_key",
            display_name="MDS API Key",
            value="",
            required=False,
            info="Key created from the test flow's API access panel.",
        ),
        StrInput(
            name="api_key_header",
            display_name="API Key Header",
            value="x-api-key",
            advanced=True,
        ),
        MultilineInput(
            name="component_ids",
            display_name="Component IDs (optional override)",
            value="",
            info="One per line: downloader=ID, transcriber=ID, logger=ID, sender=ID",
            advanced=True,
        ),
        IntInput(
            name="min_duration_sec",
            display_name="Min Call Duration (seconds)",
            value=60,
            advanced=True,
        ),
        IntInput(
            name="max_duration_sec",
            display_name="Max Call Duration (seconds)",
            value=1800,
            advanced=True,
        ),
        IntInput(
            name="max_per_agent",
            display_name="Max Calls per Agent",
            value=3,
            advanced=True,
        ),
        StrInput(
            name="queue_ids",
            display_name="Queue IDs (optional)",
            value="",
            info="Queue names or IDs, comma-separated, e.g. GSD_MQ, GSD_OF, EIS_CSC, EIS_TSG",
            advanced=True,
        ),
        IntInput(
            name="per_call_timeout_min",
            display_name="Per-Call Timeout (minutes)",
            value=35,
            advanced=True,
        ),
        IntInput(
            name="ui_wait_sec",
            display_name="Wait Before Showing Progress (seconds)",
            value=60,
            advanced=True,
        ),
        IntInput(
            name="snow_hidden_tokens",
            display_name="SNOW Hidden Tokens (estimate)",
            value=8000,
            info="Tokens Matcha adds server-side: system context plus retrieved records.",
            advanced=True,
        ),
        IntInput(
            name="kb_hidden_tokens",
            display_name="KB Hidden Tokens (estimate)",
            value=4000,
            advanced=True,
        ),
        IntInput(
            name="ceq_hidden_tokens",
            display_name="CEQ Hidden Tokens (estimate)",
            value=3000,
            advanced=True,
        ),
        IntInput(
            name="calls_per_day",
            display_name="Calls per Day (for monthly cost)",
            value=100,
            advanced=True,
        ),
        StrInput(
            name="agreement_target",
            display_name="Agreement Target (%)",
            value="90",
            advanced=True,
        ),
        StrInput(
            name="consistency_target",
            display_name="Consistency Target (%)",
            value="90",
            advanced=True,
        ),
        MultilineInput(
            name="analyst_scores",
            display_name="Analyst Scores CSV (optional)",
            value="",
            info="CSV with columns conversation_id, verdict and optionally result_<attribute> (MET / NOT_MET / NA).",
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Report", name="report", method="build_report"),
        Output(display_name="Results", name="results", method="get_bench_results"),
    ]

    # ---------------- helpers ----------------

    def _auth_data(self):
        auth = self.auth
        if isinstance(auth, list) and auth:
            auth = auth[0]
        if hasattr(auth, "data"):
            return auth.data or {}
        if isinstance(auth, dict):
            return auth
        return {}

    def _label(self):
        label = (self.run_label or "").strip()
        if label:
            return label
        return _slug(self.snow_model) + "__" + _slug(self.kb_model) + "__" + _slug(self.ceq_model)

    def _resolve_ids(self):
        ids = {}
        for line in (self.component_ids or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() and v.strip():
                    ids[k.strip().lower()] = v.strip()
        missing = [k for k in ("downloader", "transcriber", "logger") if k not in ids]
        if missing:
            return ids, (
                "Missing component IDs: " + ", ".join(missing)
                + ". Fill 'Component IDs' using the tweaks keys from the test flow's API access panel."
            )
        return ids, None

    def _ensure_call_set(self, force):
        pinned = _read_json(_PINNED_FILE, None)
        if pinned and pinned.get("calls") and not force:
            return pinned, None
        auth = self._auth_data()
        if auth.get("error") or not auth.get("access_token"):
            return None, "Genesys auth is missing or failed. Connect the Genesys Cloud Auth component with a valid token."
        token = auth["access_token"]
        region = auth.get("region", "mypurecloud.com")
        start, end = _day_window(self.call_window)
        queue_entries = [
            q.strip() for q in (self.queue_ids or "").replace("\n", ",").split(",") if q.strip()
        ]
        queues = []
        if queue_entries:
            queues, missing = _resolve_queue_ids(token, region, queue_entries)
            if not queues:
                return None, (
                    "Could not resolve any queue: " + "; ".join(missing)
                    + ". Paste the queue IDs instead."
                )
        try:
            conversations = _query_conversations(token, region, start, end, queues)
        except Exception as e:
            return None, "Genesys call search failed: " + str(e)[:300]
        candidates, cstats = _candidates(
            conversations, int(self.min_duration_sec or 60), int(self.max_duration_sec or 0)
        )
        if not candidates:
            return None, (
                "No usable calls for '" + str(self.call_window) + "' (UTC "
                + _fmt_genesys(start) + " to " + _fmt_genesys(end) + "). "
                + "Genesys returned " + str(cstats["total"]) + " voice conversations: "
                + str(cstats["no_end"]) + " not ended yet, "
                + str(cstats["too_short"]) + " shorter than Min Call Duration, "
                + str(cstats["too_long"]) + " longer than Max Call Duration, "
                + str(cstats["no_agent"]) + " with no agent participant."
            )
        picked = _select_calls(
            candidates,
            max(1, int(self.call_count or 15)),
            max(1, int(self.max_per_agent or 3)),
            int(time.time()),
            lambda conv_id: _has_audio(token, region, conv_id),
        )
        if not picked:
            return None, "Found " + str(len(candidates)) + " calls, but none had an available audio recording."
        pinned = {
            "set_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "window": self.call_window,
            "candidates_found": len(candidates),
            "calls": picked,
        }
        _write_json(_PINNED_FILE, pinned)
        return pinned, None

    def _progress_text(self, label, job, alive):
        items = job.get("items") or []
        total = len(items)
        done = [i for i in items if i["state"] == "done"]
        failed = [i for i in items if i["state"] == "error" and i.get("attempts", 0) >= _MAX_ATTEMPTS]
        secs = [i.get("seconds") for i in done if i.get("seconds")]
        avg = sum(secs) / len(secs) if secs else None
        remaining = total - len(done) - len(failed)
        state = "RUNNING" if alive else str(job.get("state") or "idle").upper()
        lines = [
            "BENCHMARK " + state + ": " + label,
            "Run URL used: " + str(job.get("run_url_used") or "-"),
            "Progress: " + str(len(done)) + "/" + str(total) + " audits done, " + str(len(failed)) + " failed",
        ]
        if alive and job.get("current"):
            lines.append("Now running: " + str(job.get("current")))
        if alive and avg and remaining > 0:
            lines.append("Estimated time left: ~" + str(int(avg * remaining / 60) + 1) + " min")
        for f in failed[:5]:
            lines.append(
                "  Failed: " + f["conversation_id"] + " (repeat " + str(f["repeat"]) + ") - " + str(f.get("error"))
            )
        if job.get("crash_error"):
            lines.append("Worker error: " + str(job.get("crash_error")))
        if alive:
            lines.append("Re-run this component any time to see progress. It keeps running if you close the browser.")
        elif failed:
            lines.append("Run again to retry failed calls (for example after refreshing an expired token).")
        return "\n".join(lines)

    def _summary(self):
        return _build_summary(
            (self.reference_label or "").strip(),
            self.analyst_scores,
            max(1, int(self.calls_per_day or 100)),
            _to_float(self.agreement_target, 90.0),
            _to_float(self.consistency_target, 90.0),
        )

    # ---------------- main ----------------

    def _bench_execute(self):
        if getattr(self, "_exec_cache", None) is not None:
            return self._exec_cache

        os.makedirs(_BENCH_DIR, exist_ok=True)
        action = self.action or ACTIONS[0]
        label = self._label()
        slug = _slug(label)
        job_path = os.path.join(_BENCH_DIR, "job_" + slug + ".json")

        if action == "Stop current run":
            with open(job_path + ".stop", "w", encoding="utf-8") as f:
                f.write("stop")
            self._exec_cache = ("Stop requested for '" + label + "'. It stops after the current call finishes.", {})
            return self._exec_cache

        if action == "Delete results for this run label":
            kept = [r for r in _read_jsonl(_RESULTS_FILE) if r.get("run_label") != label]
            tmp = _RESULTS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(json.dumps(r, default=str) + "\n")
            os.replace(tmp, _RESULTS_FILE)
            try:
                os.remove(job_path)
            except Exception:
                pass
            self._exec_cache = ("Deleted all results for '" + label + "'.", {})
            return self._exec_cache

        if action == "Fetch new call set":
            pinned, err = self._ensure_call_set(force=True)
            if err:
                self._exec_cache = ("ERROR: " + err, {})
                return self._exec_cache
            lines = [
                "New call set pinned (" + pinned["set_id"] + "): " + str(len(pinned["calls"])) + " calls from "
                + str(pinned["candidates_found"]) + " candidates.",
                "Note: run labels tested on the old set will start over on this set.",
            ]
            for c in pinned["calls"]:
                lines.append(
                    "  " + c["conversation_id"] + " | " + c["agent"] + " | " + str(c["duration_sec"]) + "s"
                )
            self._exec_cache = ("\n".join(lines), {"call_set": pinned})
            return self._exec_cache

        if action == "Show summary only":
            text, stats = self._summary()
            self._exec_cache = (text, {"labels": stats, "call_set": _read_json(_PINNED_FILE, {})})
            return self._exec_cache

        # ---- Run / resume ----
        notes = []
        with _WORKERS_LOCK:
            alive = slug in _WORKERS and _WORKERS[slug].is_alive()

        if not alive:
            base = (self.mds_base_url or "").strip()
            flow = (self.flow_id or "").strip()
            if not (self.run_url or "").strip() and (not base or not flow):
                self._exec_cache = ("ERROR: set 'MDS Base URL' and 'Test Audit Flow ID', or 'Run URL'.", {})
                return self._exec_cache
            if not (self.api_key or "").strip():
                self._exec_cache = ("ERROR: set 'MDS API Key' (from the test flow's API access panel).", {})
                return self._exec_cache

            run_url = _build_run_url(base, flow, self.run_url)
            headers = _mds_headers(self.api_key_header, self.api_key)

            ids, err = self._resolve_ids()
            if err:
                self._exec_cache = ("ERROR: " + err, {})
                return self._exec_cache

            pinned, err = self._ensure_call_set(force=False)
            if err:
                self._exec_cache = ("ERROR: " + err, {})
                return self._exec_cache

            job = _read_json(job_path, None)
            repeats = max(1, int(self.repeats or 1))
            if not job or job.get("set_id") != pinned.get("set_id"):
                if job:
                    notes.append("Call set changed, so this run label starts over on the current set.")
                items = []
                for rep in range(1, repeats + 1):
                    for c in pinned["calls"]:
                        items.append({
                            "conversation_id": c["conversation_id"],
                            "repeat": rep,
                            "state": "pending",
                            "attempts": 0,
                            "agent": c["agent"],
                            "duration_sec": c["duration_sec"],
                        })
                job = {
                    "label": label,
                    "set_id": pinned["set_id"],
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "state": "pending",
                    "items": items,
                }
            else:
                for it in job["items"]:
                    if it["state"] in ("running", "error"):
                        it["state"] = "pending"
                        it["attempts"] = 0
                        it["error"] = None

            job["models"] = {"snow": self.snow_model, "kb": self.kb_model, "ceq": self.ceq_model}
            job["run_url_used"] = run_url
            job.pop("crash_error", None)
            _write_json(job_path, job)

            pending = [i for i in job["items"] if i["state"] != "done"]
            if pending:
                snow_price = RATE_CARD.get(self.snow_model, [0, 0])
                kb_price = RATE_CARD.get(self.kb_model, [0, 0])
                ceq_price = RATE_CARD.get(self.ceq_model, [0, 0])
                cfg = {
                    "run_url": run_url,
                    "headers": headers,
                    "ids": ids,
                    "label": label,
                    "timeout_min": max(5, int(self.per_call_timeout_min or 35)),
                    "logger_cfg": {
                        "run_label": label,
                        "snow_model": self.snow_model,
                        "kb_model": self.kb_model,
                        "ceq_model": self.ceq_model,
                        "snow_in_price": snow_price[0],
                        "snow_out_price": snow_price[1],
                        "kb_in_price": kb_price[0],
                        "kb_out_price": kb_price[1],
                        "ceq_in_price": ceq_price[0],
                        "ceq_out_price": ceq_price[1],
                        "snow_hidden_tokens": int(self.snow_hidden_tokens or 0),
                        "kb_hidden_tokens": int(self.kb_hidden_tokens or 0),
                        "ceq_hidden_tokens": int(self.ceq_hidden_tokens or 0),
                    },
                }
                try:
                    os.remove(job_path + ".stop")
                except Exception:
                    pass
                thread = threading.Thread(target=_worker, args=(slug, job_path, cfg), daemon=True)
                with _WORKERS_LOCK:
                    _WORKERS[slug] = thread
                thread.start()
                alive = True
                notes.append(
                    "Started: " + str(len(pending)) + " audits for '" + label + "' on call set " + pinned["set_id"] + "."
                )
            else:
                notes.append("All audits for '" + label + "' are already done.")

        deadline = time.time() + max(0, int(self.ui_wait_sec or 60))
        while alive and time.time() < deadline:
            time.sleep(3)
            with _WORKERS_LOCK:
                alive = slug in _WORKERS and _WORKERS[slug].is_alive()

        job = _read_json(job_path, {}) or {}
        progress = self._progress_text(label, job, alive)
        summary_text, stats = self._summary()
        text = "\n".join(notes + [progress, "", summary_text])
        self._exec_cache = (text, {"progress": progress, "labels": stats, "call_set": _read_json(_PINNED_FILE, {})})
        return self._exec_cache

    # ---------------- outputs ----------------

    def build_report(self) -> Message:
        text, _ = self._bench_execute()
        return Message(text=text)

    def get_bench_results(self) -> Data:
        _, data = self._bench_execute()
        return Data(data=data)
