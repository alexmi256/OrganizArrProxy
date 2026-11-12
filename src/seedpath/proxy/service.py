"""Ingress service: decide what to forward, then enqueue after answering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from seedpath.clients.ports import RpcResponse
from seedpath.config import Settings
from seedpath.core.models import GrabJob
from seedpath.errors import RpcParseError, RpcRewriteError
from seedpath.logging import get_logger
from seedpath.metrics import GRABS
from seedpath.proxy.xmlrpc import (
    LOAD_METHODS,
    PAUSE_MAP,
    LoadPayload,
    analyze_load,
    parse_request,
    response_is_fault,
    rewrite_method,
)
from seedpath.util import utcnow

log = get_logger(__name__)


class JobSink(Protocol):
    """Anything that can accept a placement job (the worker, in practice)."""

    async def enqueue(self, job: GrabJob) -> None: ...


@dataclass(frozen=True, slots=True)
class ForwardDecision:
    """The bytes to send upstream plus the follow-up work they imply."""

    body: bytes
    method: str | None
    job: GrabJob | None = None
    rewritten: bool = False


class ProxyService:
    """Stateless decision logic; persistence happens in the worker."""

    def __init__(
        self,
        settings: Settings,
        worker: JobSink,
    ) -> None:
        self._settings = settings
        self._worker = worker

    def inspect(self, body: bytes) -> ForwardDecision:
        """Parse just enough to retarget ``load.*`` calls.

        Anything unparseable, unknown, or missing a usable category is
        forwarded untouched. In particular seedpath never pauses a torrent it
        cannot later identify and place.
        """
        try:
            method, params = parse_request(body)
        except RpcParseError as exc:
            log.debug("proxy.forward_unparsed", error=str(exc), size=len(body))
            return ForwardDecision(body=body, method=None)

        if method not in LOAD_METHODS:
            return ForwardDecision(body=body, method=method)

        try:
            payload = analyze_load(method, params)
        except Exception as exc:
            log.warning("proxy.load_unidentifiable", method=method, error=str(exc))
            return ForwardDecision(body=body, method=method)

        if payload.category is None:
            log.warning("proxy.load_without_category", method=method, infohash=payload.infohash)
            return ForwardDecision(body=body, method=method)

        if self._settings.app_for_category(payload.category) is None:
            log.warning(
                "proxy.category_unmapped",
                category=payload.category,
                infohash=payload.infohash,
            )
            return ForwardDecision(body=body, method=method)

        forward_body = body
        rewritten = False
        paused_method = PAUSE_MAP.get(method)
        if paused_method is not None:
            try:
                forward_body = rewrite_method(body, paused_method)
            except RpcRewriteError as exc:
                # Preserve the request rather than returning a 500 to arr.
                # It will start in the client's directory; no worker runs.
                log.error("proxy.rewrite_failed", method=method, error=str(exc))
                return ForwardDecision(body=body, method=method)
            rewritten = True

        job = GrabJob(
            infohash=payload.infohash,
            category=payload.category,
            release_name=payload.release_name,
            is_raw=payload.is_raw,
            received_at=utcnow(),
        )
        structlog.contextvars.bind_contextvars(infohash=payload.infohash, category=payload.category)
        log.info(
            "proxy.load_accepted",
            method=method,
            paused_as=paused_method,
            is_magnet=payload.is_magnet,
            release=payload.release_name,
        )
        structlog.contextvars.clear_contextvars()
        return ForwardDecision(
            body=forward_body,
            method=method,
            job=job,
            rewritten=rewritten,
        )

    async def after_response(self, decision: ForwardDecision, response: RpcResponse) -> None:
        """Run after the HTTP response to the *arr app has been sent."""
        if decision.job is None:
            return
        if response.status_code != 200:
            log.error(
                "proxy.upstream_error",
                status=response.status_code,
                infohash=decision.job.infohash,
            )
            return
        if response_is_fault(response.body):
            log.info(
                "proxy.upstream_fault_passthrough",
                infohash=decision.job.infohash,
                release=decision.job.release_name,
            )
            return
        GRABS.labels(category=decision.job.category or "").inc()
        await self._worker.enqueue(decision.job)


__all__ = ["ForwardDecision", "LoadPayload", "ProxyService"]
