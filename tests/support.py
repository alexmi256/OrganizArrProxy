"""Shared test builders and fakes."""

from __future__ import annotations

import base64
import hashlib
import xmlrpc.client
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from seedpath.clients.ports import RpcResponse
from seedpath.config import Settings
from seedpath.core.models import GrabJob
from seedpath.core.resolver import Resolver
from seedpath.errors import ArrError, UpstreamError
from seedpath.proxy.service import ProxyService
from seedpath.runtime import Runtime


def bencode(value: Any) -> bytes:
    """Minimal encoder, used only to build fixtures."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted(value.items())
        return b"d" + b"".join(bencode(key) + bencode(item) for key, item in items) + b"e"
    msg = f"cannot bencode {type(value)!r}"
    raise TypeError(msg)


@dataclass(frozen=True)
class TorrentFixture:
    data: bytes
    infohash: str
    name: str


def make_torrent(
    name: str = "Big.Buck.Bunny.2008.1080p", *, multifile: bool = False
) -> TorrentFixture:
    info: dict[bytes, Any] = {
        b"name": name.encode(),
        b"piece length": 16384,
        b"pieces": b"\x00" * 20,
    }
    if multifile:
        info[b"files"] = [
            {b"length": 1024, b"path": [b"Big.Buck.Bunny.mkv"]},
            {b"length": 64, b"path": [b"Big.Buck.Bunny.nfo"]},
        ]
    else:
        info[b"length"] = 1024 * 1024
    payload = {
        b"announce": b"http://tracker.invalid/announce",
        b"info": info,
    }
    data = bencode(payload)
    return TorrentFixture(
        data=data,
        infohash=hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest(),
        name=name,
    )


def magnet_for(fixture: TorrentFixture, *, base32: bool = False, name: str | None = None) -> str:
    if base32:
        value = base64.b32encode(bytes.fromhex(fixture.infohash)).decode()
    else:
        value = fixture.infohash
    display = f"&dn={name or fixture.name}"
    return f"magnet:?xt=urn:btih:{value}&tr=udp%3A%2F%2Ftracker.invalid%3A80{display}"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "state_path": tmp_path / "state.db",
        "library_roots": ["/data/movies", "/data/tv"],
        "radarr_url": "http://radarr:7878",
        "radarr_api_key": "radarr-key",
        "sonarr_url": "http://sonarr:8989",
        "sonarr_api_key": "sonarr-key",
        "category_map": {"radarr": "radarr", "tv-sonarr": "sonarr"},
        "resolve_timeout": 0.01,
        "rescan_poll": 0.01,
        "load_timeout": 0.01,
        "reconcile_interval": 60,
        "background_workers": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_job(
    fixture: TorrentFixture,
    *,
    category: str | None = "radarr",
    is_raw: bool = True,
    release_name: str | None = None,
) -> GrabJob:
    return GrabJob(
        infohash=fixture.infohash,
        category=category,
        release_name=release_name or fixture.name,
        is_raw=is_raw,
        received_at=datetime.now(UTC),
    )


class FakeTorrent:
    """Records calls and lets tests inject faults."""

    def __init__(self) -> None:
        self.hash_present = True
        self.complete = False
        self.version_string = "0.9.8"
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.forwarded: list[bytes] = []
        self.forward_response = xmlrpc.client.dumps((0,), methodresponse=True).encode()
        self.forward_error: Exception | None = None
        self.forward_status = 200
        self.set_directory_faults = 0
        self.start_faults = 0
        self.state_faults = 0
        self.started = False
        self.complete_fault = False
        self.raise_version = False
        self.torrents: list[dict[str, Any]] = []
        self.erased: list[str] = []

    async def aclose(self) -> None:
        return None

    async def forward(self, body: bytes, content_type: str | None = None) -> RpcResponse:
        self.forwarded.append(body)
        if self.forward_error is not None:
            raise self.forward_error
        return RpcResponse(self.forward_status, self.forward_response, "text/xml")

    async def call(self, method: str, *params: Any) -> Any:
        self.calls.append((method, params))
        if method == "d.name":
            if not self.hash_present:
                raise xmlrpc.client.Fault(-501, "Torrent not found")
            return "Big.Buck.Bunny"
        if method == "d.directory.set":
            if self.set_directory_faults > 0:
                self.set_directory_faults -= 1
                raise xmlrpc.client.Fault(-501, "Could not create directory")
            return 0
        if method == "d.start":
            if self.start_faults > 0:
                self.start_faults -= 1
                raise xmlrpc.client.Fault(-501, "Cannot start")
            self.started = True
            return 0
        if method == "d.complete":
            return int(self.complete)
        if method in {"d.state", "d.is_active"} and self.state_faults > 0:
            self.state_faults -= 1
            raise xmlrpc.client.Fault(-501, "transient")
        if method == "d.state":
            # Paused and started items are both open (state 1); is_active
            # distinguishes them, matching rTorrent's documented semantics.
            return 1
        if method == "d.is_active":
            return int(self.started)
        if method == "d.erase":
            self.erased.append(str(params[0]))
            return 0
        if method == "d.multicall2":
            return [
                [
                    torrent.get("hash"),
                    torrent.get("name"),
                    torrent.get("base_path", ""),
                    torrent.get("directory", ""),
                    torrent.get("custom1"),
                    torrent.get("state", 0),
                    torrent.get("is_active", 0),
                    torrent.get("complete", 0),
                    torrent.get("left_bytes", 0),
                    torrent.get("size_bytes", 0),
                ]
                for torrent in self.torrents
            ]
        if method == "system.client_version":
            if self.raise_version:
                raise UpstreamError("down")
            return self.version_string
        msg = f"unexpected rTorrent call: {method}"
        raise AssertionError(msg)

    async def version(self) -> str:
        if self.raise_version:
            raise UpstreamError("down")
        return self.version_string

    async def hash_exists(self, infohash: str) -> bool:
        try:
            await self.call("d.name", infohash)
        except xmlrpc.client.Fault:
            return False
        return True

    async def set_directory(self, infohash: str, path: str) -> None:
        await self.call("d.directory.set", infohash, path)

    async def start(self, infohash: str) -> None:
        await self.call("d.start", infohash)

    async def is_complete(self, infohash: str) -> bool:
        if self.complete_fault:
            raise xmlrpc.client.Fault(-501, "transient")
        if not self.hash_present:
            raise xmlrpc.client.Fault(-501, "Torrent not found")
        return bool(await self.call("d.complete", infohash))

    async def is_started(self, infohash: str) -> bool:
        if not await self.call("d.state", infohash):
            return False
        return bool(await self.call("d.is_active", infohash))

    async def erase(self, infohash: str) -> None:
        await self.call("d.erase", infohash)

    async def list_torrents(self, fields: list[str] | None = None) -> list[dict[str, Any]]:
        return list(self.torrents)


class FakeArr:
    def __init__(self, name: str = "radarr", kind: str = "radarr") -> None:
        self.name = name
        self.kind = kind
        self.history: dict[str, Any] | None = None
        self.item: dict[str, Any] = {
            "id": 1,
            "path": "/data/movies/Big Buck Bunny (2008)",
            "seasonFolder": False,
        }
        self.episode: dict[str, Any] = {"seasonNumber": 2}
        self.parse: dict[str, Any] = {}
        self.find_error: Exception | None = None
        self.rescans: list[tuple[int, str]] = []
        self.find_calls = 0
        self.status_error: Exception | None = None

    async def find_grab(self, infohash: str) -> dict[str, Any] | None:
        self.find_calls += 1
        if self.find_error is not None:
            raise self.find_error
        return self.history

    async def get_item(self, item_id: int) -> dict[str, Any]:
        return self.item

    async def get_episode(self, episode_id: int) -> dict[str, Any]:
        return self.episode

    async def parse_title(self, title: str) -> dict[str, Any]:
        return self.parse

    async def rescan(self, item_id: int, path: str) -> None:
        self.rescans.append((item_id, path))

    async def system_status(self) -> dict[str, Any]:
        if self.status_error is not None:
            raise self.status_error
        return {"version": "5.0.0"}


class FakeWorker:
    def __init__(self) -> None:
        self.jobs: list[GrabJob] = []

    async def enqueue(self, job: GrabJob) -> None:
        self.jobs.append(job)


def make_runtime(
    settings: Settings,
    *,
    torrent: FakeTorrent | None = None,
    apps: dict[str, FakeArr] | None = None,
    worker: FakeWorker | None = None,
) -> Runtime:
    torrent = torrent or FakeTorrent()
    apps = apps or {"radarr": FakeArr()}
    worker = worker or FakeWorker()
    resolver = Resolver(
        cast(Any, apps),
        settings.configured_category_map,
        settings.library_roots,
        resolve_timeout=settings.resolve_timeout,
    )
    return Runtime(
        settings=settings,
        store=cast(Any, None),
        torrent=cast(Any, torrent),
        apps=cast(Any, apps),
        resolver=cast(Any, resolver),
        worker=cast(Any, worker),
        proxy=ProxyService(settings, worker),
        arr_http=cast(Any, None),
    )


def assert_error_is_arr(exc: Exception) -> None:
    assert isinstance(exc, ArrError)
