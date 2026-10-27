from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


class CustomComponent(Component):
    display_name = "Mission Cost Summary"

    description = (
        "Combines Routing Log outputs from multiple Resilient Matcha "
        "Mission nodes into one readable cost report."
    )

    inputs = [
        DataInput(
            name="snow_log",
            display_name="SNOW Routing Log",
            required=False,
        ),
        DataInput(
            name="kb_log",
            display_name="KB Routing Log",
            required=False,
        ),
        DataInput(
            name="ceq_log",
            display_name="CEQ Routing Log",
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
        snow = self._unwrap(self.snow_log)
        kb = self._unwrap(self.kb_log)
        ceq = self._unwrap(self.ceq_log)

        total_cost = round(
            (snow.get("total_cost_usd") or 0)
            + (kb.get("total_cost_usd") or 0)
            + (ceq.get("total_cost_usd") or 0),
            6,
        )

        any_fallback = any(
            (log or {}).get("fallback_used")
            for log in (snow, kb, ceq)
        )

        any_error = any(
            (log or {}).get("error")
            for log in (snow, kb, ceq)
        )

        lines = [
            "MISSION COST SUMMARY",
            self._line("SNOW Ticket Match", snow),
            self._line("KB & Procedures", kb),
            self._line("CEQ Audit", ceq),
            "",
            "Total cost this call: $" + str(total_cost),
            "Fallback used: "
            + (
                "YES - review flagged above"
                if any_fallback
                else "no"
            ),
            "Errors: "
            + (
                "YES - see above"
                if any_error
                else "none"
            ),
        ]

        return Message(text="\n".join(lines))
