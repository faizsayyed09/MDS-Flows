import io
import re
import base64
import zipfile
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, IntInput, StrInput, SecretStrInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TICKET_RE = re.compile(r"\b(?:INC|RITM|REQ|TASK|CHG|PRB|CS)\s?\d{6,}\b", re.IGNORECASE)
KB_RE = re.compile(r"\bKB\d{6,}\b", re.IGNORECASE)
LINE_RE = re.compile(r"^\[(\d{1,3}):(\d{2})\]\s+(AGENT|CALLER|IVR):\s*(.*)$")
PHONE_RE = re.compile(r"(?<![\w-])\+?\d[\d \t().-]{5,}\d(?![\w-])")
DIGITS_RE = re.compile(r"(?<!\d)\d{5,15}(?!\d)")
NUMLIKE_RE = re.compile(r"\+?\d[\d \t().\-]{3,}\d|\d{5,}")
EMAIL_RE = re.compile(r"[\w.\-]+@[\w\-]+(?:\.[\w\-]+)+")
ID_RE = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{5,}\b")
SPELLED_RE = re.compile(r"(?<![A-Za-z0-9'])(?:[A-Za-z0-9][ \t\-]{1,3}){3,}[A-Za-z0-9](?![A-Za-z0-9])")
NAME_RE = re.compile(
    r"(?i:\bmy name is|\bmy name's|\bfull name is|\bthis is)\s+"
    r"([A-Z][A-Za-z'\-]+(?:[ ,]+[A-Z][A-Za-z'\-]+){0,3})"
)
ADDRESS_RE = re.compile(
    r"(?i:\b(?:thank you|thanks|okay|alright|sure|and|hi|hello))[,]?\s+([A-Z][a-z]{2,})\b"
)
PASSWORD_RE = re.compile(r"pass\s?word|passcode|\bpin\b|\bcode is\b", re.IGNORECASE)
HELP_RE = re.compile(r"how (?:may|can|could) i (?:help|assist)|what can i (?:do|help)|can i help", re.IGNORECASE)
HOLD_RE = re.compile(
    r"hold on|can you hold|give me (?:a|one|two|1|2) (?:sec|second|minute|min|moment)"
    r"|one (?:sec|second|moment|minute)|bear with me|let me (?:check|get|find|grab|look)",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\b(?:department|dept|unit|building|floor|room|clinic|ward|campus|station|office|site)\b",
    re.IGNORECASE,
)
NOTE_RE = re.compile(
    r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}) - ([^\n(]+?) \((?:Work notes|Additional comments)\)"
)
ROW_FORMATS = ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M")
XNS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
BAD_STATUS = ("deleted", "error", "failed", "archived")

NOT_NAMES = set("""
Thank Thanks Please Sure Okay Yes Yeah You Your The This That What Can May Let Have Bye Just Now
Good Great Perfect Alright Hello One Give Got Sir Madam Maam Take Right Well Hold Welcome
""".split())

STOP = set("""
that this with have from your there their they them then than what when where which while would could
should about after again also been before being both come does doing done down each even ever every
here into just know like make many more most much must need okay other over please really right same
some still sure take tell thank thanks these thing things think those through today told very want
wanted well were will yeah yours call calling called help number ticket name hello good morning
evening afternoon able sorry welcome anything else issue problem working work time mean actually
because something going check give minute hold back line service desk direct extension
""".split())

_EXPORT_CACHE = {"key": None, "rows": [], "label": "", "uploaded_et": None}


# ---------------- small helpers ----------------

