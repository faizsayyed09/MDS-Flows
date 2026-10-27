import os
import re
import json
import time
import tempfile

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MultilineInput, IntInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

_DIR = os.path.join(tempfile.gettempdir(), "ceq_dispatcher")
_CACHE_FILE = os.path.join(_DIR, "roster_cache.json")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_MISS_RECHECK_SEC = 3600
_TIME_BUDGET_SEC = 90

DEFAULT_ROSTER = "\n".join([
    "Arun Nair: Selvi Nadar, Gunjan Dhomne, Karen John, Danesh Sampat, Azimuddin Patel, "
    "Abhishek Kamble, Sarita Sukheja, Ranjana Gupta, Diksha Salve, Shahnawaz Shirgaonkar, "
    "Kaniz Khan, Muriel Adam",
    "Akshay Malewar: Apoorva Motghare, Elisa Salve, Shweta Yadav, Faiq Kamboo, Amit Sonar, "
    "Anjali Rajawat, Bhagyalaxmi Nair, Zoya Ahmed, Neha Santosh Zende, Hardik Gajjar, Faiz Sayyed",
    "Akhil Pillai: Aishwarya Kudale, Shital Jadhav, Utkarsh Mishra, Prasad Surwade, Jyoti Waybhase, "
    "Subhash Vishwakarma, Akshay Sapkale, Vikas Lokhande, Adersh Unnikrishnan, Aleena Ahmed, "
    "Keyur Tanna, Sakshi Shede",
    "Tim McIntyre: Benita Brown, Jean SalinasVega, Stephen Emerson, Anthony Norman, Ritchie Smith, "
    "Mary Alford, Anthony Williams, Chavon Jackson, Ryan Dalton, Richard Jackson, Gareth Cook, "
    "James Vandel, Kali Bryant, Jefferson Campos, Aaron Morado, Gary Stewart, Stephanie Fernandez, "
    "Kevin Hollon, Justin Shelton",
])


def _auth(value):
    if isinstance(value, list) and value:
        value = value[0]
    if hasattr(value, "data"):
        value = value.data or {}
    return value if isinstance(value, dict) else {}


def _norm(text):
    text = re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _compact(text):
    return _norm(text).replace(" ", "")


def _load_cache():
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    os.makedirs(_DIR, exist_ok=True)
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, _CACHE_FILE)


def _make_entry(manager, raw):
    raw = raw.strip()
    override = ""
    name = raw
    if "=" in raw:
        name, value = raw.rsplit("=", 1)
        m = _UUID_RE.search(value)
        if m:
            override = m.group(0).lower()
    return {"manager": manager.strip(), "name": name.strip(), "override": override}


def _parse_roster(text):
    lines = []
    for line in (text or "").splitlines():
        if line.strip() and not line.strip().startswith("#"):
            lines.append(line.rstrip())
    raw_entries = []
    if lines and "\t" in lines[0] and ":" not in lines[0]:
        headers = [h.strip() for h in lines[0].split("\t")]
        for line in lines[1:]:
            cells = line.split("\t")
            for i, cell in enumerate(cells):
                if i < len(headers) and headers[i] and cell.strip():
                    raw_entries.append(_make_entry(headers[i], cell))
    else:
        for line in lines:
            if ":" not in line:
                continue
            manager, rest = line.split(":", 1)
            for part in re.split(r"[,;\t]", rest):
                if part.strip():
                    raw_entries.append(_make_entry(manager, part))
    entries = {}
    duplicates = []
    for e in raw_entries:
        if not e["name"]:
            continue
        key = _norm(e["name"])
        if key in entries and entries[key]["manager"] != e["manager"]:
            duplicates.append(e["name"] + " (" + entries[key]["manager"] + " and " + e["manager"] + ")")
        entries[key] = e
    return list(entries.values()), duplicates


