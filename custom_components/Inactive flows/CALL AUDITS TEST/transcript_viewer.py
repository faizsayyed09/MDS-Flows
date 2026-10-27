import re
import html
from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, IntInput, StrInput, DropdownInput, Output
from lfx.schema.message import Message
from lfx.schema.dataframe import DataFrame


LINE_RE = re.compile(r"^\[(\d{2}:\d{2})\]\s+(?:(AGENT|CALLER|IVR):\s*)?(.*)$")
MARKER_RE = re.compile(r"-----\s*(.+?)\s*-----")


def _call_dict(value):
    if isinstance(value, list) and value:
        value = value[0]
    if hasattr(value, "data"):
        value = value.data or {}
    return value if isinstance(value, dict) else {}


def _parse_rows(transcript):
    rows = []
    for raw in (transcript or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        marker = MARKER_RE.search(line)
        if marker:
            rows.append({
                "time": "",
                "speaker": "EVENT",
                "text": marker.group(1),
                "uncertain_words": 0,
            })
            continue
        m = LINE_RE.match(line)
        if m:
            text = m.group(3)
            rows.append({
                "time": m.group(1),
                "speaker": m.group(2) or "",
                "text": text,
                "uncertain_words": text.count("(?)"),
            })
        else:
            rows.append({
                "time": "",
                "speaker": "",
                "text": line,
                "uncertain_words": line.count("(?)"),
            })
    for i, r in enumerate(rows, start=1):
        r["line"] = i
    return rows


class CustomComponent(Component):
    display_name = "Transcript Viewer"
    description = "Shows the full transcript as a table, in pages, or as HTML for email, so nothing is cut off."

    inputs = [
        DataInput(
            name="call_data",
            display_name="Call Data",
            required=True,
            info="Connect the Structured Data output of the QA Transcriber.",
        ),
        DropdownInput(
            name="speaker_filter",
            display_name="Speaker",
            options=["ALL", "AGENT", "CALLER", "IVR", "EVENT"],
            value="ALL",
            info="EVENT = hold and dead-air markers.",
        ),
        StrInput(
            name="search",
            display_name="Search",
            value="",
            info="Only show lines containing this text (case-insensitive). Leave blank for all lines.",
        ),
        IntInput(
            name="page",
            display_name="Page",
            value=1,
            info="Page number for the Transcript Page output.",
        ),
        IntInput(
            name="lines_per_page",
            display_name="Lines per Page",
            value=25,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Transcript Table", name="table", method="build_table", group_outputs=True),
        Output(display_name="Transcript Page", name="page_text", method="build_page", group_outputs=True),
        Output(display_name="Transcript HTML", name="html_text", method="build_html", group_outputs=True),
    ]

    # ---------------- helpers ----------------

    def _load(self):
        call = _call_dict(self.call_data)
        if call.get("status") == "PROCESSING":
            return call, [], "Transcription still processing (" + str(call.get("overall_progress_pct")) + "%). Re-run later."
        if call.get("error"):
            return call, [], "Transcriber error: " + str(call.get("error"))
        transcript = call.get("clean_transcript") or ""
        if not transcript:
            return call, [], "No transcript found in Call Data."
        return call, _parse_rows(transcript), None

    def _filtered(self, rows):
        speaker = (self.speaker_filter or "ALL").upper()
        term = (self.search or "").strip().lower()
        out = []
        for r in rows:
            if speaker != "ALL" and r["speaker"] != speaker:
                continue
            if term and term not in r["text"].lower():
                continue
            out.append(r)
        return out

    def _header(self, call, total, shown):
        return "\n".join([
            "Conversation: " + str(call.get("conversation_id")),
            "Agent: " + str(call.get("agent_names") or "unknown"),
            "Duration: " + str(call.get("audio_duration_sec")) + "s | Model: " + str(call.get("model_used")),
            "Transcript confidence: " + str(call.get("transcript_confidence"))
            + " | Uncertain words: " + str(call.get("uncertain_word_pct")) + "%",
            "Lines: " + str(shown) + " shown of " + str(total),
        ])

    # ---------------- outputs ----------------

    def build_table(self) -> DataFrame:
        call, rows, err = self._load()
        if err:
            return DataFrame([{"line": 0, "time": "", "speaker": "", "text": err, "uncertain_words": 0}])
        rows = self._filtered(rows)
        if not rows:
            rows = [{"line": 0, "time": "", "speaker": "", "text": "No lines match the filter.", "uncertain_words": 0}]
        ordered = [
            {
                "line": r["line"],
                "time": r["time"],
                "speaker": r["speaker"],
                "text": r["text"],
                "uncertain_words": r["uncertain_words"],
            }
            for r in rows
        ]
        return DataFrame(ordered)

    def build_page(self) -> Message:
        call, rows, err = self._load()
        if err:
            return Message(text=err)
        total = len(rows)
        rows = self._filtered(rows)
        per_page = max(1, int(self.lines_per_page or 25))
        pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = min(max(1, int(self.page or 1)), pages)
        chunk = rows[(page - 1) * per_page: page * per_page]

        body = []
        for r in chunk:
            if r["speaker"] == "EVENT":
                body.append("        ----- " + r["text"] + " -----")
            else:
                prefix = ("[" + r["time"] + "] ") if r["time"] else ""
                who = (r["speaker"] + ": ") if r["speaker"] else ""
                body.append(prefix + who + r["text"])

        text = "\n".join([
            self._header(call, total, len(rows)),
            "Page " + str(page) + " of " + str(pages),
            "========================",
            "\n".join(body) if body else "No lines match the filter.",
            "========================",
            ("Next: set Page to " + str(page + 1) + " and re-run.") if page < pages else "End of transcript.",
        ])
        return Message(text=text)

    def build_html(self) -> Message:
        call, rows, err = self._load()
        if err:
            return Message(text="<p>" + html.escape(err) + "</p>")
        total = len(rows)
        rows = self._filtered(rows)
        esc = html.escape
        colours = {"AGENT": "#0b5394", "CALLER": "#38761d", "IVR": "#888888", "EVENT": "#b8860b"}
        cell = "padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top"

        trs = []
        for r in rows:
            colour = colours.get(r["speaker"], "#333333")
            text = esc(r["text"]).replace(
                "(?)", "<span style='color:#c82333;font-weight:bold'>(?)</span>"
            )
            if r["speaker"] == "EVENT":
                trs.append(
                    "<tr><td style='" + cell + "'></td><td style='" + cell + "'></td>"
                    + "<td style='" + cell + ";color:" + colour + ";font-style:italic'>" + text + "</td></tr>"
                )
            else:
                trs.append(
                    "<tr><td style='" + cell + ";color:#666;white-space:nowrap'>" + esc(r["time"]) + "</td>"
                    + "<td style='" + cell + ";color:" + colour + ";font-weight:bold'>" + esc(r["speaker"]) + "</td>"
                    + "<td style='" + cell + "'>" + text + "</td></tr>"
                )

        header = esc(self._header(call, total, len(rows))).replace("\n", "<br>")
        page = (
            "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:900px'>"
            + "<h2>Call Transcript</h2>"
            + "<p style='font-size:13px;color:#444'>" + header + "</p>"
            + "<p style='font-size:12px;color:#888'>Words marked "
            + "<span style='color:#c82333;font-weight:bold'>(?)</span> were uncertain.</p>"
            + "<table style='border-collapse:collapse;width:100%;font-size:13px'>"
            + "".join(trs)
            + "</table></div>"
        )
        return Message(text=page)