def _alnum(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique(items, limit):
    out = []
    for i in items:
        if i and i not in out:
            out.append(i)
        if len(out) >= limit:
            break
    return out


def _clip(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def _words(s):
    out = set()
    for w in re.findall(r"[a-z]{4,}", str(s or "").lower()):
        if w in STOP:
            continue
        if len(w) > 5 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def _tokens(s):
    return [t for t in re.findall(r"[a-z]{3,}", str(s or "").lower()) if t not in STOP]


def _similar(a, b):
    return a == b or (min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.8)


def _name_like(value, names):
    vt = _tokens(value)
    for n in names:
        nt = _tokens(n)
        if nt and vt and _similar(nt[-1], vt[-1]):
            return True
    return False


def _mmss(sec):
    m, s = divmod(int(float(sec)), 60)
    return "%02d:%02d" % (m, s)


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "unknown"


# ---------------- time ----------------

def _utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_eastern(dt):
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        u = dt.astimezone(timezone.utc).replace(tzinfo=None)
        mar = datetime(u.year, 3, 8)
        mar += timedelta(days=(6 - mar.weekday()) % 7)
        nov = datetime(u.year, 11, 1)
        nov += timedelta(days=(6 - nov.weekday()) % 7)
        edt = (mar + timedelta(hours=7)) <= u < (nov + timedelta(hours=6))
        return u - timedelta(hours=4 if edt else 5)


def _row_dt(value):
    s = str(value or "").strip()
    if not s:
        return None
    try:
        f = float(s)
        if 20000 < f < 80000:
            return datetime(1899, 12, 30) + timedelta(seconds=round(f * 86400))
    except ValueError:
        pass
    for fmt in ROW_FORMATS:
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


# ---------------- xlsx (stdlib only) ----------------

def _col(ref):
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _read_xlsx(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(XNS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(XNS + "t")))
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if not sheets:
        return []
    table = []
    for row in ET.fromstring(zf.read(sheets[0])).iter(XNS + "row"):
        cells = {}
        for c in row.iter(XNS + "c"):
            idx = _col(c.get("r", ""))
            kind = c.get("t")
            if kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(XNS + "t"))
            else:
                v = c.find(XNS + "v")
                val = v.text if (v is not None and v.text is not None) else ""
                if kind == "s" and val != "":
                    val = shared[int(val)]
            if idx >= 0:
                cells[idx] = val
        if cells:
            table.append([cells.get(i, "") for i in range(max(cells) + 1)])
    if not table:
        return []
    header = [str(h).strip() for h in table[0]]
    rows = []
    for r in table[1:]:
        rows.append({header[i]: (r[i] if i < len(r) else "") for i in range(len(header)) if header[i]})
    for r in rows:
        for k in list(r.keys()):
            kl = k.lower()
            if kl.startswith(("opened", "updated")) and "by" not in kl:
                dt = _row_dt(r[k])
                if dt:
                    r[k] = dt.strftime("%Y-%m-%d %H:%M:%S")
    return rows


def _get(row, *keys):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def _persons(row):
    out = []
    for k, v in row.items():
        kl = str(k).lower()
        if v and ("requestor" in kl or "requested for" in kl or kl.startswith("contact [")
                  or kl in ("contact", "caller")):
            out.append(str(v))
    return " ".join(out)


def _compact(row, limit):
    fields = (
        ("Number", ("Number",)),
        ("Type", ("Type",)),
        ("Opened (ET)", ("Opened",)),
        ("Opened by", ("Opened by",)),
        ("Updated (ET)", ("Updated", "Updated(u_updated_on)")),
        ("Updated by", ("Updated by", "Updated by(u_updated_by)")),
        ("Company", ("Company",)),
        ("Short description", ("Short description",)),
        ("Contact type", ("Contact type",)),
        ("Assignment group", ("Assignment group",)),
        ("Priority", ("Priority",)),
        ("State", ("State",)),
        ("Parent", ("Parent",)),
    )
    head = []
    for label, keys in fields:
        v = _get(row, *keys).strip()
        if v:
            head.append(label + ": " + v)
    p = _persons(row).strip()
    if p:
        head.append("Requester/Contact: " + p)
    out = ["  " + " | ".join(head)]
    for key in ("Description", "Work notes", "Additional comments"):
        v = re.sub(r"\[/?code\]|<[^>]+>", " ", _get(row, key))
        v = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", v)).strip()
        if v:
            out.append("  " + key + ": " + _clip(v, limit))
    return "\n".join(out)


def _newest_activity(rows):
    best = None
    for r in rows:
        for key in ("Opened", "Updated", "Updated(u_updated_on)"):
            dt = _row_dt(_get(r, key))
            if dt and (best is None or dt > best):
                best = dt
    return best


def _row_numbers(text):
    out = set()
    for m in NUMLIKE_RE.finditer(text):
        d = _digits(m.group(0))
        if len(d) >= 5:
            out.add(d)
        if len(d) >= 7:
            out.add(d[-7:])
    return out


# ---------------- interaction parsing ----------------

def _spelled(text):
    out = []
    for m in SPELLED_RE.finditer(text):
        if PASSWORD_RE.search(text[max(0, m.start() - 80):m.start()]):
            continue
        w = _alnum(m.group(0))
        if len(w) >= 4:
            out.append(w)
    return out


def _identifiers(lines, agent_names):
    caller = " ".join(t for sp, _, t in lines if sp == "CALLER")
    agent = " ".join(t for sp, _, t in lines if sp == "AGENT")
    spoken = " ".join(t for sp, _, t in lines if sp != "IVR")

    tickets = _unique([re.sub(r"\s", "", t).upper() for t in TICKET_RE.findall(spoken)], 8)
    ticket_digits = {_digits(t) for t in tickets}

    spelled = _spelled(spoken)
    numbers = [_digits(m) for m in PHONE_RE.findall(spoken)]
    numbers += DIGITS_RE.findall(spoken)
    numbers += [s for s in spelled if s.isdigit()]
    numbers = _unique([n for n in numbers if 5 <= len(n) <= 15 and n not in ticket_digits], 8)

    ids = [_alnum(m) for m in ID_RE.findall(spoken) if not TICKET_RE.match(m)]
    ids += [s for s in spelled if not s.isdigit()]
    ids = _unique([i for i in ids if not i.isdigit()], 8)

    names = _unique([n.strip(" ,") for n in NAME_RE.findall(caller)], 3)
    own = {t for n in agent_names for t in _tokens(n)}
    heard = []
    for n in ADDRESS_RE.findall(agent):
        if n in NOT_NAMES or n.lower() in own:
            continue
        heard.append(n)
    heard = _unique(heard, 3)

    return {
        "tickets": tickets,
        "numbers": numbers,
        "phones": [n for n in numbers if len(n) >= 7],
        "ids": ids,
        "emails": _unique([e.lower() for e in EMAIL_RE.findall(spoken.replace(" .", "."))], 3),
        "names": names,
        "names_heard": heard,
        "name_tokens": _unique([t for n in names + heard for t in _tokens(n)], 8),
    }


def _issue_turns(spoken):
    start = None
    for i, (sp, _, t) in enumerate(spoken):
        if sp == "AGENT" and HELP_RE.search(t):
            start = i + 1
            break
    if start is None:
        start = min(3, len(spoken))
    return spoken[start:start + 10]


def _caller_pauses(lines, silences):
    callers = [(s, t) for sp, s, t in lines if sp == "CALLER"]
    out = []
    for ev in silences or []:
        try:
            st = float(ev.get("start"))
            dur = float(ev.get("duration") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        asks = [(s, t) for s, t in callers if 0 <= st - s <= 25 and HOLD_RE.search(t)]
        if asks:
            s, t = asks[-1]
            out.append({
                "silence_start": _mmss(st),
                "silence_sec": int(round(dur)),
                "requested_at": _mmss(s),
                "caller_said": _clip(t, 100),
            })
    return out


# ---------------- scoring ----------------

def _score(row, ctx):
    number = _get(row, "Number").strip().upper()
    if not number:
        return None
    text = " ".join(str(v) for v in row.values() if v)
    alnum = _alnum(text)
    notes = _get(row, "Work notes") + "\n" + _get(row, "Additional comments")
    lo, hi = ctx["lo"], ctx["hi"]

    email = _get(row, "Email", "Opened by.Email").strip().lower()
    by_agent = (email in ctx["emails"]) or _name_like(_get(row, "Opened by"), ctx["names"])
    opened = _row_dt(_get(row, "Opened"))
    updated = _row_dt(_get(row, "Updated", "Updated(u_updated_on)"))
    in_window = bool(opened and lo <= opened <= hi)

    touched = False
    for ts, who in NOTE_RE.findall(notes):
        t = _row_dt(ts)
        if t and lo <= t <= hi and _name_like(who, ctx["names"]):
            touched = True
            break
    if not touched and updated and lo <= updated <= hi:
        if _name_like(_get(row, "Updated by", "Updated by(u_updated_by)"), ctx["names"]):
            touched = True

    parent = _get(row, "Parent").strip().upper()
    explicit = [t for t in ctx["tickets"]
                if t == number or t == parent or (t.startswith("CS") and t in alnum)]

    if not (by_agent or touched or explicit):
        return None

    score, reasons = 0, []
    if explicit:
        score += 100
        reasons.append("number from the interaction links to this row: " + ", ".join(explicit))
    if by_agent and in_window:
        score += 30
        reasons.append("opened by the agent at " + _fmt(opened) + " ET, inside the window")
        if ctx["start"] - timedelta(minutes=5) <= opened <= ctx["end"] + timedelta(minutes=45):
            score += 10
    elif touched:
        score += 20
        reasons.append("agent noted/updated this ticket inside the window")
    elif by_agent:
        reasons.append("opened by the agent but OUTSIDE the window (" + _fmt(opened) + " ET)")

    persons = _tokens(_persons(row))
    name_hits = [n for n in ctx["name_tokens"] if any(_similar(n, p) for p in persons)]
    if name_hits:
        score += min(25, 10 * len(name_hits))
        reasons.append("caller name matches requester/contact (" + ", ".join(name_hits) + ")")
    else:
        desc = _tokens(_get(row, "Description"))
        name_hits = [n for n in ctx["name_tokens"] if len(n) >= 4 and any(_similar(n, p) for p in desc)]
        if name_hits:
            score += min(15, 6 * len(name_hits))
            reasons.append("caller name found in description (" + ", ".join(name_hits) + ")")

    row_nums = _row_numbers(text)
    num_hits = [n for n in ctx["numbers"] if n in row_nums or (len(n) >= 7 and n[-7:] in row_nums)]
    if num_hits:
        score += min(35, 25 + 5 * (len(num_hits) - 1))
        reasons.append("caller numbers found in ticket (" + ", ".join(num_hits) + ")")

    id_hits = [i for i in ctx["ids"] if len(i) >= 6 and i in alnum]
    if id_hits:
        score += min(30, 15 * len(id_hits))
        reasons.append("IDs/assets found in ticket (" + ", ".join(id_hits) + ")")

    row_words = _words(_get(row, "Short description") + " " + _get(row, "Description"))
    common = sorted(w for w in ctx["issue_words"]
                    if w in row_words and ctx["df"].get(w, 0) <= ctx["df_cap"])
    if common:
        score += min(20, 5 * len(common))
        reasons.append("issue words: " + ", ".join(common[:6]))

    ctype = _get(row, "Contact type").lower()
    if ctx["channel"] == "CALL":
        channel_hit = "phone" in ctype
    else:
        channel_hit = "email" in ctype
    if channel_hit:
        score += 5

    phone_hits = [n for n in num_hits if len(n) >= 7]
    return {
        "number": number,
        "score": score,
        "reasons": reasons,
        "opened_et": _fmt(opened),
        "row": row,
        "facts": {
            "opened_by_agent": by_agent,
            "opened_in_window": in_window,
            "agent_touched_in_window": touched,
            "caller_name_in_ticket": bool(name_hits),
            "caller_phone_in_ticket": (bool(phone_hits) if ctx["phones"] else None),
            "contact_type_matches_channel": channel_hit,
            "kb_articles_in_ticket": _unique([k.upper() for k in KB_RE.findall(notes)], 5),
        },
    }


INSTRUCTIONS = {
    "MATCHED": (
        "MATCHED: use Candidate 1 as the matched ticket unless its content clearly contradicts the "
        "interaction. Write issueSummary from the Issue discussion. Return JSON only."
    ),
    "AMBIGUOUS": (
        "AMBIGUOUS: choose between the candidates using the caller identifiers and the Issue "
        "discussion. If none fits, return NOT_FOUND. Return JSON only."
    ),
    "NOT_FOUND": (
        "NOT_FOUND: code found no fitting ticket. Return NOT_FOUND unless a listed candidate clearly "
        "matches the caller and issue. Write issueSummary from the Issue discussion. Return JSON only."
    ),
    "PENDING_EXPORT": (
        "PENDING_EXPORT: the export was uploaded before this interaction's ticket could be exported. "
        "Return NOT_FOUND with match_confidence NONE and write issueSummary from the Issue discussion. "
        "Return JSON only."
    ),
    "NO_EXPORT": (
        "SEARCH MODE: code could not read the export. Search the attached ServiceNow export yourself: "
        "find the row whose Email column equals the agent email (or Opened by / Updated by / note author "
        "is the agent) with Opened, Updated or an agent note inside the US Eastern search window, and "
        "whose caller identifiers and issue match the Issue discussion. Return MATCHED only with evidence "
        "from a row you can see; otherwise NOT_FOUND. Return JSON only."
    ),
}


class CustomComponent(Component):
    display_name = "Call Fingerprint + SNOW Pre-Match"
    description = (
        "Downloads the latest SNOW xlsx from the SNOW mission folder (Matcha file API), scores every row "
        "deterministically and builds the fingerprint for the SNOW mission."
    )

    inputs = [
        DataInput(name="call_data", display_name="Call Data", required=True,
                  info="Structured Data output of the QA Transcriber or Email Fetcher."),
        StrInput(name="matcha_base_url", display_name="Matcha Base URL",
                 value="https://matcha.harriscomputer.com"),
        SecretStrInput(name="matcha_api_key", display_name="Matcha API Key", required=True),
        StrInput(name="snow_mission_id", display_name="SNOW Mission ID", value="50641"),
        StrInput(name="snow_folder_id", display_name="SNOW Folder ID", value="31790"),
        IntInput(name="window_minutes", display_name="Match Window (minutes)", value=60),
        IntInput(name="top_candidates", display_name="Candidates Sent to Mission", value=3, advanced=True),
        IntInput(name="turn_chars", display_name="Max Characters per Turn", value=200, advanced=True),
        IntInput(name="row_chars", display_name="Max Characters per Ticket Field", value=700, advanced=True),
        IntInput(name="pending_grace_min", display_name="Pending-Export Grace (minutes)", value=60,
                 advanced=True,
                 info="NOT_FOUND becomes PENDING_EXPORT if the export was uploaded before call end + this."),
    ]

    outputs = [
        Output(display_name="Fingerprint", name="fingerprint", method="build_fingerprint", group_outputs=True),
        Output(display_name="Pre-Match Data", name="prematch", method="build_prematch", group_outputs=True),
    ]

    # ---------------- inputs ----------------

    def _call(self):
        cd = self.call_data
        if isinstance(cd, list) and cd:
            cd = cd[0]
        if hasattr(cd, "data"):
            cd = cd.data or {}
        return cd if isinstance(cd, dict) else {}

    # ---------------- Matcha file API ----------------

    def _headers(self):
        return {
            "MATCHA-API-KEY": str(self.matcha_api_key or "").strip(),
            "Accept": "application/json",
            "User-Agent": UA,
        }

    def _list_files(self, base):
        params = {}
        folder = _digits(self.snow_folder_id)
        mission = _digits(self.snow_mission_id)
        if folder:
            params["folder_id"] = folder
        if mission:
            params["mission_id"] = mission
        if not params:
            raise RuntimeError("SNOW mission ID and folder ID are both blank")
        r = requests.get(base + "/rest/api/v1/files", params=params, headers=self._headers(), timeout=45)
        if r.status_code != 200:
            raise RuntimeError("HTTP " + str(r.status_code) + ": " + _clip(r.text, 120))
        data = r.json()
        if isinstance(data, dict):
            data = data.get("data") or data.get("files") or data.get("output") or []
        files = []
        for f in data if isinstance(data, list) else []:
            if not isinstance(f, dict):
                continue
            if not str(f.get("filename") or "").lower().endswith(".xlsx"):
                continue
            if str(f.get("status") or "").lower() in BAD_STATUS:
                continue
            if _int(f.get("id"), 0) <= 0:
                continue
            files.append(f)
        files.sort(key=lambda f: (str(f.get("updated_at") or ""), _int(f.get("id"), 0)), reverse=True)
        return files

    def _download(self, base, file_id):
        headers = self._headers()
        headers["Accept"] = "*/*"
        r = requests.get(base + "/rest/api/v1/download", params={"file_id": file_id},
                         headers=headers, timeout=120)
        if r.status_code != 200:
            raise RuntimeError("download HTTP " + str(r.status_code))
        data = r.content or b""
        if data[:2] == b"PK":
            return data
        if data[:4] == b"UEsD":
            return base64.b64decode(data)
        raise RuntimeError("download is not an xlsx (first bytes " + repr(data[:8]) + ")")

    def _from_cache(self, note):
        if _EXPORT_CACHE["rows"]:
            return (_EXPORT_CACHE["rows"], note + "; using cached export",
                    _EXPORT_CACHE["label"] + " (cached)", _EXPORT_CACHE["uploaded_et"])
        return [], note, "", None

    def _load_export(self):
        base = (self.matcha_base_url or "").strip().rstrip("/")
        if not base or not str(self.matcha_api_key or "").strip():
            return [], "Matcha base URL or API key missing", "", None
        try:
            files = self._list_files(base)
        except Exception as e:
            return self._from_cache("file listing failed: " + str(e)[:200])
        if not files:
            return self._from_cache("no .xlsx in SNOW folder " + str(self.snow_folder_id))

        errors = []
        for i, f in enumerate(files[:3]):
            fid = _int(f.get("id"), 0)
            stamp = str(f.get("updated_at") or "")
            uploaded_et = _to_eastern(_utc(stamp))
            label = (str(f.get("filename")) + " (file " + str(fid) + ", uploaded "
                     + _fmt(uploaded_et) + " ET)")
            cache_key = (fid, stamp)
            if _EXPORT_CACHE["key"] == cache_key and _EXPORT_CACHE["rows"]:
                return _EXPORT_CACHE["rows"], "", _EXPORT_CACHE["label"], _EXPORT_CACHE["uploaded_et"]
            try:
                rows = _read_xlsx(self._download(base, fid))
            except Exception as e:
                errors.append("file " + str(fid) + ": " + str(e)[:160])
                continue
            if not rows:
                errors.append("file " + str(fid) + ": no rows")
                continue
            _EXPORT_CACHE.update({"key": cache_key, "rows": rows, "label": label, "uploaded_et": uploaded_et})
            note = "" if i == 0 else "newest file unreadable (" + "; ".join(errors) + "); used an older file"
            return rows, note, label, uploaded_et

        return self._from_cache("; ".join(errors) or "export download failed")

    # ---------------- main ----------------

    def _run(self):
        if getattr(self, "_fp", None) is not None:
            return self._fp

        call = self._call()
        if call.get("status") != "DONE":
            self._fp = {"skipped": True, "status": "SKIPPED"}
            return self._fp

        channel = "EMAIL" if "EMAIL" in str(call.get("channel") or "").upper() else "CALL"
        start = _utc(call.get("call_start"))
        end = _utc(call.get("call_end")) or start
        s_et, e_et = _to_eastern(start), _to_eastern(end)
        win = timedelta(minutes=_int(self.window_minutes, 60))
        lo = (s_et - win) if s_et else None
        hi = (e_et + win) if e_et else None

        transcript = str(call.get("clean_transcript") or "").replace("(?)", "")
        lines = []
        for raw in transcript.splitlines():
            m = LINE_RE.match(raw.strip())
            if m:
                lines.append((m.group(3), int(m.group(1)) * 60 + int(m.group(2)), m.group(4)))
        if not lines and transcript.strip():
            lines = [("CALLER", 0, transcript)]
        spoken = [x for x in lines if x[0] != "IVR"]

        agent_names = [n.strip() for n in str(call.get("agent_names") or "").split(",") if n.strip()]
        agent_emails = {str(e).strip().lower() for e in (call.get("agent_emails") or []) if e}

        ids = _identifiers(lines, agent_names)
        issue = _issue_turns(spoken)
        issue_words = _words(" ".join(t for _, _, t in issue))
        issue_excerpt = _clip(" ".join(t for sp, _, t in issue if sp == "CALLER"), 600)
        locations = _unique([_clip(t, 140) for sp, _, t in spoken
                             if sp == "CALLER" and LOCATION_RE.search(t)], 4)
        pauses = _caller_pauses(lines, call.get("long_silence_events"))

        rows, err, label, uploaded_et = self._load_export()
        newest = _newest_activity(rows)

        df = {}
        for r in rows:
            for w in _words(_get(r, "Short description") + " " + _get(r, "Description")):
                df[w] = df.get(w, 0) + 1

        ctx = {
            "lo": lo, "hi": hi, "start": s_et, "end": e_et, "channel": channel,
            "emails": agent_emails, "names": agent_names, "tickets": ids["tickets"],
            "name_tokens": ids["name_tokens"], "numbers": ids["numbers"], "phones": ids["phones"],
            "ids": ids["ids"], "issue_words": issue_words, "df": df,
            "df_cap": max(3, len(rows) // 10),
        }

        score_lo, score_hi = lo, hi

        if score_lo is None and ids["tickets"]:
            score_lo, score_hi = datetime(1900, 1, 1), datetime(2100, 1, 1)

        ctx["lo"], ctx["hi"] = score_lo, score_hi

        scored = []

        if rows and score_lo:
            scored = [s for s in (_score(r, ctx) for r in rows) if s]

        scored.sort(key=lambda s: s["score"], reverse=True)

        top = scored[:max(1, _int(self.top_candidates, 3))]

        if not rows:
            status, conf = "NO_EXPORT", "LOW"

        elif not top or top[0]["score"] < 45:
            status, conf = "NOT_FOUND", "LOW"

            grace = timedelta(minutes=_int(self.pending_grace_min, 60))
            ref = uploaded_et or newest

            if e_et and ref and ref < e_et + grace:
                status = "PENDING_EXPORT"

        else:
            best = top[0]["score"]
            second = top[1]["score"] if len(top) > 1 else 0

            if best >= 100 or (best >= 70 and best - second >= 25):
                status, conf = "MATCHED", "HIGH"
            else:
                status, conf = "AMBIGUOUS", "MEDIUM"

        best_ticket = (
            top[0]["number"]
            if (top and status in ("MATCHED", "AMBIGUOUS"))
            else ""
        )

        per_turn = _int(self.turn_chars, 200)
        out = [
            ("EMAIL" if channel == "EMAIL" else "CALL") + " FINGERPRINT",
            "Channel: " + channel,
            "Agent (Opened by): " + (", ".join(agent_names) or "unknown"),
            "Agent email: " + (", ".join(sorted(agent_emails)) or "unknown"),
            "Interaction (US Eastern): " + _fmt(s_et) + " to " + _fmt(e_et),
            "Search window (US Eastern, same zone as the export): " + _fmt(lo) + " to " + _fmt(hi),
            "Ticket/case numbers stated: " + (", ".join(ids["tickets"]) or "none"),
            "",
            "CALLER IDENTIFIERS:",
            "  Names stated: " + (", ".join(ids["names"]) or "none"),
            "  Names heard (agent addressing caller, low confidence): " + (", ".join(ids["names_heard"]) or "none"),
            "  Numbers (phone / extension / employee ID): " + (", ".join(ids["numbers"]) or "none"),
            "  Emails: " + (", ".join(ids["emails"]) or "none"),
            "  IDs / assets / spelled-out codes: " + (", ".join(ids["ids"]) or "none"),
            "  Location statements: " + (" | ".join(locations) or "none"),
            "",
            "Issue discussion:",
        ]
        turns = ["[" + _mmss(s) + "] " + sp + ": " + _clip(t, per_turn) for sp, s, t in issue]
        out += turns or ["(none)"]
        out += ["", "Closing:"]
        closing = ["[" + _mmss(s) + "] " + sp + ": " + _clip(t, per_turn) for sp, s, t in spoken[-3:]]
        out += closing or ["(none)"]
        out += [
            "",
            "DETERMINISTIC PRE-MATCH (code, export " + (label or "n/a") + ", " + str(len(rows))
            + " rows, newest activity " + _fmt(newest) + " ET): " + status + " / " + conf,
        ]
        if err:
            out.append("Code export note: " + err)
        if status != "NO_EXPORT":
            for i, c in enumerate(top, 1):
                out.append("Candidate %d: %s (score %d) - %s" % (
                    i, c["number"], c["score"], "; ".join(c["reasons"]) or "agent involvement only"))
                out.append(_compact(c["row"], _int(self.row_chars, 700)))
        out += ["", INSTRUCTIONS[status]]

        self._fp = {
            "skipped": False,
            "text": "\n".join(out),
            "status": status,
            "confidence": conf,
            "best_ticket": best_ticket,
            "candidates": [
                {"number": c["number"], "score": c["score"], "reasons": c["reasons"],
                 "opened_et": c["opened_et"], "facts": c["facts"]}
                for c in top
            ],
            "candidate_rows": {c["number"]: c["row"] for c in top},
            "channel": channel,
            "window_et": [_fmt(lo), _fmt(hi)],
            "tickets_mentioned": ids["tickets"],
            "caller_names": ids["names"],
            "caller_names_heard": ids["names_heard"],
            "caller_numbers": ids["numbers"],
            "caller_phones": ids["phones"],
            "issue_excerpt": issue_excerpt,
            "location_statements": locations,
            "caller_requested_pauses": pauses,
            "export": label,
            "export_rows": len(rows),
            "export_uploaded_et": _fmt(uploaded_et),
            "export_newest_et": _fmt(newest),
            "export_error": err,
        }
        return self._fp

    # ---------------- outputs ----------------

    def build_fingerprint(self) -> Message:
        r = self._run()
        if r.get("skipped"):
            try:
                self.stop("fingerprint")
            except Exception:
                pass
            return Message(text="FINGERPRINT_SKIPPED: transcription not finished")
        self.status = (
            r["status"] + " " + r["best_ticket"] + " | rows " + str(r["export_rows"])
            + " | " + (r["export"] or "no export")
            + ((" | " + r["export_error"]) if r["export_error"] else "")
        )
        return Message(text=r["text"])

    def build_prematch(self) -> Data:
        r = self._run()
        return Data(data={k: v for k, v in r.items() if k != "text"})
