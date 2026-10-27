from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


class CustomComponent(Component):

    display_name = "Mission Cost Summary"

    description = (
        "Combines Routing Log outputs from one or more Resilient Matcha "
        "Mission nodes into one readable cost report."
    )

    inputs = [
        DataInput(
            name="cx_scorer_log",
            display_name="CX Scorer Routing Log",
            required=False,
        ),
        DataInput(
            name="snow_log",
            display_name="SNOW Ticket Match Routing Log (optional)",
            required=False,
        ),
        DataInput(
            name="kb_log",
            display_name="KB & Procedures Routing Log (optional)",
            required=False,
        ),
        DataInput(
            name="ceq_log",
            display_name="CEQ Audit Routing Log (optional)",
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="Cost Report",
            name="report",
            method="build_report",
        ),
    ]

    def _unwrap(self, value):
        if isinstance(value, list) and value:
            value = value[0]

        if hasattr(value, "data"):
            return value.data or {}

        return value if isinstance(value, dict) else {}

    def _line(self, label, log):
        if not log:
            return label + ": not run"

        served = log.get("served_by") or "FAILED"
        fallback = " (fallback)" if log.get("fallback_used") else ""
        cost = log.get("total_cost_usd", 0.0)
        attempts = len(log.get("attempts") or [])
        error = log.get("error")

        base = (
            label + ": " + served + fallback
            + " | attempts " + str(attempts)
            + " | $" + str(cost)
        )

        if error:
            base += " | ERROR: " + str(error)[:150]

        return base

    def build_report(self) -> Message:
        cx = self._unwrap(getattr(self, "cx_scorer_log", None))
        snow = self._unwrap(getattr(self, "snow_log", None))
        kb = self._unwrap(getattr(self, "kb_log", None))
        ceq = self._unwrap(getattr(self, "ceq_log", None))

        logs = {
            "CX Scorer": cx,
            "SNOW Ticket Match": snow,
            "KB & Procedures": kb,
            "CEQ Audit": ceq,
        }

        active_logs = {
            label: log
            for label, log in logs.items()
            if log
        }

        total_cost = round(
            sum(
                (log.get("total_cost_usd") or 0)
                for log in active_logs.values()
            ),
            6,
        )

        any_fallback = any(
            (log or {}).get("fallback_used")
            for log in active_logs.values()
        )

        any_error = any(
            (log or {}).get("error")
            for log in active_logs.values()
        )

        lines = ["MISSION COST SUMMARY"]

        if active_logs:
            for label, log in active_logs.items():
                lines.append(self._line(label, log))
        else:
            lines.append("No mission logs connected.")

        lines += [
            "",
            "Total cost this call: $" + str(total_cost),
            "Fallback used: "
            + ("YES - review flagged above" if any_fallback else "no"),
            "Errors: "
            + ("YES - see above" if any_error else "none"),
        ]

        return Message(text="\n".join(lines))
