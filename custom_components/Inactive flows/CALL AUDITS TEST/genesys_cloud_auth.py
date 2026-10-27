import time
import requests

from lfx.custom.custom_component.component import Component
from lfx.io import StrInput, SecretStrInput, Output
from lfx.schema.data import Data


_TOKEN_CACHE = {}


class CustomComponent(Component):
    display_name = "Genesys Cloud Auth"
    description = "Returns a Genesys Cloud access token (client credentials, or a manual token for testing)."

    inputs = [
        StrInput(
            name="region",
            display_name="Genesys Region",
            value="mypurecloud.com",
        ),
        SecretStrInput(
            name="manual_token",
            display_name="Manual Token (testing only)",
            value="",
            info="Paste a Bearer BEARER_TOKEN_HERE from your browser session for testing. Leave blank in production.",
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
    ]

    outputs = [
        Output(display_name="Token", name="token", method="get_token"),
    ]

    def get_token(self) -> Data:
        region = (self.region or "mypurecloud.com").strip()

        manual = (self.manual_token or "").strip()
        if manual:
            if manual.lower().startswith("bearer "):
                manual = manual[7:].strip()
            return Data(data={
                "access_token": manual,
                "region": region,
                "source": "manual",
            })

        if not self.client_id or not self.client_secret:
            return Data(data={"error": "Provide either a Manual Token or Client ID + Client Secret."})

        cache_key = self.client_id
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached["expires_at"] > time.time() + 30:
            return Data(data={
                "access_token": cached["token"],
                "region": region,
                "source": "client_credentials_cached",
            })

        try:
            resp = requests.post(
                "https://login." + region + "/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            return Data(data={"error": "Token request failed: " + str(e)})

        _TOKEN_CACHE[cache_key] = {
            "token": payload["access_token"],
            "expires_at": time.time() + payload.get("expires_in", 3600),
        }

        return Data(data={
            "access_token": payload["access_token"],
            "region": region,
            "source": "client_credentials",
        })
