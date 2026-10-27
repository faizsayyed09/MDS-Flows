import os
import re
import json
import time
import tempfile
from datetime import datetime

import requests

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, DataInput, IntInput, BoolInput, DropdownInput, Output
from lfx.schema.data import Data


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _shared_audio_dir():
    base = os.environ.get("LANGFLOW_CONFIG_DIR") or ""
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        path = os.path.join(base, "_audio_tmp")
    else:
        path = os.path.join(tempfile.gettempdir(), "_audio_tmp")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "x"))


def _purge_old_audio(max_age_hours):
    folder = _shared_audio_dir()
    cutoff = time.time() - max(1, int(max_age_hours or 6)) * 3600
    removed = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            pass
    return removed


class CustomComponent(Component):
    display_name = "Genesys Recording Downloader"
    description = (
        "Downloads call audio from Genesys Cloud into shared persistent storage, "
        "plus Genesys hold events for accurate hold auditing."
    )

    inputs = [
        DataInput(
            name="auth",
            display_name="Auth Token",
            info="Output of the Genesys Cloud Auth component.",
            required=True,
        ),
        MessageTextInput(
            name="conversation_id",
            display_name="Conversation ID",
            required=False,
            info="Type an ID for testing, or connect a text/message output.",
        ),
        DataInput(
            name="trigger_data",
            display_name="Trigger Data (optional)",
            required=False,
            info="Connect a webhook payload here. The conversationId is extracted automatically.",
        ),
        BoolInput(
            name="fetch_hold_events",
            display_name="Fetch Genesys Hold Events",
            value=True,
            info="Needs analytics:conversationDetail:view. Falls back gracefully if unavailable.",
        ),
        IntInput(
            name="audio_retention_hours",
            display_name="Audio Retention (hours)",
            value=6,
            info="Downloaded audio older than this is deleted automatically on each run.",
        ),
        BoolInput(
            name="force_redownload",
            display_name="Force Re-download",
            value=False,
            info="Ignore audio already downloaded for this conversation and fetch it again.",
            advanced=True,
        ),
        DropdownInput(
            name="format_id",
            display_name="Format",
            options=["DEFAULT", "WEBM", "MP3", "WAV", "OGG_OPUS"],
            value="DEFAULT",
            advanced=True,
        ),
        IntInput(
            name="max_wait_ms",
            display_name="Server Wait (ms)",
            value=5000,
            advanced=True,
        ),
        IntInput(
            name="max_poll_attempts",
            display_name="Max Poll Attempts",
            value=6,
            advanced=True,
        ),
        IntInput(
            name="poll_interval_sec",
            display_name="Poll Interval (seconds)",
            value=5,
            advanced=True,
        ),
        BoolInput(
            name="include_screen_recordings",
            display_name="Also download screen recordings",
            value=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Audio Files", name="audio_files", method="download"),
    ]

    # ---------------- auth helpers ----------------

    def _auth_data(self):
        auth = self.auth
        if isinstance(auth, list) and auth:
            auth = auth[0]
        if hasattr(auth, "data"):
            return auth.data or {}
        if isinstance(auth, dict):
            return auth
        return {}

    def _headers(self):
        auth = self._auth_data()
        if auth.get("error"):
            raise RuntimeError("Auth component error: " + str(auth.get("error")))
        token = auth.get("access_token")
        if not token:
            raise RuntimeError("No access_token found in auth input.")
        return {"Authorization": "Bearer " + token, "Accept": "application/json"}

    def _base_url(self):
        region = self._auth_data().get("region", "mypurecloud.com")
        return "https://api." + region

    # ---------------- conversation id ----------------

    def _resolve_conversation_id(self):
        candidates = []

        td = self.trigger_data
        if isinstance(td, list) and td:
            td = td[0]
        if hasattr(td, "data"):
            td = td.data or {}
        if isinstance(td, dict) and td:
            for key in ("conversationId", "conversation_id", "ConversationId"):
                if td.get(key):
                    candidates.append(str(td.get(key)))
            try:
                candidates.append(json.dumps(td))
            except Exception:
                pass

        raw = self.conversation_id
        if hasattr(raw, "text"):
            raw = raw.text
        if raw:
            candidates.append(str(raw))

        for c in candidates:
            m = _UUID_RE.search(c)
            if m:
                return m.group(0).lower()
        return ""

    # ---------------- recordings ----------------

    def _fetch_recordings(self, conv_id):
        url = self._base_url() + "/api/v2/conversations/" + conv_id + "/recordings"
        params = {"maxWaitMs": int(self.max_wait_ms or 5000)}
        if self.format_id and self.format_id != "DEFAULT":
            params["formatId"] = self.format_id

        resp = requests.get(url, headers=self._headers(), params=params, timeout=60)
        status = resp.status_code
        body = resp.text or ""

        if status == 202 or (status == 200 and not body.strip()):
            return "PROCESSING", [], status
        if status == 404:
            return "NOT_FOUND", [], status
        if status == 401:
            raise RuntimeError("HTTP 401 - token expired or invalid. Get a fresh token.")
        if status == 403:
            raise RuntimeError("HTTP 403 - token lacks recording permissions. Body: " + body[:300])
        if status >= 400:
            raise RuntimeError("HTTP " + str(status) + " - " + body[:300])

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                "Non-JSON response (HTTP " + str(status) + "). First 300 chars: " + body[:300]
            )

        if isinstance(data, dict):
            data = data.get("entities") or [data]
        return "OK", data, status

    def _extract_media_uris(self, recording):
        media_uris = recording.get("mediaUris") or {}
        out = []
        for key, block in media_uris.items():
            uri = (block or {}).get("mediaUri")
            if uri:
                out.append((key, uri))
        out.sort(key=lambda x: x[0])
        return out

    def _download_one(self, media_uri, file_name):
        folder = _shared_audio_dir()
        final_path = os.path.join(folder, _safe_name(file_name))

        if not self.force_redownload and os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
            os.utime(final_path, None)
            return {
                "file_path": final_path,
                "size_bytes": str(os.path.getsize(final_path)),
                "content_type": "cached",
                "reused": True,
            }

        part_path = final_path + "." + str(os.getpid()) + ".part"
        resp = requests.get(media_uri, timeout=180, stream=True)
        resp.raise_for_status()
        try:
            with open(part_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1048576):
                    if chunk:
                        f.write(chunk)
            if os.path.getsize(part_path) == 0:
                raise RuntimeError("Downloaded file is empty")
            os.replace(part_path, final_path)
        finally:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass

        return {
            "file_path": final_path,
            "size_bytes": str(os.path.getsize(final_path)),
            "content_type": resp.headers.get("content-type", "unknown"),
            "reused": False,
        }

    # ---------------- genesys hold events ----------------

    def _fetch_hold_events(self, conv_id, recording_start):
        url = self._base_url() + "/api/v2/analytics/conversations/" + conv_id + "/details"
        resp = requests.get(url, headers=self._headers(), timeout=30)

        if resp.status_code == 403:
            raise RuntimeError("HTTP 403 - missing analytics:conversationDetail:view permission")
        if resp.status_code == 404:
            raise RuntimeError("HTTP 404 - analytics details not available yet")
        if resp.status_code >= 400:
            raise RuntimeError("HTTP " + str(resp.status_code) + " - " + (resp.text or "")[:200])

        data = resp.json()
        origin = _parse_ts(recording_start) or _parse_ts(data.get("conversationStart"))
        if origin is None:
            raise RuntimeError("No reference start time to align hold events")

        holds = []
        wrapups = []
        queues = []

        for p in data.get("participants", []) or []:
            if p.get("purpose") not in ("agent", "user"):
                continue
            for s in p.get("sessions", []) or []:
                media = s.get("mediaType")
                if media and media != "voice":
                    continue
                for seg in s.get("segments", []) or []:
                    code = seg.get("wrapUpCode")
                    if code and code not in wrapups:
                        wrapups.append(code)
                    queue = seg.get("queueId")
                    if queue and queue not in queues:
                        queues.append(queue)
                    if seg.get("segmentType") != "hold":
                        continue
                    st = _parse_ts(seg.get("segmentStart"))
                    en = _parse_ts(seg.get("segmentEnd"))
                    if not st or not en:
                        continue
                    a = (st - origin).total_seconds()
                    b = (en - origin).total_seconds()
                    if b > a:
                        holds.append([a, b])

        holds.sort()
        merged = []
        for a, b in holds:
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])

        events = []
        for a, b in merged:
            events.append({
                "start": round(a, 1),
                "end": round(b, 1),
                "duration": round(b - a, 1),
            })
        return events, wrapups, queues

    # ---------------- main ----------------

    def download(self) -> Data:
        purged = _purge_old_audio(self.audio_retention_hours)

        conv_id = self._resolve_conversation_id()
        if not conv_id:
            return Data(data={
                "error": "NO_CONVERSATION_ID",
                "detail": "Provide a Conversation ID, or connect Trigger Data containing conversationId.",
            })

        attempts = max(1, int(self.max_poll_attempts or 6))
        interval = max(1, int(self.poll_interval_sec or 5))

        recording = None
        track_uris = []
        screen_recordings = []
        last_state = ""
        last_status = None

        attempt = 1
        while attempt <= attempts:
            try:
                state, recordings, last_status = self._fetch_recordings(conv_id)
            except Exception as e:
                return Data(data={
                    "error": "REQUEST_FAILED",
                    "conversation_id": conv_id,
                    "detail": str(e),
                })

            last_state = state

            if state == "NOT_FOUND":
                return Data(data={
                    "error": "NOT_FOUND",
                    "conversation_id": conv_id,
                    "detail": "Conversation not found, or it has no recordings.",
                })

            if state == "OK":
                audio = [r for r in recordings if r.get("media") == "audio"]
                screen_recordings = [r for r in recordings if r.get("media") == "screen"]
                if audio:
                    recording = audio[0]
                    track_uris = self._extract_media_uris(recording)
                    if track_uris and recording.get("fileState", "AVAILABLE") == "AVAILABLE":
                        break
                elif recordings:
                    return Data(data={
                        "error": "NO_AUDIO_RECORDING",
                        "conversation_id": conv_id,
                        "raw_media_types": [r.get("media") for r in recordings],
                    })

            if attempt < attempts:
                time.sleep(interval)
            attempt += 1

        if not recording or not track_uris:
            return Data(data={
                "error": "NOT_READY",
                "conversation_id": conv_id,
                "detail": "Recording not available after " + str(attempts)
                + " attempts. Last state=" + last_state
                + ", HTTP " + str(last_status) + ". Retry later.",
            })

        fmt = self.format_id if self.format_id and self.format_id != "DEFAULT" else "webm"
        suffix = "." + fmt.lower().replace("ogg_opus", "ogg")
        recording_id = recording.get("id") or "rec"

        tracks = []
        for track_key, uri in track_uris:
            file_name = conv_id + "_" + str(recording_id) + "_track" + str(track_key) + suffix
            try:
                info = self._download_one(uri, file_name)
                info["track"] = track_key
                tracks.append(info)
            except Exception as e:
                tracks.append({"track": track_key, "error": str(e)})

        agents = []
        for u in recording.get("users", []):
            agents.append({
                "id": u.get("id"),
                "name": u.get("name"),
                "email": u.get("username"),
            })

        result = {
            "conversation_id": conv_id,
            "recording_id": recording_id,
            "track_count": len(tracks),
            "tracks": tracks,
            "likely_dual_channel": len(tracks) == 2,
            "agents": agents,
            "start_time": recording.get("startTime"),
            "end_time": recording.get("endTime"),
            "format": fmt,
            "audio_folder": _shared_audio_dir(),
            "old_files_purged": purged,
            "hold_data_available": False,
            "genesys_holds": [],
            "hold_data_error": None,
            "wrapup_code_ids": [],
            "queue_ids": [],
        }

        if self.fetch_hold_events:
            try:
                holds, wrapups, queues = self._fetch_hold_events(conv_id, recording.get("startTime"))
                result["hold_data_available"] = True
                result["genesys_holds"] = holds
                result["wrapup_code_ids"] = wrapups
                result["queue_ids"] = queues
            except Exception as e:
                result["hold_data_error"] = str(e)
        else:
            result["hold_data_error"] = "disabled"

        if self.include_screen_recordings and screen_recordings:
            screen_tracks = []
            for sr in screen_recordings:
                for track_key, uri in self._extract_media_uris(sr):
                    file_name = conv_id + "_" + str(sr.get("id") or "screen") + "_screen" + str(track_key) + ".webm"
                    try:
                        info = self._download_one(uri, file_name)
                        info["track"] = track_key
                        screen_tracks.append(info)
                    except Exception as e:
                        screen_tracks.append({"track": track_key, "error": str(e)})
            result["screen_tracks"] = screen_tracks

        for t in tracks:
            if "file_path" in t:
                result["file_path"] = t["file_path"]
                break

        return Data(data=result)
