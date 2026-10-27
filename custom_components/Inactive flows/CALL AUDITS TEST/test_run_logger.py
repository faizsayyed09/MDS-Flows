import os
import json
import time
import tempfile
from datetime import datetime, timezone

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, MultilineInput, StrInput, Output
from lfx.schema.message import Message

_DEFAULT_RESULTS = os.path.join(tempfile.gettempdir(), "qa_model_benchmark", "results.jsonl")


def _text(value):
    if value is None:
        return ""
    if hasattr(value, "text"):
        value = value.text
    return str(value or "")


def _data(value):
    if isinstance(value, list) and value:
        value = value[0]
    if hasattr(value, "data"):
        value = value.data or {}
    return value if isinstance(value, dict) else {}


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CustomComponent(Component):
    display_name = "Test Run Logger"
    description = "Records estimated token cost and the audit result for one run. Driven by the Model Benchmark Runner."

    inputs = [
        MultilineInput(
            name="run_config",
            display_name="Run Config (JSON)",
            value="{}",
            info="Set automatically by the Model Benchmark Runner. Leave as {} for manual runs.",
            advanced=True,
        ),
        StrInput(
            name="results_path",
            display_name="Results File",
            value=_DEFAULT_RESULTS,
            advanced=True,
        ),
        DataInput(
            name="call_data",
            display_name="Call Data",
            required=True,
            info="Structured Data output of the QA Transcriber.",
        ),
        DataInput(
            name="dashboard_row",
            display_name="Dashboard Row",
            required=True,
            info="Dashboard Row output of the Audit Result Parser.",
        ),
        MessageTextInput(name="snow_input", display_name="SNOW Mission Input", required=False),
        MessageTextInput(name="snow_output", display_name="SNOW Mission Output", required=False),
        MessageTextInput(name="kb_input", display_name="KB Mission Input", required=False),
        MessageTextInput(name="kb_output", display_name="KB Mission Output", required=False),
        MessageTextInput(name="ceq_input", display_name="CEQ Mission Input", required=False),
        MessageTextInput(name="ceq_output", display_name="CEQ Mission Output", required=False),
    ]

    outputs = [
        Output(display_name="Log Summary", name="log_summary", method="log_row"),
    ]

    def log_row(self) -> Message:
        try:
            cfg = json.loads(self.run_config or "{}")
        except Exception:
            cfg = {}

        call = _data(self.call_data)
        row = _data(self.dashboard_row)

        pairs = [
            ("snow", self.snow_input, self.snow_output),
            ("kb", self.kb_input, self.kb_output),
            ("ceq", self.ceq_input, self.ceq_output),
        ]

        missions = {}
        total = 0.0
        for key, sent, received in pairs:
            in_chars = len(_text(sent))
            out_chars = len(_text(received))
            hidden = int(_num(cfg.get(key + "_hidden_tokens"), 0)) if in_chars else 0
            in_tokens = round(in_chars / 4) + hidden
            out_tokens = round(out_chars / 4)
            cost = (
                in_tokens / 1000000.0 * _num(cfg.get(key + "_in_price"))
                + out_tokens / 1000000.0 * _num(cfg.get(key + "_out_price"))
            )
            missions[key] = {
                "model": cfg.get(key + "_model", ""),
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
                "cost_usd": round(cost, 6),
            }
            total += cost

        record = {
            "type": "result",
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            "logged_epoch": time.time(),
            "run_label": cfg.get("run_label") or "manual",
            "repeat": int(_num(cfg.get("repeat"), 1)),
            "models": {
                "snow": cfg.get("snow_model", ""),
                "kb": cfg.get("kb_model", ""),
                "ceq": cfg.get("ceq_model", ""),
            },
            "conversation_id": call.get("conversation_id") or row.get("conversation_id"),
            "audio_duration_sec": call.get("audio_duration_sec"),
            "transcript_confidence": call.get("transcript_confidence"),
            "uncertain_word_pct": call.get("uncertain_word_pct"),
            "transcription_model": call.get("model_used"),
            "missions": missions,
            "total_cost_usd": round(total, 6),
            "row": row,
        }

        path = (self.results_path or _DEFAULT_RESULTS).strip()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

        lines = [
            "Logged: " + str(record["conversation_id"]) + " | label " + str(record["run_label"])
            + " | repeat " + str(record["repeat"]),
            "Estimated cost: $" + str(record["total_cost_usd"]),
        ]
        for key in ("snow", "kb", "ceq"):
            m = missions[key]
            lines.append(
                "  " + key.upper() + ": $" + str(m["cost_usd"]) + " (" + str(m["in_tokens"])
                + " in / " + str(m["out_tokens"]) + " out)"
            )
        lines.append("Verdict: " + str(row.get("verdict")) + " (" + str(row.get("score_pct")) + "%)")
        return Message(text="\n".join(lines))
