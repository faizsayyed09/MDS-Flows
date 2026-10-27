import re

import time

from datetime import datetime, date, timedelta, timezone

from zoneinfo import ZoneInfo

import requests

from lfx.custom.custom_component.component import Component

from lfx.io import DataInput, DropdownInput, IntInput, MultilineInput, StrInput, Output

from lfx.schema.data import Data

FMT = "%Y-%m-%dT%H:%M:%S.000Z"

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

ID_CHUNK = 50

AGENT_PURPOSES = ("agent", "user")

CUSTOMER_PURPOSES = ("customer", "external")

AGENT_SEG_TYPES = ("interact", "hold")

AGENT_REASONS = ("peer", "client", "endpoint", "inactivity", "sessionexpired")

CUSTOMER_REASONS = ("endpoint", "inactivity", "sessionexpired")

def _unwrap(value):

    if isinstance(value, list) and value:

        value = value[0]

    if hasattr(value, "data"):

        value = value.data or {}

    return value if isinstance(value, dict) else {}

def _norm(text):

    text = re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())

    return re.sub(r"\s+", " ", text).strip()

def _dt(value):

    try:

        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    except (TypeError, ValueError):

        return None

def _parse_when(text, tz, is_end):

    text = (text or "").strip()

    if not text:

        return None

    if len(text) == 10:

        d = date.fromisoformat(text)

        dt = datetime(d.year, d.month, d.day, tzinfo=tz)

        if is_end:

            return dt + timedelta(days=1)

        return dt

    dt = datetime.fromisoformat(text.replace("T", " "))

    if dt.tzinfo:

        return dt

    return dt.replace(tzinfo=tz)

def _chunks(start, end):

    out = []

    cur = start

    while cur < end:

        nxt = min(end, cur + timedelta(days=1))

        a = cur.astimezone(timezone.utc).strftime(FMT)

        b = nxt.astimezone(timezone.utc).strftime(FMT)

        out.append(a + "/" + b)

        cur = nxt

    return out

def _select(selection, roster_agents):

    tokens = []

    for t in re.split(r"[,;\n]+", selection or ""):

        if t.strip():

            tokens.append(t.strip())

    if not tokens:

        return dict(roster_agents), []

    for t in tokens:

        if t.lower() in ("all", "*"):

            return dict(roster_agents), []

    by_manager = {}

    for uid, a in roster_agents.items():

        by_manager.setdefault(_norm(a.get("manager")), []).append(uid)

    picked = {}

    notes = []

    for tok in tokens:

        m = UUID_RE.search(tok)

        if m:

            uid = m.group(0).lower()

            name = ""

            if "=" in tok:

                name = tok.split("=")[0].strip()

            fallback = {"name": name, "roster_name": "", "manager": ""}

            picked[uid] = roster_agents.get(uid) or fallback

            continue

        key = _norm(tok)

        if key in by_manager:

            for uid in by_manager[key]:

                picked[uid] = roster_agents[uid]

            continue

        hits = []

        for u, a in roster_agents.items():

            name = _norm(a.get("name"))

            roster_name = _norm(a.get("roster_name"))

            if key == name or key == roster_name:

                hits.append(u)

            elif key.replace(" ", "") == name.replace(" ", ""):

                hits.append(u)

        if not hits:

            words = key.split()

            for u, a in roster_agents.items():

                blob = (_norm(a.get("name")) + " " + _norm(a.get("roster_name"))).split()

                ok = True

                for w in words:

                    if w not in blob:

                        ok = False

                if ok:

                    hits.append(u)

        if len(hits) == 1:

            picked[hits[0]] = roster_agents[hits[0]]

        elif len(hits) > 1:

            names = []

            for u in hits:

                names.append(str(roster_agents[u].get("name")))

            notes.append("AMBIGUOUS '" + tok + "': " + ", ".join(names))

        else:

            notes.append("NOT IN ROSTER '" + tok + "': check the Roster Report or paste the Genesys user ID")

    return picked, notes

def _last_reason(items):

    if not items:

        return ""

    items = sorted(items, key=lambda t: t[0] or "")

    return items[-1][1]

