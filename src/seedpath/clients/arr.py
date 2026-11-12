"""Servarr REST client.

One instance talks to one app. Version prefixes differ (v3 for Radarr,
Sonarr and Whisparr; v1 for Lidarr and Readarr) and are derived from the app
kind, never configured by hand.
"""

from __future__ import annotations

from typing import Any

import httpx

from seedpath.config import AppConfig
from seedpath.errors import ArrError
from seedpath.logging import get_logger

log = get_logger(__name__)

#: history record key that holds the owning item's id.
HISTORY_ID_KEYS: dict[str, str] = {
    "radarr": "movieId",
    "whisparr": "movieId",
    "sonarr": "seriesId",
    "lidarr": "artistId",
    "readarr": "authorId",
}

#: REST resource path for the owning item.
ITEM_PATHS: dict[str, str] = {
    "radarr": "movie",
    "whisparr": "movie",
    "sonarr": "series",
    "lidarr": "artist",
    "readarr": "author",
}

#: The rescan command each app understands.
RESCAN_COMMANDS: dict[str, str] = {
    "radarr": "RescanMovie",
    "whisparr": "RescanMovie",
    "sonarr": "RescanSeries",
    "lidarr": "RescanFolders",
    "readarr": "RescanFolders",
}


class ArrClient:
    """Typed wrapper around the parts of the *arr API seedpath needs."""

    def __init__(self, app: AppConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._app = app
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            trust_env=False,
            # Never follow a redirect: httpx strips Authorization but not
            # X-Api-Key, so a cross-origin redirect would leak the app key.
            follow_redirects=False,
        )

    @property
    def name(self) -> str:
        return self._app.name

    @property
    def kind(self) -> str:
        return self._app.kind

    @property
    def url(self) -> str:
        return self._app.url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _url(self, path: str) -> str:
        return f"{self._app.url}{self._app.api_prefix}/{path.lstrip('/')}"

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_body: Any) -> Any:
        return await self._request("POST", path, json_body=json_body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        url = self._url(path)
        headers = {
            "X-Api-Key": self._app.api_key.get_secret_value(),
            "Accept": "application/json",
        }
        try:
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            msg = f"{self._app.name}: {method} {path} failed: {exc}"
            raise ArrError(msg) from exc
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            msg = (
                f"{self._app.name}: {method} {path} redirected to {location!r}; refusing to follow"
            )
            raise ArrError(msg, status_code=response.status_code)
        if response.status_code >= 400:
            snippet = response.text[:500]
            msg = f"{self._app.name}: {method} {path} returned HTTP {response.status_code}"
            raise ArrError(msg, status_code=response.status_code, body=snippet)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            msg = f"{self._app.name}: {method} {path} returned invalid JSON"
            raise ArrError(msg, status_code=response.status_code) from exc

    async def system_status(self) -> dict[str, Any]:
        data = await self.get("system/status")
        return data if isinstance(data, dict) else {}

    async def find_grab(self, infohash: str) -> dict[str, Any] | None:
        """Find the ``Grabbed`` history row for an infohash.

        The row is written *after* the download client returns, so callers
        must retry; a 200 with an empty ``records`` list is not an error.
        """
        data = await self.get(
            "history",
            {"downloadId": infohash, "eventType": 1, "page": 1, "pageSize": 5},
        )
        if not isinstance(data, dict):
            return None
        records = data.get("records")
        if not isinstance(records, list):
            return None
        for record in records:
            if isinstance(record, dict):
                return record
        return None

    async def get_item(self, item_id: int) -> dict[str, Any]:
        path = ITEM_PATHS[self._app.kind]
        data = await self.get(f"{path}/{item_id}")
        if not isinstance(data, dict):
            msg = f"{self._app.name}: unexpected response for {path}/{item_id}"
            raise ArrError(msg)
        return data

    async def get_episode(self, episode_id: int) -> dict[str, Any]:
        data = await self.get(f"episode/{episode_id}")
        if not isinstance(data, dict):
            msg = f"{self._app.name}: unexpected response for episode/{episode_id}"
            raise ArrError(msg)
        return data

    async def parse_title(self, title: str) -> dict[str, Any]:
        data = await self.get("parse", {"title": title})
        return data if isinstance(data, dict) else {}

    async def rescan(self, item_id: int, path: str) -> None:
        """Ask the app to scan the folder, adopting files in place."""
        kind = self._app.kind
        command = RESCAN_COMMANDS[kind]
        if kind in {"lidarr", "readarr"}:
            body: dict[str, Any] = {"name": command, "folders": [path]}
        else:
            id_key = HISTORY_ID_KEYS[kind]
            body = {"name": command, id_key: item_id}
        await self.post("command", body)
        log.info("arr.rescan_requested", app=self.name, item_id=item_id, path=path)
