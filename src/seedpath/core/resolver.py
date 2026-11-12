"""Map a grab to the folder the release belongs in.

Resolution order follows the brief: the authoritative path is the *arr
history row (written moments after the grab), with ``parse?title=`` as the
name-heuristic fallback. The resulting path is checked against
``LIBRARY_ROOTS`` before it is handed to rTorrent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from seedpath.clients.arr import HISTORY_ID_KEYS, ITEM_PATHS
from seedpath.clients.ports import ArrClientProtocol
from seedpath.core.models import GrabJob, ResolvedItem
from seedpath.errors import ArrError, ResolutionError
from seedpath.logging import get_logger
from seedpath.util import normalize_posix_path, path_within_roots

log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def compose_destination(kind: str, item: Mapping[str, Any], season_number: int | None) -> str:
    """Build the destination folder from an *arr item resource."""
    path = item.get("path")
    if not isinstance(path, str) or not path:
        msg = f"{kind} item has no path"
        raise ResolutionError(msg)

    try:
        if kind != "sonarr" or not item.get("seasonFolder"):
            return normalize_posix_path(path)
        if season_number is None:
            msg = "Sonarr has season folders enabled but the season number is unknown"
            raise ResolutionError(msg)
        return normalize_posix_path(f"{path.rstrip('/')}/Season {season_number:02d}")
    except ValueError as exc:
        msg = f"{kind} item has an unusable path: {path!r}"
        raise ResolutionError(msg) from exc


class Resolver:
    """Resolve a :class:`GrabJob` into a library destination."""

    def __init__(
        self,
        apps: Mapping[str, ArrClientProtocol],
        category_map: Mapping[str, str],
        library_roots: list[str],
        *,
        resolve_timeout: float = 10.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._apps = dict(apps)
        self._category_map = dict(category_map)
        self._library_roots = list(library_roots)
        self._timeout = resolve_timeout
        self._sleep = sleep

    def app_for_category(self, category: str | None) -> ArrClientProtocol:
        if category is None:
            msg = "torrent has no category; cannot choose an app"
            raise ResolutionError(msg)
        app_name = self._category_map.get(category)
        if app_name is None:
            msg = f"category {category!r} is not in CATEGORY_MAP"
            raise ResolutionError(msg)
        app = self._apps.get(app_name)
        if app is None:
            msg = f"category {category!r} maps to unconfigured app {app_name!r}"
            raise ResolutionError(msg)
        return app

    async def resolve(self, job: GrabJob) -> ResolvedItem:
        app = self.app_for_category(job.category)
        deadline = asyncio.get_running_loop().time() + self._timeout
        delay = 0.25
        last_error: str | None = None

        while True:
            try:
                item = await self._from_history(app, job)
            except ArrError as exc:
                last_error = str(exc)
                log.warning("resolve.history_failed", infohash=job.infohash, error=last_error)
                item = None
            if item is not None:
                return self._checked(item)

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await self._sleep(min(delay, remaining))
            delay = min(delay * 2, 2.0)

        try:
            item = await self._from_parse(app, job)
        except ArrError as exc:
            last_error = str(exc)
            log.warning("resolve.parse_failed", infohash=job.infohash, error=last_error)
            item = None
        if item is not None:
            return self._checked(item)

        detail = f": {last_error}" if last_error else ""
        msg = f"could not resolve {job.infohash} via history or parse{detail}"
        raise ResolutionError(msg)

    def _checked(self, item: ResolvedItem) -> ResolvedItem:
        if not self._library_roots:
            msg = "LIBRARY_ROOTS is empty; refusing to place anywhere"
            raise ResolutionError(msg)
        if not path_within_roots(item.path, self._library_roots):
            msg = f"destination {item.path!r} is outside LIBRARY_ROOTS"
            raise ResolutionError(msg)
        return item

    async def _from_history(self, app: ArrClientProtocol, job: GrabJob) -> ResolvedItem | None:
        record = await app.find_grab(job.infohash)
        if record is None:
            return None
        kind = app.kind
        id_key = HISTORY_ID_KEYS[kind]
        item_id = _as_int(record.get(id_key))
        if item_id is None:
            log.warning(
                "resolve.history_missing_id",
                infohash=job.infohash,
                app=app.name,
                key=id_key,
            )
            return None

        item = await app.get_item(item_id)
        season_number: int | None = None
        if kind == "sonarr" and item.get("seasonFolder"):
            season_number = _as_int(record.get("seasonNumber"))
            episode_id = _as_int(record.get("episodeId"))
            if season_number is None and episode_id is not None:
                episode = await app.get_episode(episode_id)
                season_number = _as_int(episode.get("seasonNumber"))
            if season_number is None:
                log.warning("resolve.history_missing_season", infohash=job.infohash)
                return None

        return ResolvedItem(
            app=app.name,
            item_id=item_id,
            kind=kind,
            path=compose_destination(kind, item, season_number),
            season_number=season_number,
            source="history",
        )

    async def _from_parse(self, app: ArrClientProtocol, job: GrabJob) -> ResolvedItem | None:
        if not job.release_name:
            return None
        payload = await app.parse_title(job.release_name)
        kind = app.kind
        item_id: int | None = None
        item: dict[str, Any] | None = None

        nested = payload.get(ITEM_PATHS[kind])
        if isinstance(nested, dict) and (candidate := _as_int(nested.get("id"))) is not None:
            item_id = candidate
            item = nested
        else:
            item_id = _as_int(payload.get(HISTORY_ID_KEYS[kind]))

        season_number: int | None = None
        parsed_info = payload.get("parsedEpisodeInfo")
        if isinstance(parsed_info, dict):
            season_number = _as_int(parsed_info.get("seasonNumber"))

        if item_id is None:
            return None
        if item is None:
            item = await app.get_item(item_id)

        if kind == "sonarr" and item.get("seasonFolder") and season_number is None:
            log.warning("resolve.parse_missing_season", infohash=job.infohash)
            return None

        return ResolvedItem(
            app=app.name,
            item_id=item_id,
            kind=kind,
            path=compose_destination(kind, item, season_number),
            season_number=season_number,
            source="parse",
        )