def _derive_disconnect(conv):

    if not conv.get("conversationEnd"):

        return "In Progress"

    agent_segs = []

    cust_segs = []

    ivr_segs = []

    acd_segs = []

    transfer_segs = 0

    cust_ends = []

    for p in conv.get("participants") or []:

        pur = str(p.get("purpose") or "").strip().lower()

        last_end = ""

        for s in p.get("sessions") or []:

            for x in s.get("segments") or []:

                st = str(x.get("segmentType") or "").strip().lower()

                dr = str(x.get("disconnectType") or "").strip().lower()

                end = x.get("segmentEnd") or ""

                if pur in AGENT_PURPOSES:

                    if st in AGENT_SEG_TYPES and dr in AGENT_REASONS:

                        agent_segs.append((end, dr))

                    if "transfer" in st:

                        transfer_segs += 1

                elif pur in CUSTOMER_PURPOSES:

                    if st == "interact":

                        if dr in CUSTOMER_REASONS:

                            cust_segs.append((end, dr))

                        if end > last_end:

                            last_end = end

                elif pur == "ivr":

                    if dr == "peer":

                        ivr_segs.append((end, dr))

                elif pur == "acd":

                    if dr == "peer":

                        acd_segs.append((end, dr))

        if pur in CUSTOMER_PURPOSES and last_end:

            cust_ends.append(last_end)

    if agent_segs:

        reason = _last_reason(agent_segs)

        if reason == "peer" and cust_segs:

            return "External"

        if reason == "inactivity":

            return "Conversation Inactivity"

        if reason == "sessionexpired":

            return "Session Expired"

        return "Agent"

    if cust_segs:

        reason = _last_reason(cust_segs)

        if reason == "inactivity":

            return "Conversation Inactivity"

        if reason == "sessionexpired":

            return "Session Expired"

    if ivr_segs and cust_segs:

        return "External"

    if acd_segs and cust_segs:

        return "External"

    if transfer_segs and len(cust_ends) >= 2:

        ends = sorted(cust_ends)

        if ends[0] < ends[-1]:

            return "External"

        return "Agent"

    return "System"

