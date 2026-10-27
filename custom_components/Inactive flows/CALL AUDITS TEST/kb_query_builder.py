import re
import json

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message


class CustomComponent(Component):
    display_name = "KB Query Builder"
    description = "Turns the SNOW mission JSON into a short search query for the KB mission."

    inputs = [
        MessageTextInput(
            name="ticket_response",
            display_name="Ticket Response",
            required=True,
        ),
    ]

    outputs = [
        Output(display_name="KB Query", name="kb_query", method="build_query"),
    ]

    def build_query(self) -> Message:
        raw = self.ticket_response
        if hasattr(raw, "text"):
            raw = raw.text
        raw = str(raw or "")

        data = {}
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except Exception:
                data = {}

        summary = str(data.get("issueSummary") or "").strip()
        if not summary:
            summary = re.sub(r"\s+", " ", raw)[:300]

        query = json.dumps({
            "issueSummary": summary[:400],
            "client": str(data.get("client") or ""),
            "product": str(data.get("product") or ""),
        })
        return Message(text=query)