def _search(token, region, match_type, value, page_size):
    url = "https://api." + region + "/api/v2/users/search"
    body = {
        "pageSize": page_size,
        "query": [{"type": match_type, "fields": ["name"], "value": value}],
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError("HTTP " + str(resp.status_code) + ": " + (resp.text or "")[:150])
    data = resp.json() if (resp.text or "").strip() else {}
    return data.get("results") or []


def _lookup(token, region, name):
    target = _norm(name)
    tokens = target.split()
    if not tokens:
        return [], "empty"
    results = _search(token, region, "EXACT", name, 25)
    exact = [u for u in results if _norm(u.get("name")) == target]
    if exact:
        return exact, "exact"
    results = _search(token, region, "CONTAINS", tokens[0], 100)
    spaced = [u for u in results if _compact(u.get("name")) == _compact(name)]
    if spaced:
        return spaced, "spacing differs"
    if len(tokens) >= 2:
        loose = []
        for u in results:
            words = _norm(u.get("name")).split()
            if tokens[0] in words and tokens[-1] in words:
                loose.append(u)
        if loose:
            return loose, "first + last name"
    return [], "none"


class CustomComponent(Component):
    display_name = "Audit Roster"
    description = "Editable list of managers and the agents to audit. Resolves names to Genesys IDs and caches them."

    inputs = [
        DataInput(
            name="auth",
            display_name="Genesys Auth",
            required=False,
            info="Output of the Genesys Cloud Auth component.",
        ),
        MultilineInput(
            name="roster",
            display_name="Roster",
            value=DEFAULT_ROSTER,
            info=(
                "One line per manager: 'Manager: Agent, Agent, ...'. Or paste the Excel sheet "
                "with manager names in the header row. Start a line with # to pause it. "
                "Fix ambiguous names as 'Agent Name=user-id'."
            ),
        ),
        IntInput(
            name="cache_hours",
            display_name="Cache Matches (hours)",
            value=24,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Roster Data", name="roster_data", method="build_roster_data", group_outputs=True),
        Output(display_name="Roster Report", name="roster_report", method="build_roster_report", group_outputs=True),
    ]

    def _resolve_roster(self):
        if getattr(self, "_roster_result", None) is not None:
            return self._roster_result

        entries, duplicates = _parse_roster(self.roster)
        auth = _auth(self.auth)
        token = auth.get("access_token") if not auth.get("error") else None
        region = auth.get("region", "mypurecloud.com")

        cache = _load_cache()
        now = time.time()
        ttl = max(1, int(self.cache_hours or 24)) * 3600
        started = time.time()
        fatal = ""

        agents = {}
        notes = {"flagged": [], "missing": [], "ambiguous": [], "pending": [], "errors": []}

        for e in entries:
            name = e["name"]
            manager = e["manager"]
            if e["override"]:
                agents[e["override"]] = {"name": name, "roster_name": name, "manager": manager}
                continue

            key = _norm(name)
            cached = cache.get(key)
            fresh = False
            if cached:
                age = now - float(cached.get("ts") or 0)
                if cached.get("status") == "ok" and age < ttl:
                    fresh = True
                elif cached.get("status") in ("missing", "ambiguous") and age < _MISS_RECHECK_SEC:
                    fresh = True

            if not fresh:
                if fatal or not token or time.time() - started > _TIME_BUDGET_SEC:
                    if cached and cached.get("status") == "ok":
                        fresh = True
                    else:
                        notes["pending"].append(name)
                        continue
                else:
                    try:
                        users, how = _lookup(token, region, name)
                    except Exception as ex:
                        message = str(ex)
                        if "401" in message:
                            fatal = "Genesys token expired (HTTP 401). Using cached matches only."
                        elif "403" in message:
                            fatal = "Token cannot search users (HTTP 403). Using cached matches only."
                        else:
                            notes["errors"].append(name + " (" + message[:80] + ")")
                        if cached and cached.get("status") == "ok":
                            fresh = True
                        else:
                            notes["pending"].append(name)
                            continue
                    if not fresh:
                        active = [u for u in users if str(u.get("state", "active")).lower() == "active"] or users
                        if len(active) == 1:
                            u = active[0]
                            cached = {
                                "status": "ok",
                                "id": str(u.get("id")).lower(),
                                "name": u.get("name"),
                                "email": u.get("email"),
                                "how": how,
                                "ts": now,
                            }
                        elif len(active) > 1:
                            cached = {
                                "status": "ambiguous",
                                "candidates": [
                                    {"name": u.get("name"), "email": u.get("email"), "id": u.get("id")}
                                    for u in active[:5]
                                ],
                                "ts": now,
                            }
                        else:
                            cached = {"status": "missing", "ts": now}
                        cache[key] = cached

            if cached.get("status") == "ok":
                agents[cached["id"]] = {
                    "name": cached.get("name") or name,
                    "roster_name": name,
                    "manager": manager,
                }
                if cached.get("how") not in ("exact", None):
                    notes["flagged"].append(
                        name + " -> " + str(cached.get("name")) + " | " + str(cached.get("email") or "-")
                        + " [" + str(cached.get("how")) + "]"
                    )
            elif cached.get("status") == "ambiguous":
                notes["ambiguous"].append((name, cached.get("candidates") or []))
            else:
                notes["missing"].append(name + " (" + manager + ")")

        _save_cache(cache)

        managers = {}
        for info in agents.values():
            managers[info["manager"]] = managers.get(info["manager"], 0) + 1

        self._roster_result = {
            "agents": agents,
            "managers": managers,
            "entries": len(entries),
            "duplicates": duplicates,
            "notes": notes,
            "fatal": fatal,
            "generated": now,
        }
        return self._roster_result

    def build_roster_data(self) -> Data:
        r = self._resolve_roster()
        return Data(data={
            "agents": r["agents"],
            "managers": r["managers"],
            "generated": r["generated"],
        })

    def build_roster_report(self) -> Message:
        r = self._resolve_roster()
        notes = r["notes"]
        lines = [
            "AUDIT ROSTER: " + str(r["entries"]) + " names, " + str(len(r["agents"])) + " resolved",
        ]
        if r["fatal"]:
            lines.append("WARNING: " + r["fatal"])
        lines.append("")
        lines.append("By manager:")
        for manager, count in sorted(r["managers"].items()):
            lines.append("  " + manager + ": " + str(count))
        if notes["missing"]:
            lines.append("")
            lines.append("NOT FOUND (check spelling against Genesys):")
            for item in notes["missing"]:
                lines.append("  " + item)
        if notes["ambiguous"]:
            lines.append("")
            lines.append("AMBIGUOUS (add 'Name=user-id' in the roster):")
            for name, candidates in notes["ambiguous"]:
                lines.append("  " + name + ":")
                for c in candidates:
                    lines.append(
                        "      " + str(c.get("name")) + " | " + str(c.get("email") or "-")
                        + " | " + str(c.get("id"))
                    )
        if notes["flagged"]:
            lines.append("")
            lines.append("MATCHED LOOSELY (confirm these are the right people):")
            for item in notes["flagged"]:
                lines.append("  " + item)
        if r["duplicates"]:
            lines.append("")
            lines.append("LISTED UNDER TWO MANAGERS (last one used):")
            for item in r["duplicates"]:
                lines.append("  " + item)
        if notes["pending"]:
            lines.append("")
            lines.append("NOT CHECKED YET (run again to finish): " + ", ".join(notes["pending"]))
        if notes["errors"]:
            lines.append("")
            lines.append("ERRORS:")
            for item in notes["errors"]:
                lines.append("  " + item)
        return Message(text="\n".join(lines))