class CustomComponent(Component):

    display_name = "Genesys Interaction Lister"

    description = "Completed voice and email interactions for selected agents, with the derived conversation-level disconnect type."

    inputs = [

        DataInput(name="auth", display_name="Auth Token", required=True),

        DataInput(name="roster", display_name="Audit Roster", required=False,

            info="Roster Data output of the Audit Roster component."),

        MultilineInput(name="agent_selection", display_name="Agents to Analyse", value="",

            info="Agent names, manager names (whole team) or Genesys user IDs. Blank or ALL = every resolved roster agent. Example: Akshay Malewar"),

        StrInput(name="start_time", display_name="Start (date or date time)", value="",

            info="2026-10-20 or 2026-10-20 09:00. Blank = Lookback Days."),

        StrInput(name="end_time", display_name="End (date inclusive, or date time exclusive)", value="",

            info="2026-10-21 (whole day included) or 2026-10-21 18:00."),

        DropdownInput(name="time_zone", display_name="Time Zone of Start/End",

            options=["America/New_York", "Asia/Kolkata", "UTC"], value="America/New_York"),

        IntInput(name="lookback_days", display_name="Lookback Days (if Start blank)", value=1, advanced=True),

        MultilineInput(name="conversation_ids", display_name="Conversation IDs (optional)", value="",

            advanced=True,

            info="If filled, only these calls are analysed (still limited to the selected agents)."),

        IntInput(name="max_days", display_name="Max Window (days)", value=31, advanced=True),

        IntInput(name="max_pages", display_name="Max Pages per Day (100 each)", value=50, advanced=True),

    ]

    outputs = [Output(display_name="Interactions", name="interactions", method="build")]

    def _req(self, method, url, headers, **kw):

        resp = None

        for _ in range(5):

            resp = requests.request(method, url, headers=headers, timeout=90, **kw)

            if resp.status_code != 429:

                return resp

            time.sleep(int(resp.headers.get("Retry-After", "5")))

        return resp

    def _media_filter(self):

        return {

            "type": "or",

            "predicates": [

                {"dimension": "mediaType", "value": "voice"},

                {"dimension": "mediaType", "value": "email"},

            ],

        }

    def _query(self, api, headers, interval, uids):

        url = api + "/api/v2/analytics/conversations/details/query"

        groups = []

        if uids:

            for i in range(0, len(uids), ID_CHUNK):

                preds = []

                for u in uids[i:i + ID_CHUNK]:

                    preds.append({"dimension": "userId", "value": u})

                groups.append(preds)

        else:

            groups.append(self._media_filter()["predicates"])

        out = []

        for preds in groups:

            filters = [{"type": "or", "predicates": preds}]

            if uids:

                filters.append(self._media_filter())

            body = {

                "interval": interval,

                "order": "asc",

                "orderBy": "conversationStart",

                "paging": {"pageSize": 100, "pageNumber": 1},

                "segmentFilters": filters,

            }

            for page in range(1, max(1, int(self.max_pages or 50)) + 1):

                body["paging"]["pageNumber"] = page

                r = self._req("POST", url, headers, json=body)

                if r.status_code >= 400:

                    raise RuntimeError("ANALYTICS_HTTP_" + str(r.status_code) + " " + r.text[:200])

                batch = r.json().get("conversations") or []

                out.extend(batch)

                if len(batch) < 100:

                    break

        return out

    def build(self) -> Data:

        auth = _unwrap(self.auth)

        if auth.get("error") or not auth.get("access_token"):

            return Data(data={"error": "AUTH", "detail": str(auth.get("error") or "no access_token")})

        api = "https://api." + auth.get("region", "mypurecloud.com")

        headers = {"Authorization": "Bearer " + auth["access_token"], "Content-Type": "application/json"}

        tz = ZoneInfo(self.time_zone or "America/New_York")

        roster = _unwrap(self.roster)

        roster_agents = {}

        for k, v in (roster.get("agents") or {}).items():

            roster_agents[str(k).lower()] = v

        if roster_agents or (self.agent_selection or "").strip():

            selected, notes = _select(self.agent_selection, roster_agents)

            if not selected:

                detail = " | ".join(notes) or "Roster resolved no agents. Check the Roster Report."

                return Data(data={"error": "NO_AGENTS_SELECTED", "detail": detail})

        else:

            selected = {}

            notes = []

        uids = sorted(selected)

        ids = []

        for x in re.split(r"[\s,;]+", self.conversation_ids or ""):

            if x:

                ids.append(x)

        convs = {}

        try:

            if ids:

                for cid in ids:

                    r = self._req("GET", api + "/api/v2/analytics/conversations/" + cid + "/details", headers)

                    if r.ok:

                        convs[cid] = r.json()

                days = set()

                for c in convs.values():

                    if c.get("conversationStart"):

                        days.add(_dt(c["conversationStart"]).astimezone(tz).date())

                if not days:

                    return Data(data={"error": "NO_CONVERSATIONS_FOUND", "detail": "Check IDs and permissions."})

                days = sorted(days)

                start = datetime(days[0].year, days[0].month, days[0].day, tzinfo=tz)

                end = datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=tz) + timedelta(days=1)

                email_uids = uids

                if not email_uids:

                    found = set()

                    for c in convs.values():

                        for p in c.get("participants") or []:

                            if p.get("purpose") == "agent" and p.get("userId"):

                                found.add(str(p["userId"]).lower())

                    email_uids = sorted(found)

                for iv in _chunks(start, end):

                    for c in self._query(api, headers, iv, email_uids):

                        convs.setdefault(c["conversationId"], c)

            else:

                start = _parse_when(self.start_time, tz, False)

                end = _parse_when(self.end_time, tz, True)

                if start is None:

                    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

                    start = today - timedelta(days=max(1, int(self.lookback_days or 1)))

                    if end is None:

                        end = today

                if end is None:

                    end = start + timedelta(days=1)

                if end <= start:

                    raise ValueError("End must be after Start.")

                if end - start > timedelta(days=max(1, int(self.max_days or 31))):

                    raise ValueError("Window longer than Max Window (days). Split the run.")

                for iv in _chunks(start, end):

                    for c in self._query(api, headers, iv, uids):

                        convs.setdefault(c["conversationId"], c)

        except ValueError as e:

            detail = str(e)[:200] + " Use YYYY-MM-DD or YYYY-MM-DD HH:MM."

            return Data(data={"error": "BAD_WINDOW", "detail": detail})

        except RuntimeError as e:

            return Data(data={"error": "LISTER", "detail": str(e)[:300]})

        users = {}

        def user(uid):

            if uid not in users:

                r = self._req("GET", api + "/api/v2/users/" + uid, headers)

                j = {}

                if r is not None and r.ok:

                    j = r.json()

                email = j.get("email") or j.get("username") or ""

                users[uid] = {"name": j.get("name", ""), "email": email.strip().lower()}

            return users[uid]

        idset = set(ids)

        out = []

        counts = {}

        disc_counts = {}

        for c in convs.values():

            if not c.get("conversationEnd"):

                continue

            parts = c.get("participants") or []

            cust = ""

            for p in parts:

                if p.get("purpose") in CUSTOMER_PURPOSES and p.get("participantName"):

                    cust = p.get("participantName")

                    break

            derived = _derive_disconnect(c)

            for p in parts:

                uid = str(p.get("userId") or "").lower()

                if p.get("purpose") != "agent" or not uid:

                    continue

                if selected and uid not in selected:

                    continue

                for s in p.get("sessions") or []:

                    media = s.get("mediaType")

                    if media not in ("voice", "email"):

                        continue

                    if media == "voice" and idset and c["conversationId"] not in idset:

                        continue

                    segs = s.get("segments") or []

                    inter = []

                    holds = []

                    queue_id = ""

                    wrapup = ""

                    raw = ""

                    for x in segs:

                        st = str(x.get("segmentType") or "").strip().lower()

                        if st == "interact" and x.get("segmentEnd"):

                            inter.append([x["segmentStart"], x["segmentEnd"]])

                        if st == "hold" and x.get("segmentEnd"):

                            holds.append([x["segmentStart"], x["segmentEnd"]])

                        if not queue_id and x.get("queueId"):

                            queue_id = x.get("queueId")

                        if not wrapup and x.get("wrapUpCode"):

                            wrapup = x.get("wrapUpCode")

                        if x.get("disconnectType"):

                            raw = str(x.get("disconnectType")).strip().lower()

                    if not inter:

                        continue

                    first = min(x[0] for x in inter)

                    if media == "voice" and not ids:

                        moment = _dt(first)

                        if moment is None or moment < start or moment >= end:

                            continue

                    u = user(p["userId"])

                    info = selected.get(uid) or roster_agents.get(uid) or {}

                    rec = {

                        "conversation_id": c["conversationId"],

                        "media": media,

                        "user_id": p["userId"],

                        "agent_name": u["name"] or info.get("name", ""),

                        "agent_email": u["email"],

                        "manager": info.get("manager", ""),

                        "direction": c.get("originatingDirection", ""),

                        "queue_id": queue_id,

                        "wrapup": wrapup,

                        "disconnect_type": derived,

                        "disconnect_raw": raw,

                        "agent_released_own_session": raw in ("client", "endpoint"),

                        "ani": s.get("ani", ""),

                        "caller_name": cust or s.get("remoteNameDisplayable", ""),

                        "interact": inter,

                        "holds": holds,

                        "start": first,

                        "end": max(x[1] for x in inter),

                    }

                    out.append(rec)

                    label = rec["agent_name"] or uid

                    k = counts.setdefault(label, [0, 0])

                    if media == "voice":

                        k[0] += 1

                        disc_counts[derived] = disc_counts.get(derived, 0) + 1

                    else:

                        k[1] += 1

        window = start.strftime("%Y-%m-%d %H:%M") + " to " + end.strftime("%Y-%m-%d %H:%M") + " " + str(tz)

        lines = []

        for n in sorted(counts):

            v = counts[n]

            lines.append(n + ": " + str(v[0]) + " calls, " + str(v[1]) + " emails")

        per_agent = "; ".join(lines) or "no interactions found"

        no_email = 0

        for u in users.values():

            if not u["email"]:

                no_email += 1

        disc = []

        for k in sorted(disc_counts):

            disc.append(k + " " + str(disc_counts[k]))

        status = str(len(uids) or "all") + " agent(s) | " + window + " | " + per_agent

        if disc:

            status = status + " | disconnect: " + ", ".join(disc)

        if notes:

            status = status + " | " + " | ".join(notes)

        if no_email:

            status = status + " | users without email: " + str(no_email)

        self.status = status

        chosen = []

        for k, v in selected.items():

            chosen.append({"user_id": k, "name": v.get("name", ""), "manager": v.get("manager", "")})

        return Data(data={

            "interval": window,

            "interactions": out,

            "selection_notes": notes,

            "selected_agents": chosen,

        })
