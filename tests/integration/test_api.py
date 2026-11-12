"""HTTP surface: XML-RPC passthrough, auth, health and metrics."""

from __future__ import annotations

import base64
import xmlrpc.client
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from seedpath.api.app import create_app
from seedpath.config import Settings
from seedpath.errors import ArrError, UpstreamError
from tests.support import (
    FakeArr,
    FakeTorrent,
    FakeWorker,
    make_runtime,
    make_settings,
    make_torrent,
)


def _client(
    settings: Settings,
    *,
    torrent: FakeTorrent | None = None,
    apps: dict[str, FakeArr] | None = None,
    worker: FakeWorker | None = None,
) -> tuple[TestClient, FakeTorrent, FakeWorker]:
    torrent = torrent or FakeTorrent()
    worker = worker or FakeWorker()
    runtime = make_runtime(settings, torrent=torrent, apps=apps, worker=worker)
    return TestClient(create_app(settings, runtime=runtime, background=False)), torrent, worker


def _raw_load(payload: bytes, method: str = "load.raw_start") -> bytes:
    return xmlrpc.client.dumps(
        ("", xmlrpc.client.Binary(payload), "d.custom1.set=radarr"),
        methodname=method,
    ).encode()


def test_load_is_paused_and_job_enqueued(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, torrent, worker = _client(settings)
    fixture = make_torrent()
    body = _raw_load(fixture.data)

    with client:
        response = client.post("/RPC2", content=body, headers={"content-type": "text/xml"})

    assert response.status_code == 200
    assert response.content == torrent.forward_response
    params, method = xmlrpc.client.loads(torrent.forwarded[0])
    assert method == "load.raw"
    assert isinstance(params[1], xmlrpc.client.Binary)
    assert params[1].data == fixture.data
    assert [job.infohash for job in worker.jobs] == [fixture.infohash]
    assert worker.jobs[0].category == "radarr"


def test_paused_load_is_not_rewritten(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, torrent, worker = _client(settings)
    body = _raw_load(make_torrent().data, method="load.raw")

    with client:
        client.post("/RPC2", content=body)

    assert torrent.forwarded[0] == body
    assert len(worker.jobs) == 1


def test_non_load_calls_pass_through_byte_for_byte(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, torrent, worker = _client(settings)
    body = xmlrpc.client.dumps(("", "", "d.name=", "d.hash="), methodname="d.multicall2").encode()

    with client:
        response = client.post("/RPC2", content=body)

    assert response.status_code == 200
    assert torrent.forwarded[0] == body
    assert worker.jobs == []


def test_duplicate_fault_passes_through_without_worker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, torrent, worker = _client(settings)
    fault = xmlrpc.client.dumps(
        xmlrpc.client.Fault(-501, "Duplicate torrent"), methodresponse=True
    ).encode()
    torrent.forward_response = fault

    with client:
        response = client.post("/RPC2", content=_raw_load(make_torrent().data))

    assert response.content == fault
    assert worker.jobs == []


def test_magnet_load_is_accepted(tmp_path: Path) -> None:
    from tests.support import magnet_for

    settings = make_settings(tmp_path)
    client, torrent, worker = _client(settings)
    fixture = make_torrent()
    body = xmlrpc.client.dumps(
        ("", magnet_for(fixture), "d.custom1.set=radarr"),
        methodname="load.start",
    ).encode()

    with client:
        client.post("/RPC2", content=body)

    _, method = xmlrpc.client.loads(torrent.forwarded[0])
    assert method == "load.normal"
    assert worker.jobs[0].infohash == fixture.infohash


def test_unidentifiable_load_is_not_paused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, torrent, worker = _client(settings)
    body = xmlrpc.client.dumps(
        ("", "https://example.invalid/file.torrent", "d.custom1.set=radarr"),
        methodname="load.start",
    ).encode()

    with client:
        client.post("/RPC2", content=body)

    assert torrent.forwarded[0] == body
    assert worker.jobs == []


def test_upstream_failure_returns_502(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    torrent = FakeTorrent()
    torrent.forward_error = UpstreamError("connection refused")
    client, _, _ = _client(settings, torrent=torrent)

    with client:
        response = client.post("/RPC2", content=_raw_load(make_torrent().data))

    assert response.status_code == 502


def test_ingress_auth_is_enforced(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, rt_user="user", rt_pass="pass")
    client, _, _ = _client(settings)
    body = _raw_load(make_torrent().data)

    with client:
        unauthorised = client.post("/RPC2", content=body)
        wrong = client.post(
            "/RPC2", content=body, headers={"authorization": "Basic d3Jvbmc6bm9wZQ=="}
        )
        token = base64.b64encode(b"user:pass").decode()
        ok = client.post("/RPC2", content=body, headers={"authorization": f"Basic {token}"})

    assert unauthorised.status_code == 401
    assert unauthorised.headers["www-authenticate"].startswith("Basic")
    assert wrong.status_code == 401
    assert ok.status_code == 200


def test_ingress_auth_can_be_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, rt_user="user", rt_pass="pass", ingress_auth=False)
    client, _, _ = _client(settings)
    with client:
        assert client.post("/RPC2", content=_raw_load(make_torrent().data)).status_code == 200


def test_oversized_body_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_body_bytes=1024)
    client, torrent, _ = _client(settings)
    with client:
        response = client.post("/RPC2", content=b"x" * 2048)
    assert response.status_code == 413
    assert torrent.forwarded == []


def test_chunked_body_is_rejected_without_content_length(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_body_bytes=1024)
    client, torrent, _ = _client(settings)

    def body() -> Iterator[bytes]:
        yield b"x" * 600
        yield b"y" * 600

    with client:
        response = client.post("/RPC2", content=body())
    assert response.status_code == 413
    assert torrent.forwarded == []


def test_healthz_reports_dependencies(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    torrent = FakeTorrent()
    app = FakeArr()
    client, _, _ = _client(settings, torrent=torrent, apps={"radarr": app})

    with client:
        response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert {check["name"] for check in payload["checks"]} == {"rtorrent", "radarr"}


def test_healthz_degrades_when_app_fails(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = FakeArr()
    app.status_error = ArrError("down")
    client, _, _ = _client(settings, apps={"radarr": app})

    with client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_livez_and_metrics(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, _, _ = _client(settings)
    with client:
        assert client.get("/livez").status_code == 200
        metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert b"seedpath_grabs_total" in metrics.content
    assert b"seedpath_placements_total" in metrics.content


def test_index(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client, _, _ = _client(settings)
    with client:
        response = client.get("/")
    assert response.status_code == 200
    assert "/RPC2" in response.json()["endpoints"]


@pytest.mark.parametrize(
    ("path", "method"), [("/RPC2", "get"), ("/healthz", "post"), ("/livez", "post")]
)
def test_wrong_methods_are_rejected(tmp_path: Path, path: str, method: str) -> None:
    settings = make_settings(tmp_path)
    client, _, _ = _client(settings)
    with client:
        assert client.request(method, path).status_code == 405


class _OrderingWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.response_started = False
        self.seen_response_started: bool | None = None

    async def enqueue(self, job: Any) -> None:
        self.seen_response_started = self.response_started
        await super().enqueue(job)


async def test_response_is_sent_before_worker_enqueue(tmp_path: Path) -> None:
    """AGENTS invariant 3: arr must be answered before follow-up work starts."""
    settings = make_settings(tmp_path)
    worker = _OrderingWorker()
    runtime = make_runtime(settings, worker=worker)
    app = create_app(settings, runtime=runtime, background=False)
    app.state.runtime = runtime

    body = _raw_load(make_torrent().data)
    messages: list[MutableMapping[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            worker.response_started = True
        messages.append(message)

    scope: Any = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/RPC2",
        "raw_path": b"/RPC2",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"text/xml"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8080),
    }

    await app(scope, receive, send)

    assert worker.seen_response_started is True
    assert len(worker.jobs) == 1
    assert any(message["type"] == "http.response.body" for message in messages)
