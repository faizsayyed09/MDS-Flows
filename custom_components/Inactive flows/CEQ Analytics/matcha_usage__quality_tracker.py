import os
import json
import time
import tempfile
from datetime import datetime, timezone

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, DropdownInput, IntInput, MultilineInput, Output
from lfx.schema.message import Message

RATE_CARD = {
    "Gemini 3.8 Flash": [1.50, 7.50],
    "Gemini 3.7 Flash": [1.50, 7.50],
    "Gemini 3.5 Flash": [0.50, 3.00],
    "Gemini 3 Flash": [0.50, 3.00],
    "Gemini 2.5 Flash": [0.30, 2.50],
    "Gemini 2.5 Flash-Lite": [0.10, 0.40],
    "Gemini 3.1 Pro": [2.00, 12.00],
    "Claude Sonnet 5": [2.00, 10.00],
    "Claude Haiku 4.5": [1.00, 5.00],
    "GPT-5.4 Nano": [0.20, 1.25],
    "GPT-5.1": [1.25, 10.00],
    "GPT-5.4": [1.75, 14.00],
}
MODEL_OPTIONS = list(RATE_CARD.keys())
_FRESHNESS_SCORE = {"FRESH": 100, "DEGRADED": 70, "PROCESSING": 70, "STALE": 40}


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


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_ledger():
    rows = []
    path = _p("cost_quality_ledger.jsonl")
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def _text(value):
    if value is None:
        return ""
    if hasattr(value, "text"):
        return str(value.text or "")
    return str(value)


def _data(value):
    if isinstance(value, list) and value:
        value = value[0]
    if hasattr(value, "data"):
        value = value.data or {}
    return value if isinstance(value, dict) else {}


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return None
    if hasattr(obj, "__dict__"):
        return vars(obj)
    return None


def _usage_from(value):
    candidates = []
    props = getattr(value, "properties", None)
    if props is not None:
        candidates.append(getattr(props, "usage", None))
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        candidates.append(data.get("matcha_usage"))
        candidates.append(data.get("usage"))
    candidates.append(getattr(value, "matcha_usage", None))
    for c in candidates:
        u = _as_dict(c)
        if not u:
            continue
        it = _num(u.get("input_tokens") or u.get("prompt_tokens"))
        ot = _num(u.get("output_tokens") or u.get("completion_tokens"))
        cost = _num(u.get("cost_usd") or u.get("total_cost") or u.get("cost"))
        if it or ot or cost:
            return {"in": it, "out": ot, "cost": cost}
    return None


