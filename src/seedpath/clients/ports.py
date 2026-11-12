"""Structural interfaces the core depends on.

Keeping the core behind protocols makes the worker and resolver testable with
small fakes instead of a live rTorrent and four Servarr containers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """An XML-RPC HTTP response, kept as bytes."""

    status_code: int
    body: bytes
    content_type: str | None


@runtime_checkable
class TorrentClient(Protocol):
    """The slice of rTorrent that seedpath uses."""

    async def forward(self, body: bytes, content_type: str | None = None) -> RpcResponse: ...

    async def call(self, method: str, *params: Any) -> Any: ...

    async def version(self) -> str: ...

    async def hash_exists(self, infohash: str) -> bool: ...

    async def set_directory(self, infohash: str, path: str) -> None: ...

    async def start(self, infohash: str) -> None: ...

    async def is_complete(self, infohash: str) -> bool: ...

    async def is_started(self, infohash: str) -> bool: ...

    async def erase(self, infohash: str) -> None: ...

    async def list_torrents(self, fields: list[str] | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class ArrClientProtocol(Protocol):
    """The slice of one Servarr app that seedpath uses."""

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    async def find_grab(self, infohash: str) -> dict[str, Any] | None: ...

    async def get_item(self, item_id: int) -> dict[str, Any]: ...

    async def get_episode(self, episode_id: int) -> dict[str, Any]: ...

    async def parse_title(self, title: str) -> dict[str, Any]: ...

    async def rescan(self, item_id: int, path: str) -> None: ...

    async def system_status(self) -> dict[str, Any]: ...
