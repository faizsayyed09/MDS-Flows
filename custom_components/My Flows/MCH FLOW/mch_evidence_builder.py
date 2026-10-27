import html
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from langflow.custom import Component
from langflow.io import FileInput, HandleInput, IntInput, MessageTextInput, MultilineInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class MCHEvidenceBuilder(Component):
    display_name = "MCH Evidence Builder"
    description = "Joins Genesys, SNOW and KB exports with the transcript into one verified evidence packet per call."
    icon = "file-search"
    name = "MCHEvidenceBuilder"

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    TICKET_RE = r"\b(INC|RITM|REQ|CS|SCTASK)\s?-?(\d{7,8})\b"
    STAMP_RE = r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}) - (.+?) \((Work notes|Additional comments)\)"
    CONTACT_KEYS = ["Contact [Incident]", "Contact [Case]", "Requested for [Requested Item]", "Requestor [Request]"]
    DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                   "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
    FIELD_LABELS = {
        "caller_name": ["Name First/Last", "User's name"],
        "role": ["Role/ Job title", "Role/Job title", "Role"],
        "department": ["Department"],
        "location": ["Location", "Device location", "Device Location"],
        "application": ["Affected Application", "Application"],
        "routing": ["Routing"],
    }
    STOP = set((
        "the and for with that this have has had you your are was were not but from what when they them there here "
        "about into been also then than some more any all get got going want need like see let just know can could "
        "would will should please thank thanks okay yeah yes right sure call calling called issue user users "
        "service desk monadnock hospital community mch general article altera help ticket one two"
    ).split())

    DEFAULT_CONFIG = json.dumps({
        "allowed_caller_role": ["Nurse / Clinical", "Physician / Provider", "Medical Assistant",
                                "Registration / Front Desk", "Billing / PFS", "Pharmacy", "Lab / Imaging",
                                "Manager / Supervisor", "Administrative / Office", "IT / Onsite Tech",
                                "Vendor", "Other", "Unknown"],
        "allowed_issue_type": ["Password Reset / Account Unlock", "MFA", "Citrix / Remote Access / VPN",
                               "Paragon EMR", "eCW (eClinicalWorks)", "OneContent", "Printing",
                               "Hardware - Desktop / Monitor / WOW", "Email / Outlook", "Access Request",
                               "Telephony", "Ticket Status Follow-up", "Other Application", "Other"],
        "mch_team": ["Aishwarya Kudale", "Shital Jadhav", "Utkarsh Mishra", "Prasad Surwade", "Jyoti Waybhase",
                     "Subhash Vishwakarma", "Akshay Sapkale", "Vikas Lokhande", "Adersh Unnikrishnan",
                     "Aleena Ahmed", "Keyur Tanna", "Sakshi Shede", "Elisa Salve", "Faiq Kamboo",
                     "Neha Santosh Zende", "Anjali Rajawat", "Hardik Gajjar"],
        "name_aliases": {"sheetal jadhav": "Shital Jadhav", "adersh unni krishnan": "Adersh Unnikrishnan"},
        "overflow_queue_markers": ["GSD_OF"],
        "handoff_bonus": 25,
        "mch_action_hours": 24,
        "kb_full_text_count": 1,
        "kb_chars": 2500,
        "description_chars": 1000
    }, indent=1)

    inputs = [
        MessageTextInput(name="conversation_id", display_name="Conversation ID", required=True),
        HandleInput(name="transcript", display_name="Transcript (Structured Data)", input_types=["Data", "Message"]),
        FileInput(name="genesys_file", display_name="Genesys Interactions Export (.xlsx)", file_types=["xlsx"]),
        FileInput(name="snow_file", display_name="SNOW Ticket Export (.xlsx)", file_types=["xlsx"]),
        FileInput(name="kb_file", display_name="MCH KB Export (.xlsx)", file_types=["xlsx"]),
        MultilineInput(name="config_json", display_name="Config JSON", value="{}", advanced=True),
        IntInput(name="window_before_min", display_name="Window before call (min)", value=5, advanced=True),
        IntInput(name="window_after_min", display_name="Window after call (min)", value=45, advanced=True),
        IntInput(name="history_days", display_name="Repeat-caller lookback (days)", value=30, advanced=True),
        IntInput(name="max_transcript_chars", display_name="Max transcript chars", value=60000, advanced=True),
    ]

    outputs = [
        Output(display_name="Evidence JSON", name="evidence", method="build_evidence", group_outputs=True),
        Output(display_name="Mission Packet", name="packet", method="build_packet", group_outputs=True),
    ]

    # ---------- file helpers ----------
    def _path(self, v):
        if isinstance(v, list):
            v = v[0] if v else None
        if not v:
            return None
        p = str(v)
        if os.path.exists(p):
            return p
        for fn in ("resolve_path", "get_full_path"):
            try:
                rp = getattr(self, fn)(p)
                if rp and os.path.exists(rp):
                    return rp
            except Exception:
                pass
        return p

    def _col_idx(self, letters):
        n = 0
        for ch in letters.upper():
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    def _read_xlsx(self, path):
        z = zipfile.ZipFile(path)
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(self.NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(self.NS + "t")))
        sheet = "xl/worksheets/sheet1.xml"
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rid = wb.find(self.NS + "sheets")[0].get(self.RNS + "id")
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for r in rels:
                if r.get("Id") == rid:
                    t = r.get("Target").lstrip("/")
                    sheet = t if t.startswith("xl/") else "xl/" + t
        except Exception:
            pass
        root = ET.fromstring(z.read(sheet))
        rows = []
        for row in root.iter(self.NS + "row"):
            cells = {}
            for c in row.findall(self.NS + "c"):
                ref = re.sub(r"\d", "", c.get("r", ""))
                col = self._col_idx(ref) if ref else len(cells)
                t = c.get("t")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(self.NS + "t"))
                else:
                    v = c.find(self.NS + "v")
                    val = v.text if v is not None and v.text is not None else ""
                    if t == "s" and val != "":
                        val = shared[int(val)]
                cells[col] = val
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        return rows

    def _table(self, path, must_have):
        if not path or not os.path.exists(path):
            raise ValueError(f"File not found: {path}")
        rows = self._read_xlsx(path)
        for i, r in enumerate(rows[:15]):
            hdr = [str(x).strip() for x in r]
            if must_have in hdr:
                out = []
                for r2 in rows[i + 1:]:
                    if not any(str(x).strip() for x in r2):
                        continue
                    out.append({hdr[j]: (r2[j] if j < len(r2) else "") for j in range(len(hdr)) if hdr[j]})
                return out
        raise ValueError(f"Header '{must_have}' not found in {os.path.basename(path)}")

    # ---------- value helpers ----------
    def _dt(self, v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            f = float(s)
            if 20000 < f < 80000:
                return datetime(1899, 12, 30) + timedelta(seconds=round(f * 86400))
        except ValueError:
            pass
        for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _fmt(self, v):
        d = self._dt(v)
        return f"{d:%m/%d/%Y %H:%M:%S}" if d else str(v or "")

    def _num(self, v):
        try:
            return float(str(v).strip())
        except Exception:
            return 0.0

    def _g(self, row, *names):
        for n in names:
            if n in row and str(row[n]).strip() != "":
                return str(row[n]).strip()
        return ""

    def _ntok(self, s):
        return [t for t in re.sub(r"[^a-z ]", " ", str(s or "").lower()).split() if len(t) > 1]

    def _canon(self, s):
        s = str(s or "").strip()
        return (getattr(self, "_mch_aliases", None) or {}).get(s.lower(), s)

    def _same_person(self, a, b):
        a, b = self._canon(a), self._canon(b)
        ca, cb = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())
        if len(ca) >= 6 and ca == cb:
            return True
        ta, tb = set(self._ntok(a)), set(self._ntok(b))
        if len(ta) < 2 or len(tb) < 2:
            return False
        small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        return small <= big

    def _is_mch(self, name):
        team = (getattr(self, "_mch_cfg", None) or {}).get("mch_team") or []
        return any(self._same_person(name, m) for m in team)

    def _words(self, s):
        return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", str(s or "").lower()) if w not in self.STOP]

    def _strip_html(self, s):
        s = str(s or "")
        s = re.sub(r"\[/?code\]", " ", s)
        s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = html.unescape(s).replace("\u200b", "").replace("\u2060", "")
        return re.sub(r"[ \t]+", " ", s)

    def _clean_desc(self, s):
        s = self._strip_html(s)
        s = re.sub(r"(?im)^.*(phone|contact number).*$", "", s)
        return re.sub(r"\n\s*\n+", "\n", s).strip()

    def _desc_fields(self, desc):
        text = self._strip_html(desc)
        out = {}
        for key, labels in self.FIELD_LABELS.items():
            val = ""
            for lab in labels:
                m = re.search(r"(?im)^\s*" + re.escape(lab) + r"\s*:[ \t]*(.*)$", text)
                if m:
                    v = m.group(1).strip()
                    if v and v.lower() not in ("na", "n/a", "none", "-"):
                        val = v
                        break
            out[key] = val
        return out

    def _ticket_ids(self, text):
        return {f"{a.upper()}{b}" for a, b in re.findall(self.TICKET_RE, text or "", flags=re.I)}

    def _digit_runs(self, text):
        t = re.sub(r"\[\d{1,2}:\d{2}(:\d{2})?\]", " ", text or "")
        t = re.sub(r"\b(" + "|".join(self.DIGIT_WORDS) + r")\b",
                   lambda m: self.DIGIT_WORDS[m.group(1).lower()], t, flags=re.I)
        t = re.sub(r"(?<=\d)[\s,.\-]+(?=\d)", "", t)
        return set(re.findall(r"(?<!\d)\d{7,8}(?!\d)", t))

    def _stamps(self, row):
        txt = f"{row.get('Work notes', '')}\n{row.get('Additional comments', '')}"
        out = []
        for ts, who, kind in re.findall(self.STAMP_RE, txt):
            d = self._dt(ts)
            if d:
                out.append((d, who.strip(), kind))
        return out

    def _mmss(self, sec):
        try:
            s = int(float(sec))
        except Exception:
            return "00:00"
        return f"{s // 60:02d}:{s % 60:02d}"

    # ---------- transcript ----------
    def _transcript(self, obj):
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if obj is None:
            return "", {}
        d = getattr(obj, "data", obj)
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                return d, {}
        if not isinstance(d, dict):
            return str(d), {}
        for k in ("clean_transcript", "formatted_transcript", "transcript_text", "transcript", "text"):
            v = d.get(k)
            if isinstance(v, str) and len(v) > 40:
                return v, d
        for k in ("segments", "turns", "utterances"):
            segs = d.get(k)
            if isinstance(segs, list) and segs:
                lines = []
                for s in segs:
                    if isinstance(s, dict):
                        st = s.get("start", s.get("start_s", 0)) or 0
                        sp = s.get("speaker") or s.get("role") or "?"
                        lines.append(f"[{self._mmss(st)}] {sp}: {s.get('text') or ''}")
                return "\n".join(lines), d
        return json.dumps(d, default=str), d

    def _dead_air(self, d):
        if not isinstance(d, dict):
            return None
        for k in ("dead_air", "dead_air_events", "dead_air_gaps", "silences", "long_silence_events"):
            v = d.get(k)
            if isinstance(v, list):
                gaps = []
                for x in v:
                    if not isinstance(x, dict):
                        continue
                    secs = x.get("duration_s", x.get("duration", x.get("seconds")))
                    st = x.get("start", x.get("start_s", 0))
                    try:
                        gaps.append({"start": st if isinstance(st, str) else self._mmss(st),
                                     "seconds": int(round(float(secs)))})
                    except Exception:
                        continue
                return gaps
        return None

    # ---------- ticket matching ----------
    def _families(self, snow, start, end, analyst, search_text):
        cfg = getattr(self, "_mch_cfg", None) or {}
        ws = start - timedelta(minutes=int(self.window_before_min))
        we = end + timedelta(minutes=int(self.window_after_min))
        spoken = self._ticket_ids(search_text)
        runs = self._digit_runs(search_text)
        low = search_text.lower()
        txw = set(self._words(search_text))
        fams = {}
        for r in snow:
            num = str(r.get("Number", "")).strip().upper()
            if not num:
                continue
            s, why = 0, []
            if num in spoken:
                s += 100
                why.append("ticket number spoken on call / in wrap-up notes")
            elif re.sub(r"\D", "", num) in runs:
                s += 80
                why.append("ticket digits spoken on call")
            opened_by = r.get("Opened by")
            op = self._dt(r.get("Opened"))
            in_win = bool(op and ws <= op <= we)
            by_analyst = self._same_person(opened_by, analyst)
            if in_win and by_analyst:
                s += 40
                why.append("opened by analyst in call window")
            elif in_win:
                s += 5
                why.append("opened in call window by another user")
            touched = False
            for d, who, kind in self._stamps(r):
                if ws <= d <= we and self._same_person(who, analyst):
                    s += 30
                    touched = True
                    why.append(f"analyst {kind.lower()} at {d:%H:%M:%S}")
                    break
            if in_win and touched and not by_analyst and self._is_mch(opened_by):
                s += int(cfg.get("handoff_bonus", 25))
                why.append("handoff: opened by MCH team member, analyst worked the ticket")
            if s == 0:
                continue
            for k in self.CONTACT_KEYS:
                toks = self._ntok(r.get(k))
                if len(toks) >= 2 and len(toks[-1]) > 2 and re.search(r"\b" + re.escape(toks[-1]) + r"\b", low):
                    s += 20
                    why.append("caller surname heard on call")
                    break
            sdw = set(self._words(r.get("Short description", "")))
            if sdw:
                ov = len(sdw & txw) / len(sdw)
                if ov >= 0.3:
                    s += int(round(15 * ov))
                    why.append(f"issue words overlap {ov:.0%}")
            parent = str(r.get("Parent", "")).strip().upper()
            key = parent if parent.startswith("CS") else num
            f = fams.setdefault(key, {"score": 0, "members": [], "reasons": []})
            f["members"].append((s, num, r))
            f["score"] = max(f["score"], s)
            f["reasons"] += [w for w in why if w not in f["reasons"]]
        out = []
        for f in fams.values():
            if len(f["members"]) > 1:
                f["score"] += 10
            f["members"].sort(key=lambda m: -m[0])
            nums = [m[1] for m in f["members"]]
            prim = next((n for n in nums if not n.startswith("CS")), nums[0])
            row = next(m[2] for m in f["members"] if m[1] == prim)
            cs = next((n for n in nums if n.startswith("CS")), "")
            par = str(row.get("Parent", "")).strip().upper()
            if not cs and par.startswith("CS"):
                cs = par
            f["primary"], f["cs"], f["primary_row"] = prim, cs, row
            f["label"] = prim + (f" / {cs}" if cs and cs != prim else "")
            f["family"] = sorted(set(nums + ([cs] if cs else [])))
            out.append(f)
        out.sort(key=lambda x: -x["score"])
        return out, ws, we

    def _history(self, snow, fam, start, analyst):
        row = fam["primary_row"]
        family = set(fam["family"])
        contacts = [row.get(k) for k in self.CONTACT_KEYS if len(self._ntok(row.get(k))) >= 2]
        lo = start - timedelta(days=int(self.history_days))
        prior = []
        for r in snow:
            num = str(r.get("Number", "")).strip().upper()
            if num in family or str(r.get("Parent", "")).strip().upper() in family:
                continue
            op = self._dt(r.get("Opened"))
            if not op or not (lo <= op < start):
                continue
            if any(self._same_person(c, r.get(k)) for c in contacts for k in self.CONTACT_KEYS):
                prior.append({"number": num, "opened": self._fmt(r.get("Opened")),
                              "short_description": r.get("Short description", ""),
                              "state": r.get("State", ""), "assignment_group": r.get("Assignment group", ""),
                              "close_notes": str(r.get("Close notes", ""))[:200]})
        cited = sorted(self._ticket_ids(f"{row.get('Short description', '')}\n{row.get('Description', '')}") - family)
        reopen = max(self._num(row.get("Reopen count [Incident]")), self._num(row.get("Reopen count [Case]")))
        return {"contacts": contacts, "prior_tickets": prior[:5], "prior_ticket_count": len(prior),
                "cited_ticket_numbers": cited,
                "cited_by_analyst": bool(cited) and self._same_person(row.get("Opened by"), analyst),
                "reopen_count": reopen,
                "repeat_by_records": bool(prior or cited or reopen > 0)}

    def _tinfo(self, f, ws, we, snow, start, analyst, desc_chars):
        r = f["primary_row"]
        return {
            "label": f["label"], "number": f["primary"], "cs": f["cs"], "score": f["score"],
            "reasons": f["reasons"], "type": r.get("Type", ""), "opened": self._fmt(r.get("Opened")),
            "opened_by": r.get("Opened by", ""), "priority": r.get("Priority", ""), "state": r.get("State", ""),
            "assignment_group": r.get("Assignment group", ""), "assigned_to": r.get("Assigned to", ""),
            "closed_by": r.get("Closed by", ""), "close_notes": str(r.get("Close notes", ""))[:300],
            "short_description": r.get("Short description", ""),
            "contact": next((r.get(k) for k in self.CONTACT_KEYS if str(r.get(k, "")).strip()), ""),
            "reassignment_count": r.get("Reassignment count", ""),
            "fields": self._desc_fields(r.get("Description", "")),
            "description_clean": self._clean_desc(r.get("Description", ""))[:desc_chars],
            "activity_in_window": [f"{d:%m/%d %H:%M:%S} {who} ({kind})"
                                   for d, who, kind in self._stamps(r) if ws <= d <= we][:8],
            "history": self._history(snow, f, start, analyst),
        }

    def _mch_actions(self, row, ws, until):
        acts = []
        ob = row.get("Opened by")
        op = self._dt(row.get("Opened"))
        if op and ws <= op <= until and self._is_mch(ob):
            acts.append({"at": op, "who": self._canon(ob), "what": "opened ticket"})
        for d, who, kind in self._stamps(row):
            if ws <= d <= until and self._is_mch(who):
                acts.append({"at": d, "who": self._canon(who), "what": kind.lower()})
        return acts

    # ---------- KB ----------
    def _kb(self, rows, query, n_full, chars):
        q = set(self._words(query))
        scored = []
        for r in rows:
            st = str(r.get("State", "")).strip().lower()
            if st and st != "published":
                continue
            title = r.get("KB_Title") or r.get("Short description") or ""
            body = self._strip_html(r.get("Clean_Content") or r.get("Article body") or "")
            tw = set(self._words(f"{title} {r.get('Category', '')}"))
            s = 3 * len(q & tw) + min(len(q & set(self._words(body))), 15)
            scored.append({"number": r.get("KB_Number") or r.get("Number") or "", "title": title,
                           "category": r.get("Category", ""), "score": s, "body": body})
        scored.sort(key=lambda x: -x["score"])
        top = [x for x in scored[:3] if x["score"] > 0]
        for i, x in enumerate(top):
            x["full_text"] = x["body"][:chars] if i < n_full else ""
            del x["body"]
        return top, len(scored)

    # ---------- main ----------
    def _run(self):
        if getattr(self, "_mch_cache", None) is not None:
            return self._mch_cache
        cfg = json.loads(self.DEFAULT_CONFIG)
        try:
            cfg.update(json.loads(self.config_json or "{}"))
        except Exception as ex:
            raise ValueError(f"Config JSON is not valid JSON: {ex}")
        self._mch_cfg = cfg
        self._mch_aliases = {str(k).lower(): v for k, v in (cfg.get("name_aliases") or {}).items()}

        cid = str(self.conversation_id or "").strip().lower()
        flags = []
        grows = self._table(self._path(self.genesys_file), "Conversation ID")
        g = next((r for r in grows if str(r.get("Conversation ID", "")).strip().lower() == cid), None)
        if g is None:
            raise ValueError(f"Conversation {cid} not found in the Genesys export")
        start = self._dt(self._g(g, "Date"))
        if start is None:
            raise ValueError("Could not read the Genesys 'Date' column")
        dur_ms = self._num(self._g(g, "Duration (ms)", "Duration"))
        end = max(self._dt(self._g(g, "End Date")) or start, start + timedelta(milliseconds=dur_ms))
        users = [u.strip() for u in re.split(r"[;,]", self._g(g, "Users")) if u.strip()]
        analyst = self._canon(users[0]) if users else ""
        if len(users) > 1:
            flags.append("multiple_users_on_conversation")

        team = cfg.get("mch_team") or []
        analyst_is_mch = (not team) or self._is_mch(analyst)
        queue = self._g(g, "Queue")
        overflow = any(str(m).lower() in queue.lower() for m in (cfg.get("overflow_queue_markers") or []))
        if not analyst_is_mch:
            flags.append("non_mch_analyst")
        if overflow:
            flags.append("overflow_queue")

        h_ms = self._num(self._g(g, "Total Hold (ms)", "Total Hold"))
        holds = int(self._num(self._g(g, "Hold Segments")))
        if h_ms > 0 and holds == 0:
            holds = 1
        call = {
            "analyst": analyst, "users": users, "queue": queue,
            "analyst_is_mch": analyst_is_mch, "overflow": overflow,
            "start_et": f"{start:%Y-%m-%d %H:%M}", "end_et": f"{end:%Y-%m-%d %H:%M}",
            "call_date": f"{start:%m/%d/%Y}", "call_time_et": f"{start:%H:%M}", "weekday": start.weekday(),
            "duration_sec": round(dur_ms / 1000), "duration_min": round(dur_ms / 60000, 1),
            "queue_wait_sec": round(self._num(self._g(g, "Total Queue (ms)", "Total Queue")) / 1000),
            "alert_sec": round(self._num(self._g(g, "Total Alert (ms)", "Total Alert")) / 1000),
            "holds": holds, "hold_sec": round(h_ms / 1000),
            "acw_sec": round(self._num(self._g(g, "Total ACW (ms)", "Total ACW")) / 1000),
            "transferred": self._g(g, "Transferred"), "disconnect_type": self._g(g, "Disconnect Type"),
            "wrap_up": self._g(g, "Wrap-up"),
        }

        # ---- transcript ----
        tx_text, tx_data = self._transcript(self.transcript)
        lines = [ln for ln in tx_text.splitlines() if "not agent-facing" not in ln.lower()]
        tx_text = "\n".join(lines)[: int(self.max_transcript_chars)]
        not_ready = re.search(r"not[_ ]ready|pending|processing|queued|in[_ ]progress|restoring|\b202\b",
                              tx_text[:2000], flags=re.I)
        tx_ok = len(tx_text) >= 200 and not (not_ready and "AGENT" not in tx_text.upper())
        if analyst_is_mch and not tx_ok:
            raise ValueError(f"TRANSCRIPT_NOT_READY for {cid}: audio or transcription not finished. "
                             f"Wait a few minutes and run again. Received: {tx_text[:300]!r}")
        if not tx_ok:
            tx_text = ""

        if isinstance(tx_data, dict):
            if (tx_data.get("repeated_segment_count") or 0) > 0 or (tx_data.get("loops_collapsed") or 0) > 0:
                flags.append("transcript_repetition_detected")
            try:
                if float(tx_data.get("agent_confidence") or 1) < 0.90:
                    flags.append("low_agent_transcript_confidence")
            except (TypeError, ValueError):
                pass

        snow = self._table(self._path(self.snow_file), "Number")
        latest = max((d for d in (self._dt(r.get("Opened")) for r in snow) if d), default=None)
        if latest is None or latest < end:
            flags.append("snow_export_ends_before_call")

        search_text = f"{tx_text}\n{self._g(g, 'Wrap-up Notes')}"
        fams, ws, we = self._families(snow, start, end, analyst, search_text)
        best = fams[0]["score"] if fams else 0
        second = fams[1]["score"] if len(fams) > 1 else 0
        if best >= 100 or (best >= 60 and best - second >= 20):
            status = "MATCHED"
        elif best >= 30:
            status = "AMBIGUOUS"
        else:
            status = "NOT_FOUND"
        dc = int(cfg.get("description_chars", 1000))
        cands = [self._tinfo(f, ws, we, snow, start, analyst, dc) for f in fams[:3]]
        selected = cands[0] if status == "MATCHED" else None

        # ---- overflow: keep only when the MCH team actioned the ticket ----
        if not analyst_is_mch:
            if status != "MATCHED":
                cand_txt = "; ".join(f"{c['label']} (score {c['score']})" for c in cands) or "none"
                raise ValueError(f"SKIPPED_OVERFLOW_NO_MCH_ACTION for {cid}: answered by non-MCH analyst {analyst}; "
                                 f"no ticket confidently matched (status {status}; candidates: {cand_txt}).")
            until = end + timedelta(hours=int(cfg.get("mch_action_hours", 24)))
            acts = []
            for _s, _n, mrow in fams[0]["members"]:
                acts += self._mch_actions(mrow, ws, until)
            seen, uniq = set(), []
            for a in sorted(acts, key=lambda x: x["at"]):
                key = (a["at"], a["who"], a["what"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(a)
            if not uniq:
                raise ValueError(f"SKIPPED_OVERFLOW_NO_MCH_ACTION for {cid}: ticket {selected['label']} "
                                 f"has no action by the MCH team within {cfg.get('mch_action_hours', 24)}h.")
            opener = next((a["who"] for a in uniq if a["what"] == "opened ticket"), "")
            mch_owner = opener or uniq[0]["who"]
            mch_actions = [f"{a['at']:%m/%d %H:%M} {a['who']} - {a['what']}" for a in uniq]
            summary = (f"[Overflow - answered by {analyst} (non-MCH); call not scored] "
                       f"MCH action on {selected['label']}: " + "; ".join(mch_actions[:6])
                       + f". Current group: {selected.get('assignment_group') or 'n/a'}; "
                         f"state: {selected.get('state') or 'n/a'}; priority: {selected.get('priority') or 'n/a'}.")
            ev = {
                "schema_version": "mch_evidence.v1", "conversation_id": cid,
                "review_id": f"MCH-{start:%Y%m%d}-{cid[:8]}", "flags": flags, "call": call,
                "mode": "overflow_mch_action", "overflow_analyst": analyst,
                "mch_owner": mch_owner, "mch_actions": mch_actions, "overflow_summary": summary,
                "ticket": {"status": status, "selected": selected, "candidates": cands},
                "kb": [], "kb_articles_available": 0, "dead_air_code": None,
                "transcript_chars": len(tx_text),
            }
            packet = ("=== CALL METADATA (VERIFIED - Genesys; do not contradict) ===\n"
                      "OVERFLOW CALL - NOT SCORED. The call was answered by a non-MCH overflow analyst. "
                      "Set transcript_usable to false, every score to null and every fact to null.\n\n"
                      "=== TRANSCRIPT ===\n[NO TRANSCRIPT - overflow call not scored]")
            self._mch_cache = (ev, packet)
            return self._mch_cache

        kb_rows = self._table(self._path(self.kb_file), "State") if self.kb_file else []
        ref = selected or (cands[0] if cands else {})
        query = f"{ref.get('short_description', '')} {ref.get('fields', {}).get('application', '')} {tx_text[:4000]}"
        kb, kb_total = self._kb(kb_rows, query, int(cfg.get("kb_full_text_count", 1)), int(cfg.get("kb_chars", 2500)))

        ev = {
            "schema_version": "mch_evidence.v1", "conversation_id": cid,
            "review_id": f"MCH-{start:%Y%m%d}-{cid[:8]}", "flags": flags, "call": call,
            "mode": "mch_call",
            "ticket": {"status": status, "selected": selected, "candidates": cands},
            "kb": [{"number": k["number"], "title": k["title"], "score": k["score"]} for k in kb],
            "kb_articles_available": kb_total,
            "dead_air_code": self._dead_air(tx_data),
            "transcript_chars": len(tx_text),
        }

        p = ["=== CALL METADATA (VERIFIED - Genesys; do not contradict) ===",
             f"Conversation: {cid} | Analyst: {analyst} | Queue: {queue}",
             f"Start (ET): {call['start_et']} | Duration: {call['duration_min']} min | Queue wait: {call['queue_wait_sec']} s",
             f"Holds: {holds} | Total hold: {call['hold_sec']} s | Transferred: {call['transferred']} | Wrap-up: {call['wrap_up']}"]
        if flags:
            p.append("Code flags: " + ", ".join(flags))
        p += ["", f"=== TICKET MATCH (code) === Status: {status}"]
        for i, c in enumerate(cands, 1):
            p += [f"Candidate {i}: {c['label']} | score {c['score']} | {'; '.join(c['reasons'])}",
                  f"  Type {c['type']} | Opened {c['opened']} by {c['opened_by']} | Priority {c['priority']} | State {c['state']} | Group {c['assignment_group']} | Contact {c['contact']}",
                  f"  Short description: {c['short_description']}",
                  f"  Fields: {json.dumps(c['fields'])}",
                  f"  Analyst activity in window: {', '.join(c['activity_in_window']) or 'none'}",
                  f"  Description: {c['description_clean']}",
                  f"  PRIOR CONTACT: prior tickets same caller ({self.history_days}d): "
                  + ("; ".join(f"{x['number']} {x['opened']} {x['short_description']} [{x['state']}]" for x in c['history']['prior_tickets']) or "none")
                  + f" | cited in ticket: {', '.join(c['history']['cited_ticket_numbers']) or 'none'}"
                  + f" | reopen count: {int(c['history']['reopen_count'])}"]
        if not cands:
            p.append("No candidate tickets found in the export window.")
        p += ["", f"=== MCH KB SHORTLIST ({kb_total} published MCH articles) ==="]
        for k in kb:
            p.append(f"{k['number']} | {k['title']} | {k['category']}")
        for k in kb:
            if k["full_text"]:
                p += [f"--- {k['number']} full text ---", k["full_text"]]
        p += ["", "=== ALLOWED VALUES ===",
              "caller_role: " + json.dumps(cfg.get("allowed_caller_role", [])),
              "issue_type: " + json.dumps(cfg.get("allowed_issue_type", [])),
              "", "=== TRANSCRIPT ===", tx_text]

        self._mch_cache = (ev, "\n".join(p))
        return self._mch_cache

    def build_evidence(self) -> Data:
        ev, _ = self._run()
        self.status = f"{ev['ticket']['status']} | {ev['call']['analyst']} | flags: {', '.join(ev['flags']) or 'none'}"
        return Data(data=ev)

    def build_packet(self) -> Message:
        _, packet = self._run()
        return Message(text=packet)