def _et_day(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


class CustomComponent(Component):
    display_name = "Matcha Usage & Quality Tracker"
    description = "Logs per-audit Matcha cost and audit results for calls and emails, and builds summaries and a consolidated digest."

    inputs = [
        DropdownInput(name="environment", display_name="Environment", options=["DEV", "TEST", "PROD"], value="DEV"),
        DataInput(name="dashboard_row", display_name="Dashboard Row", required=False,
                  info="Audit Result Parser Dashboard Row. Leave empty in the analytics flow."),
        DataInput(name="call_data", display_name="Call / Email Data", required=False),
        MessageTextInput(name="snow_input", display_name="SNOW Mission Input", required=False),
        MessageTextInput(name="snow_output", display_name="SNOW Mission Output", required=False),
        MessageTextInput(name="kb_input", display_name="KB Mission Input", required=False),
        MessageTextInput(name="kb_output", display_name="KB Mission Output", required=False),
        MessageTextInput(name="ceq_input", display_name="CEQ Mission Input", required=False),
        MessageTextInput(name="ceq_output", display_name="CEQ Mission Output", required=False),
        DropdownInput(name="snow_model", display_name="SNOW Mission LLM", options=MODEL_OPTIONS, value="Gemini 3.8 Flash"),
        DropdownInput(name="kb_model", display_name="KB Mission LLM", options=MODEL_OPTIONS, value="Gemini 3.8 Flash"),
        DropdownInput(name="ceq_model", display_name="CEQ Mission LLM", options=MODEL_OPTIONS, value="Gemini 3.8 Flash"),
        MultilineInput(name="custom_prices", display_name="Custom Prices", value="", advanced=True,
                       info="Override or add models: 'Model Name=input/output' per line, USD per 1M tokens."),
        IntInput(name="snow_hidden_tokens", display_name="SNOW Hidden Tokens", value=8000, advanced=True),
        IntInput(name="kb_hidden_tokens", display_name="KB Hidden Tokens", value=4000, advanced=True),
        IntInput(name="ceq_hidden_tokens", display_name="CEQ Hidden Tokens", value=3000, advanced=True),
        IntInput(name="summary_days", display_name="Summary Window (days)", value=7),
        IntInput(name="calls_per_day", display_name="Projected Calls per Day", value=108, advanced=True),
        IntInput(name="emails_per_day", display_name="Projected Emails per Day", value=162, advanced=True),
        IntInput(name="ledger_retention_days", display_name="Ledger Retention (days)", value=90, advanced=True),
    ]

    outputs = [
        Output(display_name="Log Result", name="log_result", method="log_audit", group_outputs=True),
        Output(display_name="Summary", name="summary", method="build_summary", group_outputs=True),
        Output(display_name="Digest (HTML)", name="digest", method="build_digest", group_outputs=True),
    ]

    # ---------------- cost ----------------

    def _prices(self):
        prices = {k: list(v) for k, v in RATE_CARD.items()}
        for line in (self.custom_prices or "").splitlines():
            if "=" not in line or "/" not in line:
                continue
            name, pair = line.split("=", 1)
            pin, pout = pair.split("/", 1)
            if _num(pin) is not None and _num(pout) is not None:
                prices[name.strip()] = [_num(pin), _num(pout)]
        return prices

    def _mission_cost(self, sent, received, model, hidden, prices):
        sent_text = _text(sent)
        recv_text = _text(received)
        if not sent_text and not recv_text:
            return None
        pin, pout = prices.get(model, [0.0, 0.0])
        usage = _usage_from(received)
        if usage:
            in_t = usage["in"] if usage["in"] is not None else round(len(sent_text) / 4) + int(hidden or 0)
            out_t = usage["out"] if usage["out"] is not None else round(len(recv_text) / 4)
            cost = usage["cost"] if usage["cost"] is not None else in_t / 1e6 * pin + out_t / 1e6 * pout
            source = "actual"
        else:
            in_t = round(len(sent_text) / 4) + (int(hidden or 0) if sent_text else 0)
            out_t = round(len(recv_text) / 4)
            cost = in_t / 1e6 * pin + out_t / 1e6 * pout
            source = "estimated"
        return {"model": model, "in_tokens": int(in_t), "out_tokens": int(out_t),
                "cost_usd": round(cost, 6), "source": source}

    # ---------------- logging ----------------

    def log_audit(self) -> Message:
        row = _data(self.dashboard_row)
        conv = row.get("conversation_id")
        if not conv:
            return Message(text="Nothing to log: connect the Audit Result Parser's Dashboard Row.")
        call = _data(self.call_data)
        channel = str(row.get("channel") or call.get("channel") or "CALL").upper()
        channel = "EMAIL" if "EMAIL" in channel else "CALL"
        now = time.time()

        for r in _read_ledger()[-500:]:
            if r.get("conversation_id") == conv and r.get("channel") == channel and now - float(r.get("ts") or 0) < 12 * 3600:
                return Message(text="Already logged " + channel + " " + conv + " in the last 12 h; duplicate skipped.")

        prices = self._prices()
        missions = {}
        total = 0.0
        for key, sent, received, model, hidden in (
            ("snow", self.snow_input, self.snow_output, self.snow_model, self.snow_hidden_tokens),
            ("kb", self.kb_input, self.kb_output, self.kb_model, self.kb_hidden_tokens),
            ("ceq", self.ceq_input, self.ceq_output, self.ceq_model, self.ceq_hidden_tokens),
        ):
            m = self._mission_cost(sent, received, model, hidden, prices)
            if m:
                missions[key] = m
                total += m["cost_usd"]

        record = {
            "ts": now,
            "environment": self.environment,
            "channel": channel,
            "conversation_id": conv,
            "agent": row.get("agent_name"),
            "client": row.get("client"),
            "queue_id": row.get("queue_id"),
            "verdict": row.get("verdict"),
            "score_pct": row.get("score_pct"),
            "critical_count": row.get("critical_failure_count") or 0,
            "manager_review": bool(row.get("manager_review_required")),
            "ticket_match_status": row.get("ticket_match_status"),
            "parse_error": bool(row.get("ceq_parse_error")),
            "interaction_end": call.get("call_end"),
            "results": {k: v for k, v in row.items() if str(k).startswith("result_")},
            "missions": missions,
            "total_cost_usd": round(total, 6),
            "cost_source": "actual" if missions and all(m["source"] == "actual" for m in missions.values()) else "estimated",
        }
        with open(_p("cost_quality_ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

        parts = [k.upper() + " $" + str(m["cost_usd"]) for k, m in missions.items()]
        return Message(text=(
            "Logged " + channel + " " + conv + " | " + str(row.get("verdict")) + " " + str(row.get("score_pct"))
            + "% | cost $" + str(round(total, 4)) + " (" + ", ".join(parts) + ", " + record["cost_source"] + ")"
        ))

    # ---------------- analysis ----------------

    def _prune(self):
        keep_days = max(7, int(self.ledger_retention_days or 90))
        rows = _read_ledger()
        cutoff = time.time() - keep_days * 86400
        kept = [r for r in rows if float(r.get("ts") or 0) >= cutoff]
        if len(kept) < len(rows):
            path = _p("cost_quality_ledger.jsonl")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(json.dumps(r, default=str) + "\n")
            os.replace(tmp, path)

    def _stats(self):
        if getattr(self, "_stats_cache", None) is not None:
            return self._stats_cache
        self._prune()
        days = max(1, int(self.summary_days or 7))
        now = time.time()
        records = [r for r in _read_ledger() if now - float(r.get("ts") or 0) <= days * 86400]

        channels = {}
        for ch in ("CALL", "EMAIL"):
            rs = [r for r in records if r.get("channel") == ch]
            scores = [_num(r.get("score_pct")) for r in rs if _num(r.get("score_pct")) is not None]
            cost = sum(_num(r.get("total_cost_usd"), 0.0) for r in rs)
            n = len(rs)
            channels[ch] = {
                "audits": n,
                "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
                "achieved_pct": _pct(sum(1 for r in rs if r.get("verdict") == "ACHIEVED"), n),
                "critical": sum(1 for r in rs if r.get("verdict") == "CRITICAL_FAIL"),
                "reviews": sum(1 for r in rs if r.get("manager_review")),
                "ticket_match_pct": _pct(sum(1 for r in rs if r.get("ticket_match_status") == "MATCHED"), n),
                "parse_errors": sum(1 for r in rs if r.get("parse_error")),
                "cost": round(cost, 4),
                "avg_cost": round(cost / n, 4) if n else 0.0,
            }

        mission_cost = {}
        model_cost = {}
        for r in records:
            for key, m in (r.get("missions") or {}).items():
                mission_cost[key] = mission_cost.get(key, 0.0) + _num(m.get("cost_usd"), 0.0)
                model_cost[m.get("model")] = model_cost.get(m.get("model"), 0.0) + _num(m.get("cost_usd"), 0.0)

        trend = {}
        for r in records:
            day = _et_day(r.get("ts"))
            t = trend.setdefault(day, {"CALL": 0, "EMAIL": 0, "scores": [], "cost": 0.0})
            t[r.get("channel", "CALL")] = t.get(r.get("channel", "CALL"), 0) + 1
            if _num(r.get("score_pct")) is not None:
                t["scores"].append(_num(r.get("score_pct")))
            t["cost"] += _num(r.get("total_cost_usd"), 0.0)

        agents = {}
        for r in records:
            a = agents.setdefault(r.get("agent") or "unknown", {"n": 0, "missed": 0, "scores": []})
            a["n"] += 1
            if r.get("verdict") != "ACHIEVED":
                a["missed"] += 1
            if _num(r.get("score_pct")) is not None:
                a["scores"].append(_num(r.get("score_pct")))
        attention = sorted(agents.items(), key=lambda kv: (-kv[1]["missed"], -kv[1]["n"]))[:5]

        freshness = _read_json(_p("snow_freshness.json"), {})
        dispatcher = _read_json(_p("dispatcher_state.json"), {})
        failed_items = sum(1 for v in (dispatcher.get("calls") or {}).values() if v.get("state") == "failed")

        total = len(records)
        all_scores = [_num(r.get("score_pct")) for r in records if _num(r.get("score_pct")) is not None]
        quality = sum(all_scores) / len(all_scores) if all_scores else None
        critical_rate = sum(1 for r in records if r.get("verdict") == "CRITICAL_FAIL") / total if total else None
        match_pct = _pct(sum(1 for r in records if r.get("ticket_match_status") == "MATCHED"), total)
        fresh = _FRESHNESS_SCORE.get(freshness.get("status"), 0) if freshness else None
        parts = [
            (quality, 0.5),
            ((1 - critical_rate) * 100 if critical_rate is not None else None, 0.2),
            (match_pct, 0.15),
            (fresh, 0.15),
        ]
        weight = sum(w for v, w in parts if v is not None)
        health = round(sum(v * w for v, w in parts if v is not None) / weight, 1) if weight else None

        projection = (
            channels["CALL"]["avg_cost"] * int(self.calls_per_day or 0)
            + channels["EMAIL"]["avg_cost"] * int(self.emails_per_day or 0)
        ) * 30
        actual = sum(1 for r in records if r.get("cost_source") == "actual")

        self._stats_cache = {
            "days": days,
            "total": total,
            "channels": channels,
            "mission_cost": {k: round(v, 4) for k, v in mission_cost.items()},
            "model_cost": {k: round(v, 4) for k, v in model_cost.items()},
            "trend": sorted(trend.items())[-7:],
            "attention": attention,
            "freshness": freshness,
            "failed_items": failed_items,
            "paused": dispatcher.get("paused") or "",
            "last_poll": dispatcher.get("last_poll"),
            "health": health,
            "projection": round(projection, 2),
            "actual_cost_pct": _pct(actual, total),
        }
        return self._stats_cache

    def build_summary(self) -> Message:
        s = self._stats()
        c = s["channels"]
        f = s["freshness"] or {}
        lines = [
            "CEQ AUDIT SUMMARY [" + str(self.environment) + "] - last " + str(s["days"]) + " days",
            "",
            "MANAGEMENT",
            "  Overall Health Score: " + str(s["health"]),
            "  Calls audited: " + str(c["CALL"]["audits"]) + " | avg " + str(c["CALL"]["avg_score"]) + "% | achieved "
            + str(c["CALL"]["achieved_pct"]) + "% | critical " + str(c["CALL"]["critical"]),
            "  Emails audited: " + str(c["EMAIL"]["audits"]) + " | avg " + str(c["EMAIL"]["avg_score"]) + "% | achieved "
            + str(c["EMAIL"]["achieved_pct"]) + "% | critical " + str(c["EMAIL"]["critical"]),
            "  SNOW freshness: " + str(f.get("status", "unknown")) + " (" + str(f.get("age_min")) + " min old)",
            "  Manager reviews: " + str(c["CALL"]["reviews"] + c["EMAIL"]["reviews"]),
            "",
            "TREND (day: calls / emails / avg score / cost)",
        ]
        for day, t in s["trend"]:
            avg = round(sum(t["scores"]) / len(t["scores"]), 1) if t["scores"] else "-"
            lines.append("  " + day + ": " + str(t["CALL"]) + " / " + str(t["EMAIL"]) + " / " + str(avg)
                         + "% / $" + str(round(t["cost"], 3)))
        lines += ["", "OPERATIONAL", "  Analysts needing attention (not achieved / audited):"]
        for name, a in s["attention"]:
            lines.append("    " + str(name) + ": " + str(a["missed"]) + " / " + str(a["n"]))
        lines.append("  Ticket match rate: calls " + str(c["CALL"]["ticket_match_pct"]) + "%, emails "
                     + str(c["EMAIL"]["ticket_match_pct"]) + "%")
        lines += [
            "",
            "COST",
            "  Calls: $" + str(c["CALL"]["cost"]) + " (avg $" + str(c["CALL"]["avg_cost"]) + ")",
            "  Emails: $" + str(c["EMAIL"]["cost"]) + " (avg $" + str(c["EMAIL"]["avg_cost"]) + ")",
            "  By mission: " + ", ".join(k.upper() + " $" + str(v) for k, v in s["mission_cost"].items()),
            "  By model: " + ", ".join(str(k) + " $" + str(v) for k, v in s["model_cost"].items()),
            "  Projected monthly: $" + str(s["projection"]) + " | actual usage data on "
            + str(s["actual_cost_pct"]) + "% of audits (rest estimated)",
            "",
            "TECHNICAL",
            "  Dispatcher failures (retained): " + str(s["failed_items"]) + " | parse errors: "
            + str(c["CALL"]["parse_errors"] + c["EMAIL"]["parse_errors"]),
            "  Dispatcher paused: " + (s["paused"] or "no"),
            "  SNOW files ready/processing/failed: " + str(f.get("ready_count", "-")) + "/"
            + str(f.get("processing_count", "-")) + "/" + str(f.get("failed_count", "-")),
        ]
        return Message(text="\n".join(lines))

    def build_digest(self) -> Message:
        s = self._stats()
        c = s["channels"]
        f = s["freshness"] or {}
        cell = "padding:6px 10px;border:1px solid #ddd"

        def row(label, call_value, email_value):
            return ("<tr><td style='" + cell + "'>" + label + "</td><td style='" + cell + ";text-align:center'>"
                    + str(call_value) + "</td><td style='" + cell + ";text-align:center'>" + str(email_value) + "</td></tr>")

        trend_rows = ""
        for day, t in s["trend"]:
            avg = round(sum(t["scores"]) / len(t["scores"]), 1) if t["scores"] else "-"
            trend_rows += ("<tr><td style='" + cell + "'>" + day + "</td><td style='" + cell + "'>" + str(t["CALL"])
                           + "</td><td style='" + cell + "'>" + str(t["EMAIL"]) + "</td><td style='" + cell + "'>"
                           + str(avg) + "%</td><td style='" + cell + "'>$" + str(round(t["cost"], 3)) + "</td></tr>")
        attention = "".join(
            "<li>" + str(name) + ": " + str(a["missed"]) + " of " + str(a["n"]) + " not achieved</li>"
            for name, a in s["attention"] if a["missed"]
        ) or "<li>None</li>"
        html = (
            "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:820px'>"
            + "<h2>CEQ Audit Digest - last " + str(s["days"]) + " days</h2>"
            + "<p style='font-size:18px'>Overall Health Score: <b>" + str(s["health"]) + "</b> | SNOW: <b>"
            + str(f.get("status", "unknown")) + "</b></p>"
            + "<table style='border-collapse:collapse;font-size:13px'>"
            + "<tr style='background:#f2f2f2'><th style='" + cell + "'></th><th style='" + cell + "'>Calls</th><th style='"
            + cell + "'>Emails</th></tr>"
            + row("Audited", c["CALL"]["audits"], c["EMAIL"]["audits"])
            + row("Average score", str(c["CALL"]["avg_score"]) + "%", str(c["EMAIL"]["avg_score"]) + "%")
            + row("Achieved", str(c["CALL"]["achieved_pct"]) + "%", str(c["EMAIL"]["achieved_pct"]) + "%")
            + row("Critical fails", c["CALL"]["critical"], c["EMAIL"]["critical"])
            + row("Manager reviews", c["CALL"]["reviews"], c["EMAIL"]["reviews"])
            + row("Ticket matched", str(c["CALL"]["ticket_match_pct"]) + "%", str(c["EMAIL"]["ticket_match_pct"]) + "%")
            + row("Matcha cost", "$" + str(c["CALL"]["cost"]), "$" + str(c["EMAIL"]["cost"]))
            + "</table>"
            + "<h3>Trend</h3><table style='border-collapse:collapse;font-size:13px'><tr style='background:#f2f2f2'>"
            + "<th style='" + cell + "'>Day</th><th style='" + cell + "'>Calls</th><th style='" + cell + "'>Emails</th><th style='"
            + cell + "'>Avg score</th><th style='" + cell + "'>Cost</th></tr>" + trend_rows + "</table>"
            + "<h3>Analysts needing attention</h3><ul>" + attention + "</ul>"
            + "<h3>Technical</h3><p style='font-size:13px'>Dispatcher failures: " + str(s["failed_items"])
            + " | Paused: " + (s["paused"] or "no") + " | Projected monthly Matcha cost: $" + str(s["projection"])
            + "</p></div>"
        )
        return Message(text=html)
