import time
import requests

from lfx.custom.custom_component.component import Component
from lfx.io import StrInput, SecretStrInput, BoolInput, Output
from lfx.schema.data import Data

_TOKEN_CACHE = {}


def _clean_region(value):
    region = (value or "mypurecloud.com").strip().lower()

    for prefix in ("https://", "http://"):
        if region.startswith(prefix):
            region = region[len(prefix):]

    region = region.split("/")[0]

    for prefix in ("apps.", "login.", "api."):
        if region.startswith(prefix):
            region = region[len(prefix):]

    return region or "mypurecloud.com"


class CustomComponent(Component):
    display_name = "Genesys Cloud Auth"
    description = (
        "Returns a Genesys Cloud access token "
        "(client credentials, or a manual token for testing)."
    )

    inputs = [
        StrInput(
            name="region",
            display_name="Genesys Region",
            value="mypurecloud.com",
            info=(
                "Region domain only, e.g. mypurecloud.com. "
                "'apps.' or 'https://' are removed automatically."
            ),
        ),
        SecretStrInput(
            name="manual_token",
            display_name="Manual Token (testing only)",
            value="",
            info=(
                "Paste a Bearer BEARER_TOKEN_HERE from your browser session for testing. "
                "Leave blank in production."
            ),
            required=False,
        ),
        StrInput(
            name="client_id",
            display_name="OAuth Client ID",
            value="",
            required=False,
        ),
        SecretStrInput(
            name="client_secret",
            display_name="OAuth Client Secret",
            value="",
            required=False,
        ),
        BoolInput(
            name="force_refresh",
            display_name="Force New Token",
            value=False,
            info="Turn on once after permission changes, run, then turn off.",
            advanced=True,
        ),
        BoolInput(
            name="verify_org",
            display_name="Verify Organization",
            value=True,
            info=(
                "Checks which Genesys org the token belongs to "
                "(catches dev vs prod mix-ups)."
            ),
            advanced=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Token",
            name="token",
            method="get_token",
        ),
    ]

    def _org_name(self, region, token):
        try:
            resp = requests.get(
                "https://api." + region + "/api/v2/organizations/me",
                headers={"Authorization": "Bearer " + token},
                timeout=15,
            )
        except Exception as e:
            return "unknown (" + str(e)[:60] + ")"

        if resp.status_code == 200:
            return str((resp.json() or {}).get("name") or "unknown")

        return "unknown (HTTP " + str(resp.status_code) + ")"

    def _result(self, region, token, source, expires_in=None):
        data = {
            "access_token": token,
            "region": region,
            "source": source,
        }

        if expires_in is not None:
            data["expires_in"] = int(expires_in)

        if self.verify_org:
            data["org"] = self._org_name(region, token)

        self.status = (
            "Token OK | source: "
            + source
            + " | org: "
            + str(data.get("org", "not checked"))
        )

        return Data(data=data)

    def get_token(self) -> Data:
        region = _clean_region(self.region)

        manual = (self.manual_token or "").strip()
        if manual:
            if manual.lower().startswith("bearer "):
                manual = manual[7:].strip()

            return self._result(region, manual, "manual")

        client_id = (self.client_id or "").strip()
        client_secret = (self.client_secret or "").strip()

        if not client_id or not client_secret:
            self.status = "Missing credentials"
            return Data(
                data={
                    "error": (
                        "Provide either a Manual Token or "
                        "Client ID + Client Secret."
                    )
                }
            )

        if self.force_refresh:
            _TOKEN_CACHE.pop(client_id, None)

        cached = _TOKEN_CACHE.get(client_id)
        if cached and cached["expires_at"] > time.time() + 60:
            remaining = cached["expires_at"] - time.time()

            return self._result(
                region,
                cached["token"],
                "client_credentials_cached",
                remaining,
            )

        url = "https://login." + region + "/oauth/token"

        try:
            resp = requests.post(
                url,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=15,
            )
        except Exception as e:
            self.status = "Token request failed"
            return Data(
                data={
                    "error": (
                        "Token request failed ("
                        + url
                        + "): "
                        + str(e)[:200]
                    )
                }
            )

        if resp.status_code != 200:
            detail = (resp.text or "")[:300]
            self.status = (
                "Token request failed: HTTP "
                + str(resp.status_code)
            )

            return Data(
                data={
                    "error": (
                        "Token request failed: HTTP "
                        + str(resp.status_code)
                        + " from "
                        + url
                        + " - "
                        + detail
                    )
                }
            )

        payload = resp.json() or {}
        token = payload.get("access_token")

        if not token:
            return Data(
                data={
                    "error": (
                        "No access_token in response: "
                        + str(payload)[:200]
                    )
                }
            )

        expires_in = payload.get("expires_in", 3600)

        _TOKEN_CACHE[client_id] = {
            "token": token,
            "expires_at": time.time() + expires_in,
        }

        return self._result(
            region,
            token,
            "client_credentials",
            expires_in,
        )
