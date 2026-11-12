"""ASGI application: XML-RPC ingress, health and metrics."""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from seedpath import __version__
from seedpath.config import Settings
from seedpath.errors import UpstreamError
from seedpath.logging import get_logger
from seedpath.metrics import render as render_metrics
from seedpath.runtime import Runtime, build_runtime

log = get_logger(__name__)


def _authorized(request: Request, settings: Settings) -> bool:
    """Check the Basic credentials *arr was configured with.

    rTorrent behind nginx commonly uses Basic auth; the proxy must accept the
    same credentials and use them upstream. When no rTorrent username is
    configured, or ``INGRESS_AUTH=false``, no check is made.
    """
    if not settings.ingress_auth or not settings.rt_user:
        return True
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    user_ok = secrets.compare_digest(username, settings.rt_user)
    password_ok = secrets.compare_digest(password, settings.rt_secret)
    return user_ok and password_ok


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


async def _read_body(request: Request, limit: int) -> bytes | None:
    """Read at most *limit* bytes, including chunked requests."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


async def rpc_endpoint(request: Request) -> Response:
    runtime = _runtime(request)
    settings = runtime.settings

    if not _authorized(request, settings):
        return Response(
            "authentication required\n",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="seedpath"'},
        )

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            return PlainTextResponse("invalid Content-Length\n", status_code=400)
        if declared_size > settings.max_body_bytes:
            return PlainTextResponse("request body too large\n", status_code=413)

    body = await _read_body(request, settings.max_body_bytes)
    if body is None:
        return PlainTextResponse("request body too large\n", status_code=413)

    decision = runtime.proxy.inspect(body)
    try:
        upstream = await runtime.torrent.forward(decision.body, request.headers.get("content-type"))
    except UpstreamError as exc:
        log.error("proxy.upstream_unavailable", error=str(exc))
        return PlainTextResponse("rTorrent unavailable\n", status_code=502)

    headers = {"content-type": upstream.content_type or "text/xml"}
    response = Response(
        content=upstream.body,
        status_code=upstream.status_code,
        headers=headers,
    )
    # Starlette runs this only after the response has been sent, satisfying the
    # rule that the *arr call is answered before any follow-up work starts.
    response.background = BackgroundTask(runtime.proxy.after_response, decision, upstream)
    return response


async def livez(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok\n")


async def healthz(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    checks: list[dict[str, Any]] = []

    results = await asyncio.gather(
        *[_check_torrent(runtime), *[_check_app(runtime, name) for name in runtime.apps]],
    )
    checks.extend(results)
    healthy = all(check["ok"] for check in checks)
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "version": __version__, "checks": checks},
        status_code=200 if healthy else 503,
    )


async def _check_torrent(runtime: Runtime) -> dict[str, Any]:
    try:
        version = await runtime.torrent.version()
    except Exception as exc:
        return {"name": "rtorrent", "ok": False, "detail": str(exc)}
    return {"name": "rtorrent", "ok": True, "detail": version}


async def _check_app(runtime: Runtime, name: str) -> dict[str, Any]:
    try:
        status = await runtime.apps[name].system_status()
    except Exception as exc:
        return {"name": name, "ok": False, "detail": str(exc)}
    return {"name": name, "ok": True, "detail": str(status.get("version", "ok"))}


async def metrics_endpoint(_: Request) -> Response:
    body, content_type = render_metrics()
    return Response(content=body, headers={"content-type": content_type})


async def index(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "seedpath",
            "version": __version__,
            "endpoints": ["/RPC2", "/healthz", "/livez", "/metrics"],
        }
    )


def create_app(
    settings: Settings,
    *,
    runtime: Runtime | None = None,
    background: bool | None = None,
) -> Starlette:
    """Build the ASGI app.

    ``runtime`` and ``background`` are injection points for tests: real
    deployments let the lifespan build clients and start workers.
    """
    run_background = settings.background_workers if background is None else background

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        active = runtime or await build_runtime(settings)
        app.state.runtime = active
        if run_background:
            await active.worker.start()
        log.info("http.started", host=settings.host, port=settings.port, rpc_path=settings.rpc_path)
        try:
            yield
        finally:
            if run_background:
                await active.worker.stop()
            if runtime is None:
                await active.aclose()

    routes = [
        Route("/", index, methods=["GET"]),
        Route(settings.rpc_path, rpc_endpoint, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/livez", livez, methods=["GET"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
