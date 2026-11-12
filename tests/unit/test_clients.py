"""rTorrent and Servarr HTTP clients against mocked transports."""

from __future__ import annotations

import base64
import json
import xmlrpc.client
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from seedpath.clients.arr import ArrClient
from seedpath.clients.rtorrent import RTorrentClient
from seedpath.config import AppConfig
from seedpath.errors import ArrError, UpstreamError


def _rpc_transport(
    responder: Callable[[str, tuple[Any, ...]], Any],
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        params, method = xmlrpc.client.loads(request.content)
        assert method is not None
        try:
            result = responder(method, params)
        except xmlrpc.client.Fault as fault:
            body = xmlrpc.client.dumps(fault, methodresponse=True).encode()
        else:
            body = xmlrpc.client.dumps((result,), methodresponse=True).encode()
        return httpx.Response(200, content=body, headers={"content-type": "text/xml"})

    return httpx.MockTransport(handle)


async def test_call_builds_typed_request() -> None:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    def responder(method: str, params: tuple[Any, ...]) -> Any:
        seen.append((method, params))
        return "0.9.8"

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2",
        client=httpx.AsyncClient(transport=_rpc_transport(responder)),
    )
    assert await client.call("system.client_version") == "0.9.8"
    assert seen == [("system.client_version", ())]
    await client.aclose()


async def test_forward_passes_raw_bytes_and_status() -> None:
    received: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return httpx.Response(200, content=b"<fault/>", headers={"content-type": "text/xml"})

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2", client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    response = await client.forward(b"raw-body")
    assert received == [b"raw-body"]
    assert response.status_code == 200
    assert response.body == b"<fault/>"
    assert response.content_type == "text/xml"


async def test_http_error_raises_upstream_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2", client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(UpstreamError, match="HTTP 503"):
        await client.call("d.name", "a" * 40)


async def test_transport_error_raises_upstream_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2", client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    with pytest.raises(UpstreamError, match="failed"):
        await client.forward(b"x")


async def test_fault_propagates() -> None:
    def responder(method: str, params: tuple[Any, ...]) -> Any:
        raise xmlrpc.client.Fault(-501, "Torrent not found")

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2",
        client=httpx.AsyncClient(transport=_rpc_transport(responder)),
    )
    assert await client.hash_exists("a" * 40) is False


async def test_worker_operations() -> None:
    captured: dict[str, Any] = {}

    def responder(method: str, params: tuple[Any, ...]) -> Any:
        captured[method] = params
        if method == "d.complete":
            return 1
        if method == "d.state":
            return 1
        if method == "d.is_active":
            return 1
        if method == "d.name":
            return "Some Torrent"
        if method == "network.xmlrpc.size_limit":
            return 1048576
        return 0

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2",
        client=httpx.AsyncClient(transport=_rpc_transport(responder)),
    )
    await client.set_directory("a" * 40, "/data/movies/Movie")
    await client.start("a" * 40)
    assert await client.is_complete("a" * 40) is True
    assert await client.is_started("a" * 40) is True
    await client.erase("a" * 40)
    assert captured["d.directory.set"] == ("a" * 40, "/data/movies/Movie")
    assert captured["d.start"] == ("a" * 40,)
    assert captured["d.erase"] == ("a" * 40,)
    assert await client.xmlrpc_size_limit() == 1048576


async def test_is_started_does_not_treat_a_paused_torrent_as_running() -> None:
    def responder(method: str, params: tuple[Any, ...]) -> Any:
        if method == "d.state":
            return 1
        if method == "d.is_active":
            return 0
        return 0

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2",
        client=httpx.AsyncClient(transport=_rpc_transport(responder)),
    )
    assert await client.is_started("a" * 40) is False


async def test_list_torrents_maps_fields() -> None:
    def responder(method: str, params: tuple[Any, ...]) -> Any:
        assert method == "d.multicall2"
        return [["a" * 40, "Name", "/data/movies/Name", "/data/movies", "radarr", 1, 0, 1]]

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2",
        client=httpx.AsyncClient(transport=_rpc_transport(responder)),
    )
    torrents = await client.list_torrents(
        [
            "d.hash=",
            "d.name=",
            "d.base_path=",
            "d.directory=",
            "d.custom1=",
            "d.state=",
            "d.is_active=",
            "d.complete=",
        ]
    )
    assert torrents == [
        {
            "hash": "a" * 40,
            "name": "Name",
            "base_path": "/data/movies/Name",
            "directory": "/data/movies",
            "custom1": "radarr",
            "state": 1,
            "is_active": 0,
            "complete": 1,
        }
    ]


async def test_basic_auth_is_sent() -> None:
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, content=xmlrpc.client.dumps((1,), methodresponse=True).encode())

    client = RTorrentClient(
        "http://rtorrent:8000/RPC2",
        username="user",
        password="pass",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    await client.call("d.name", "a" * 40)
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    assert seen == [expected]


def _arr_app(kind: str = "radarr", name: str | None = None) -> AppConfig:
    return AppConfig(
        name=name or kind,
        kind=kind,
        url="http://arr:7878",
        api_key="secret",
    )


async def test_arr_request_headers_and_url() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"version": "5.0.0"})

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert await client.system_status() == {"version": "5.0.0"}
    assert seen[0].url.path == "/api/v3/system/status"
    assert seen[0].headers["x-api-key"] == "secret"


async def test_arr_find_grab() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("downloadId") == "empty":
            return httpx.Response(200, json={"records": []})
        return httpx.Response(200, json={"records": [{"movieId": 3}, {"movieId": 4}]})

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert await client.find_grab("abc") == {"movieId": 3}
    assert await client.find_grab("empty") is None


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("radarr", "/api/v3/movie/1"),
        ("sonarr", "/api/v3/series/1"),
        ("lidarr", "/api/v1/artist/1"),
        ("readarr", "/api/v1/author/1"),
        ("whisparr", "/api/v3/movie/1"),
    ],
)
async def test_arr_item_paths_follow_api_version(kind: str, path: str) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": 1, "path": "/data/item"})

    client = ArrClient(
        _arr_app(kind), client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    assert (await client.get_item(1))["path"] == "/data/item"
    assert seen == [path]


@pytest.mark.parametrize(
    ("kind", "command", "key"),
    [
        ("radarr", "RescanMovie", "movieId"),
        ("sonarr", "RescanSeries", "seriesId"),
        ("lidarr", "RescanFolders", "folders"),
    ],
)
async def test_arr_rescan_bodies(kind: str, command: str, key: str) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={})

    client = ArrClient(
        _arr_app(kind), client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    await client.rescan(9, "/data/path")
    assert seen[0]["name"] == command
    assert key in seen[0]


async def test_arr_redirect_is_not_followed() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://evil.invalid/steal"})

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(ArrError, match="redirected"):
        await client.get("movie/1")
    assert len(seen) == 1


async def test_arr_error_status_and_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(ArrError) as info:
        await client.get("movie/1")
    assert info.value.status_code == 500
    assert "boom" in info.value.body


async def test_arr_transport_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(ArrError, match="failed"):
        await client.get("movie/1")


async def test_arr_invalid_json() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(ArrError, match="invalid JSON"):
        await client.get("movie/1")


async def test_arr_empty_response_is_none() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = ArrClient(_arr_app(), client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert await client.post("command", {"name": "X"}) is None
