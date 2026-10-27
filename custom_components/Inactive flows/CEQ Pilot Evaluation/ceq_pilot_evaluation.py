import os
import io
import csv
import json
import time
import tempfile

from lfx.custom.custom_component.component import Component
from lfx.io import MultilineInput, IntInput, StrInput, Output
from lfx.schema.message import Message


def _data_dir():
    base = os.environ.get("LANGFLOW_CONFIG_DIR") or ""
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        path = os.path.join(base, "ceq_audit")
    else:
        path = os.path.join(tempfile.gettempdir(), "ceq_audit")
    os.makedirs(path, exist_ok=True)
    return path


def _read_ledger():
    rows = []
    path = os.path.join(_data_dir(), "cost_quality_ledger.jsonl")
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


def _num(value, default=None):
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def _verdict(value):
    s = "".join(ch for ch in str(value or "").upper() if ch.isalpha())
    if s in ("ACHIEVED", "PASS", "PASSED"):
        return "ACHIEVED"
    if s.startswith("CRITICAL"):
        return "CRITICAL_FAIL"
    if s in ("NOTACHIEVED", "FAIL", "FAILED"):
        return "NOT_ACHIEVED"
    return s


def _result(value):
    s = "".join(ch for ch in str(value or "").upper() if ch.isalpha())
    if s in ("MET", "YES", "Y", "PASS"):
        return "MET"
    if s in ("NOTMET", "NO", "N", "FAIL", "MISSED"):
        return "NOT_MET"
    if s in ("NA", "NOTAPPLICABLE"):
        return "NA"
    return ""


def _yes(value):
    return str(value or "").strip().lower() in ("y", "yes", "true", "1")


def _pct(part, whole):
    if not whole:
        return None
    return round(part / whole * 100, 1)


