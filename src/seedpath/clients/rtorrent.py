"""rTorrent XML-RPC client (egress side of the proxy)."""

from __future__ import annotations

import xmlrpc.client
from typing import Any

import httpx

from seedpath.clients.ports import RpcResponse
from seedpath.errors import UpstreamError
from seedpath.logging import get_logger
from seedpath.proxy.xmlrpc import build_request

log = get_logger(__name__)

#: Fields used by the queue poll and the reconciler. The trailing ``=`` is part
#: of rTorrent's multicast syntax.
DEFAULT_FIELDS = [
    "d.hash=",
    "d.name=",
    "d.base_path=",
    "d.directory=",
    "d.custom1=",
    "d.state=",
    "d.is_active=",
    "d.complete=",
    "d.left_bytes=",
    "d.size_bytes=",
]


class RTorrentClient:
    """Thin, typed-ish wrapper around rTorrent's XML-RPC endpoint."""

    def __init__(
        self,
        url: str,
        *,
        username: str = "",
        password: str = "",
        timeout: float = 30.0,
        verify_tls: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._auth: tuple[str, str] | None = (username, password) if username else None
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            verify=verify_tls,
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def forward(self, body: bytes, content_type: str | None = None) -> RpcResponse:
        """POST a raw XML-RPC body and return the raw response."""
        headers = {"Content-Type": content_type or "text/xml"}
        try:
            if self._auth is None:
                response = await self._http.post(self._url, content=body, headers=headers)
            else:
                response = await self._http.post(
                    self._url, content=body, headers=headers, auth=self._auth
                )
        except httpx.HTTPError as exc:
            msg = f"rTorrent XML-RPC request failed: {exc}"
            raise UpstreamError(msg) from exc
        return RpcResponse(
            status_code=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type"),
        )

    async def call(self, method: str, *params: Any) -> Any:
        """Typed call used by the worker and health checks."""
        response = await self.forward(build_request(method, params))
        if response.status_code != 200:
            snippet = response.body[:300].decode("utf-8", "replace")
            msg = f"rTorrent returned HTTP {response.status_code} for {method}: {snippet}"
            raise UpstreamError(msg)
        try:
            result, _ = xmlrpc.client.loads(response.body)
        except xmlrpc.client.Fault:
            raise
        except Exception as exc:
            msg = f"invalid XML-RPC response from rTorrent for {method}: {exc}"
            raise UpstreamError(msg) from exc
        if not isinstance(result, tuple) or len(result) != 1:
            msg = f"unexpected XML-RPC response shape for {method}: {result!r}"
            raise UpstreamError(msg)
        return result[0]

    async def version(self) -> str:
        result = await self.call("system.client_version")
        return str(result)

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
        return bool(await self.call("d.complete", infohash))

    async def is_started(self, infohash: str) -> bool:
        """True only for a started (not merely paused) torrent.

        ``d.state`` is 1 for both paused and started items; ``d.is_active``
        distinguishes them. A torrent added with ``load.normal`` is open and
        paused (state 1, active 0), which must not be mistaken for running.
        """
        if not await self.call("d.state", infohash):
            return False
        return bool(await self.call("d.is_active", infohash))

    async def erase(self, infohash: str) -> None:
        await self.call("d.erase", infohash)

    async def list_torrents(self, fields: list[str] | None = None) -> list[dict[str, Any]]:
        """Targetless multicast over every torrent, one dict per torrent."""
        field_list = fields or DEFAULT_FIELDS
        result = await self.call("d.multicall2", "", "", *field_list)
        if not isinstance(result, list):
            msg = "d.multicall2 did not return a list"
            raise UpstreamError(msg)
        names = [field.removesuffix("=").removeprefix("d.") for field in field_list]
        torrents: list[dict[str, Any]] = []
        for row in result:
            if not isinstance(row, list):
                continue
            torrents.append(dict(zip(names, row, strict=False)))
        return torrents

    async def xmlrpc_size_limit(self) -> int | None:
        """Best-effort read of ``network.xmlrpc.size_limit``."""
        try:
            value = await self.call("network.xmlrpc.size_limit")
        except (xmlrpc.client.Fault, UpstreamError):
            return None
        return int(value) if isinstance(value, int | str) else None
