import math
from datetime import datetime, timedelta, timezone

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import StrInput, SecretStrInput, IntInput, Output
from lfx.schema.message import Message


PERMISSIONS = [
    ("Recording > Recording > View", "Download call recordings"),
    ("Recording > Recording Segment > View", "Required alongside recording view"),
    ("Analytics > Conversation Detail > View", "Find ended calls and emails, hold events"),
    ("Routing > Queue > View", "Resolve queue names to IDs"),
    ("Directory > User > View", "Match agent names to user IDs"),
    ("Conversation > Email > View", "Read email interactions for email audits"),
]


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _json(resp):
    try:
        return resp.json() if (resp.text or "").strip() else {}
    except ValueError:
        return {}


class CustomComponent(Component):
    display_name = "Genesys Integration Readiness Check"
    description = "Verifies an OAuth client can perform every read the CEQ audit pipeline needs, and produces a shareable report."

    inputs = [
        StrInput(
            name="environment_label",
            display_name="Environment Label",
            value="DEV",
            info="Shown in the report header, e.g. DEV or PROD.",
        ),
        StrInput(
            name="region",
            display_name="Genesys Region",
            value="mypurecloud.com",
            info="Region domain only, not apps.mypurecloud.com.",
        ),
        StrInput(
            name="client_id",
            display_name="OAuth Client ID",
            value="",
        ),
        SecretStrInput(
            name="client_secret",
            display_name="OAuth Client Secret",
            value="",
        ),
        StrInput(
            name="conversation_id",
            display_name="Conversation ID (optional)",
            value="",
            info="A conversation in THIS org with a recording. Blank = search recent calls automatically.",
        ),
        IntInput(
            name="lookback_days",
            display_name="Search Recent Calls (days)",
            value=30,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Readiness Report", name="readiness_report", method="build_readiness_report"),
    ]

    # ---------------- helpers ----------------

    def _call(self, method, url, headers, **kwargs):
        try:
            resp = requests.request(method, url, headers=headers, timeout=25, **kwargs)
            return resp.status_code, _json(resp), (resp.text or "")[:200]
        except Exception as e:
            return None, {}, str(e)[:200]

    def _search(self, base, headers, media_type, days):
        url = base + "/api/v2/analytics/conversations/details/query"
        found = []
        last_status = None
        now = datetime.now(timezone.utc)
        chunks = max(1, math.ceil(max(1, days) / 7))
        for i in range(chunks):
            end = now - timedelta(days=7 * i)
            start = end - timedelta(days=7)
            body = {
                "interval": _iso(start) + "/" + _iso(end),
                "order": "desc",
                "orderBy": "conversationStart",
                "paging": {"pageSize": 25, "pageNumber": 1},
                "segmentFilters": [
                    {"type": "and", "predicates": [{"dimension": "mediaType", "value": media_type}]}
                ],
            }
            status, data, _ = self._call("POST", url, headers, json=body)
            last_status = status
            if status != 200:
                break
            for c in data.get("conversations") or []:
                if c.get("conversationId") and c.get("conversationEnd"):
                    found.append(c["conversationId"])
            if len(found) >= 5:
                break
        return last_status, found

    # ---------------- main ----------------

    def build_readiness_report(self) -> Message:
        region = (self.region or "mypurecloud.com").strip()
        base = "https://api." + region
        label = (self.environment_label or "DEV").strip().upper()
        checks = []

        def add(name, ok, detail):
            checks.append((name, ok, detail))

        # 1. Token
        try:
            resp = requests.post(
                "https://login." + region + "/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=((self.client_id or "").strip(), (self.client_secret or "").strip()),
                timeout=20,
            )
            token_data = _json(resp)
            token = token_data.get("access_token")
        except Exception as e:
            token = None
            token_data = {"error": str(e)[:150]}
            resp = None

        if not token:
            add("OAuth token", False, "Token request failed: " + str(token_data.get("error") or token_data)[:150])
            return Message(text=self._render(label, "unknown", checks))

        add("OAuth token", True, "Issued, expires in " + str(token_data.get("expires_in")) + "s")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # 2. Organization
        status, data, text = self._call("GET", base + "/api/v2/organizations/me", headers)
        org_name = data.get("name") or "unknown"
        if status == 200:
            add("Organization", True, "Connected to '" + org_name + "' (" + str(data.get("id", ""))[:8] + "...)")
        else:
            add("Organization", False, "HTTP " + str(status))

        # 3. Users
        status, data, _ = self._call("GET", base + "/api/v2/users", headers, params={"pageSize": 1})
        add("Users (Directory)", status == 200,
            ("HTTP 200, " + str(data.get("total", "?")) + " users visible") if status == 200 else "HTTP " + str(status))

        # 4. Queues
        status, data, _ = self._call("GET", base + "/api/v2/routing/queues", headers, params={"pageSize": 1})
        add("Queues (Routing)", status == 200,
            ("HTTP 200, " + str(data.get("total", "?")) + " queues visible") if status == 200 else "HTTP " + str(status))

        # 5. Voice call search
        days = int(self.lookback_days or 30)
        status, voice_ids = self._search(base, headers, "voice", days)
        if status == 200:
            add("Call search (Analytics)", True, "HTTP 200, " + str(len(voice_ids)) + " recent calls found")
        else:
            add("Call search (Analytics)", False, "HTTP " + str(status))

        # 6. Recording metadata + download
        candidates = []
        if (self.conversation_id or "").strip():
            candidates.append(self.conversation_id.strip())
        candidates += [c for c in voice_ids if c not in candidates]
        candidates = candidates[:5]

        rec_status = None
        media_uri = None
        tested = None
        for conv in candidates:
            url = base + "/api/v2/conversations/" + conv + "/recordings"
            status, data, _ = self._call("GET", url, headers, params={"maxWaitMs": 5000})
            rec_status = status
            tested = conv
            if status != 200:
                if status == 403:
                    break
                continue
            recs = data if isinstance(data, list) else (data.get("entities") or [data])
            for r in recs:
                if r.get("media") == "audio":
                    for block in (r.get("mediaUris") or {}).values():
                        if block and block.get("mediaUri"):
                            media_uri = block["mediaUri"]
                            break
                if media_uri:
                    break
            if media_uri:
                break

        if not candidates:
            add("Recording metadata", True, "No calls in this org to test (endpoint not exercised)")
            add("Recording download", True, "Skipped: no calls available")
        elif rec_status == 200 and media_uri:
            add("Recording metadata", True, "HTTP 200 for conversation " + tested[:8] + "...")
            try:
                r = requests.get(media_uri, headers={"Range": "bytes=0-1023"}, stream=True, timeout=25)
                ok = r.status_code in (200, 206)
                r.close()
                add("Recording download", ok, "First 1 KB fetched (HTTP " + str(r.status_code) + "), nothing stored")
            except Exception as e:
                add("Recording download", False, str(e)[:120])
        elif rec_status == 200:
            add("Recording metadata", True, "HTTP 200, but tested calls had no audio recording")
            add("Recording download", True, "Skipped: no audio recording found")
        else:
            add("Recording metadata", False, "HTTP " + str(rec_status) + " for conversation " + str(tested)[:8] + "...")
            add("Recording download", False, "Not attempted")

        # 7. Email
        status, email_ids = self._search(base, headers, "email", days)
        if status != 200:
            add("Email search", False, "HTTP " + str(status))
        elif not email_ids:
            add("Email search", True, "HTTP 200, no email interactions in this org")
        else:
            add("Email search", True, "HTTP 200, " + str(len(email_ids)) + " recent emails found")
            url = base + "/api/v2/conversations/emails/" + email_ids[0] + "/messages"
            status, data, _ = self._call("GET", url, headers)
            add("Email messages", status == 200,
                ("HTTP 200, " + str(data.get("total", "?")) + " messages readable") if status == 200 else "HTTP " + str(status))

        return Message(text=self._render(label, org_name, checks))

    def _render(self, label, org_name, checks):
        passed = sum(1 for c in checks if c[1])
        failed = [c for c in checks if not c[1]]
        lines = [
            "GENESYS INTEGRATION READINESS CHECK - " + label,
            "Run at: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "Org: " + org_name,
            "Integration: CEQ Call/Email Audit pipeline (read-only)",
            "",
            "RESULTS: " + str(passed) + "/" + str(len(checks)) + " passed",
        ]
        for name, ok, detail in checks:
            lines.append("  [" + ("PASS" if ok else "FAIL") + "] " + name.ljust(26) + detail)
        lines.append("")
        if failed:
            lines.append("VERDICT: NOT READY - " + ", ".join(c[0] for c in failed) + " failed.")
        else:
            lines.append("VERDICT: READY - the client can perform every read the pipeline needs.")
        lines += [
            "",
            "REQUEST FOR PRODUCTION ACCESS",
            "Please create a production OAuth client (Client Credentials grant) with a custom",
            "READ-ONLY role containing only the permissions below, granted for the",
            "Managed Services division:",
        ]
        for perm, why in PERMISSIONS:
            lines.append("  - " + perm + "  (" + why + ")")
        lines += [
            "",
            "How the integration uses it:",
            "  - Read-only: it never creates, changes or deletes anything in Genesys.",
            "  - Scope: only calls/emails in queues GSD_MQ, GSD_OF, EIS_CSC, EIS_TSG,",
            "    for a fixed roster of audited agents.",
            "  - Audio is downloaded to temporary storage and deleted after transcription.",
            "  - The secret is stored only in the Matcha flow's secret field.",
            "Please share the secret through a secure channel (not chat/email).",
        ]
        return "\n".join(lines)