class CustomComponent(Component):
    display_name = "CEQ Pilot Evaluation"
    description = "Compares pipeline audits with CEQ analyst scores and reports accuracy, cost and ROI for a go/no-go decision."

    inputs = [
        MultilineInput(
            name="analyst_scores",
            display_name="Analyst Scores (paste from Excel)",
            value="",
            info="Columns: conversation_id, verdict, score_pct, critical (Y/N), optional result_<attribute>.",
        ),
        IntInput(
            name="days",
            display_name="Include Audits From Last (days)",
            value=30,
        ),
        StrInput(
            name="environment",
            display_name="Environment Filter",
            value="",
            info="Blank = all environments.",
        ),
        IntInput(
            name="analyst_minutes_call",
            display_name="Manual Minutes per Call Audit",
            value=25,
        ),
        IntInput(
            name="analyst_minutes_email",
            display_name="Manual Minutes per Email Audit",
            value=15,
        ),
        StrInput(
            name="analyst_hourly_cost",
            display_name="Analyst Hourly Cost (USD)",
            value="30",
        ),
        StrInput(
            name="verdict_target",
            display_name="Verdict Agreement Target (%)",
            value="85",
        ),
        StrInput(
            name="attribute_target",
            display_name="Attribute Agreement Target (%)",
            value="90",
        ),
        StrInput(
            name="score_gap_target",
            display_name="Max Avg Score Gap (points)",
            value="8",
        ),
        IntInput(
            name="min_sample",
            display_name="Min Analyst-Scored per Channel",
            value=15,
        ),
    ]

    outputs = [
        Output(display_name="Evaluation Report", name="evaluation", method="build_evaluation"),
    ]

    def _analyst(self):
        out = {}
        text = (self.analyst_scores or "").strip()
        if not text:
            return out
        first_line = text.splitlines()[0]
        delimiter = "\t" if "\t" in first_line else ","
        for raw in csv.DictReader(io.StringIO(text), delimiter=delimiter):
            r = {}
            for k, v in raw.items():
                if k:
                    r[str(k).strip().lower()] = v
            conv = str(r.get("conversation_id") or "").strip().lower()
            if not conv:
                continue
            results = {}
            for k, v in r.items():
                if k.startswith("result_"):
                    res = _result(v)
                    if res:
                        results[k] = res
            out[conv] = {
                "verdict": _verdict(r.get("verdict")),
                "score": _num(r.get("score_pct")),
                "critical": _yes(r.get("critical")),
                "results": results,
            }
        return out

    def build_evaluation(self) -> Message:
        now = time.time()
        days = max(1, int(self.days or 30))
        env = (self.environment or "").strip().upper()

        latest = {}
        for r in _read_ledger():
            if now - float(r.get("ts") or 0) > days * 86400:
                continue
            if env and str(r.get("environment") or "").upper() != env:
                continue
            key = str(r.get("conversation_id") or "").lower()
            if key:
                latest[key] = r

        analyst = self._analyst()
        hourly = _num(self.analyst_hourly_cost, 0.0) or 0.0
        v_target = _num(self.verdict_target, 85.0)
        a_target = _num(self.attribute_target, 90.0)
        gap_target = _num(self.score_gap_target, 8.0)
        min_n = max(1, int(self.min_sample or 15))

        header = "CEQ PILOT EVALUATION - last " + str(days) + " days"
        if env:
            header += " [" + env + "]"
        lines = [header, ""]

        if not latest:
            lines.append("No audits in the ledger yet. Run pilot audits with the Usage & Quality Tracker connected.")
            return Message(text="\n".join(lines))

        decisions = []
        channel_settings = [
            ("CALL", int(self.analyst_minutes_call or 25)),
            ("EMAIL", int(self.analyst_minutes_email or 15)),
        ]

        for channel, minutes in channel_settings:
            recs = [r for r in latest.values() if r.get("channel") == channel]
            if not recs:
                continue

            costs = [_num(r.get("total_cost_usd"), 0.0) for r in recs]
            avg_cost = sum(costs) / len(costs)
            actual = sum(1 for r in recs if r.get("cost_source") == "actual")
            parse_errors = sum(1 for r in recs if r.get("parse_error"))
            matched = sum(1 for r in recs if r.get("ticket_match_status") == "MATCHED")
            manual_cost = minutes / 60.0 * hourly

            n = 0
            verdict_same = 0
            gaps = []
            attr_same = 0
            attr_total = 0
            crit_fp = 0
            crit_fn = 0
            disputed = {}

            for conv, r in latest.items():
                if r.get("channel") != channel or conv not in analyst:
                    continue
                a = analyst[conv]
                n += 1

                if _verdict(r.get("verdict")) == a["verdict"]:
                    verdict_same += 1

                ai_score = _num(r.get("score_pct"))
                if ai_score is not None and a["score"] is not None:
                    gaps.append(abs(ai_score - a["score"]))

                ai_crit = int(r.get("critical_count") or 0) > 0 or r.get("verdict") == "CRITICAL_FAIL"
                if ai_crit and not a["critical"]:
                    crit_fp += 1
                if a["critical"] and not ai_crit:
                    crit_fn += 1

                ai_results = r.get("results") or {}
                for k, v in a["results"].items():
                    if k in ai_results:
                        attr_total += 1
                        if _result(ai_results[k]) == v:
                            attr_same += 1
                        else:
                            disputed[k] = disputed.get(k, 0) + 1

            verdict_pct = _pct(verdict_same, n)
            attr_pct = _pct(attr_same, attr_total)
            avg_gap = round(sum(gaps) / len(gaps), 1) if gaps else None

            if n < min_n:
                decision = "INSUFFICIENT DATA (" + str(n) + " of " + str(min_n) + " analyst-scored)"
            elif (
                verdict_pct >= v_target
                and crit_fp == 0
                and (attr_pct is None or attr_pct >= a_target)
                and (avg_gap is None or avg_gap <= gap_target)
                and parse_errors == 0
            ):
                decision = "GO"
            elif verdict_pct >= v_target - 10 and crit_fp <= 1:
                decision = "CONDITIONAL GO (tune the disputed attributes, then re-test)"
            else:
                decision = "NO-GO (accuracy below target)"
            decisions.append(channel + ": " + decision)

            lines.append(channel + "S: " + str(len(recs)) + " audited, " + str(n) + " analyst-scored")
            lines.append("  Verdict agreement: " + str(verdict_pct) + "% (target " + str(v_target) + "%)")
            lines.append("  Attribute agreement: " + str(attr_pct) + "% (target " + str(a_target) + "%)")
            lines.append("  Avg score gap vs analyst: " + str(avg_gap) + " points (target <= " + str(gap_target) + ")")
            lines.append("  Critical errors: flagged by AI only " + str(crit_fp) + " | missed by AI " + str(crit_fn))
            lines.append("  Parse errors: " + str(parse_errors) + " | Ticket matched: " + str(_pct(matched, len(recs))) + "%")
            lines.append(
                "  Matcha cost per audit: $" + str(round(avg_cost, 4))
                + " (" + str(actual) + " of " + str(len(recs)) + " from actual usage data, rest estimated)"
            )
            lines.append(
                "  Manual audit cost: $" + str(round(manual_cost, 2))
                + " (" + str(minutes) + " min at $" + str(hourly) + "/h)"
            )
            lines.append("  Saving per audit: $" + str(round(manual_cost - avg_cost, 2)))

            if disputed:
                worst = sorted(disputed.items(), key=lambda kv: -kv[1])[:5]
                text = ", ".join(k.replace("result_", "") + " (" + str(v) + ")" for k, v in worst)
                lines.append("  Most disputed attributes: " + text)
            lines.append("")

        lines.append("DECISION")
        for d in decisions:
            lines.append("  " + d)
        lines.append("")
        lines.append(
            "Notes: cost covers Matcha missions only (not server transcription time). "
            "Analyst time is still needed for manager-review items."
        )
        return Message(text="\n".join(lines))
