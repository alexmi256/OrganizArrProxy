"""Placement workers and the completion watcher.

The HTTP request thread must never wait on an *arr call: it forwards the load,
answers the app, and hands the infohash to this module. Everything after that
is retried, persisted and recoverable.
"""

from __future__ import annotations

import asyncio
import contextlib
import xmlrpc.client
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog

from seedpath.clients.ports import ArrClientProtocol, TorrentClient
from seedpath.config import Settings
from seedpath.core.models import (
    ACTIVE_STATES,
    RESUMABLE_STATES,
    GrabJob,
    ResolvedItem,
    TorrentRecord,
    TorrentState,
)
from seedpath.core.resolver import Resolver
from seedpath.core.state import StateStore
from seedpath.errors import ArrError, ResolutionError, UpstreamError
from seedpath.logging import get_logger
from seedpath.metrics import (
    ACTIVE_TORRENTS,
    PLACEMENTS,
    QUEUE_DEPTH,
    RESCANS,
    RESOLVE_FAILURES,
    RETARGET_SECONDS,
)
from seedpath.util import path_within_roots, utcnow

log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class PlacementWorker:
    """Queue of grabs plus the periodic rescan/reconcile loops."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        torrent: TorrentClient,
        resolver: Resolver,
        apps: Mapping[str, ArrClientProtocol],
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._store = store
        self._torrent = torrent
        self._resolver = resolver
        self._apps = dict(apps)
        self._sleep = sleep
        self._queue: asyncio.Queue[GrabJob] = asyncio.Queue(maxsize=settings.worker_queue_size)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._settings.worker_count):
            self._tasks.append(asyncio.create_task(self._run_worker(index), name=f"worker-{index}"))
        self._tasks.append(asyncio.create_task(self._watch_completions(), name="completions"))
        self._tasks.append(asyncio.create_task(self._reconcile_loop(), name="reconciler"))
        log.info(
            "workers.started",
            workers=self._settings.worker_count,
            dry_run=self._settings.dry_run,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False
        log.info("workers.stopped")

    async def enqueue(self, job: GrabJob) -> None:
        """Queue a grab without blocking the caller for long."""
        async with self._inflight_lock:
            if job.infohash in self._inflight:
                log.debug("worker.already_queued", infohash=job.infohash)
                return
            self._inflight.add(job.infohash)
        try:
            await asyncio.wait_for(self._queue.put(job), timeout=1.0)
        except TimeoutError:
            async with self._inflight_lock:
                self._inflight.discard(job.infohash)
            log.error("worker.queue_full", infohash=job.infohash)
            await self._fail_open(job, "placement queue is full")
            return
        QUEUE_DEPTH.set(self._queue.qsize())

    # ------------------------------------------------------------------ worker loop
    async def _run_worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            structlog.contextvars.bind_contextvars(infohash=job.infohash, category=job.category)
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the worker alive no matter what
                log.exception("worker.unhandled", worker=index)
                await self._fail_open(job, f"unhandled worker error: {exc}")
            finally:
                async with self._inflight_lock:
                    self._inflight.discard(job.infohash)
                self._queue.task_done()
                QUEUE_DEPTH.set(self._queue.qsize())
                structlog.contextvars.clear_contextvars()

    async def process(self, job: GrabJob) -> None:
        """Public single-job entry point, used by tests and recovery."""
        await self._process(job)

    async def _process(self, job: GrabJob) -> None:
        record = await self._store.ensure(job)
        if record.state not in RESUMABLE_STATES:
            # The only source of a non-resumable job is a *new* successful
            # load (reconcile only enqueues resumable rows). A re-grab of an
            # infohash with an old DONE/FAILED/DRY_RUN row, or one whose row
            # survived a d.erase, must be placed again rather than ignored.
            if record.state == TorrentState.DRY_RUN and self._settings.dry_run:
                return
            running_states = {
                TorrentState.STARTED,
                TorrentState.COMPLETE,
                TorrentState.ADOPTED,
                TorrentState.DONE,
                TorrentState.FAILED_OPEN,
            }
            if record.state in running_states:
                running = await self._torrent_is_started(job.infohash)
                if running is None and await self._wait_for_hash(job.infohash):
                    # A slow registration can make the state query fault while
                    # the load itself succeeded; try once the hash is visible.
                    running = await self._torrent_is_started(job.infohash)
                if running is None:
                    await self._fail_open(
                        job, "could not determine whether the torrent is already running"
                    )
                    return
                if running:
                    log.error("placement.duplicate_running")
                    return
            await self._store.restart(job.infohash)
            record = await self._store.ensure(job)

        if record.state in {TorrentState.RECEIVED, TorrentState.LOADED}:
            if not await self._wait_for_hash(job.infohash):
                await self._store.transition(
                    job.infohash,
                    TorrentState.FAILED,
                    error=(
                        "torrent did not appear in rTorrent within "
                        f"{self._settings.load_timeout:g}s"
                    ),
                )
                PLACEMENTS.labels(app=record.app or "unknown", outcome="failed").inc()
                log.error("placement.hash_missing", timeout=self._settings.load_timeout)
                return
            await self._store.transition(job.infohash, TorrentState.LOADED)
            record = await self._reload(job.infohash)

        if record.dest is None:
            resolved = await self._resolve(job)
            if resolved is None:
                return
            await self._store.transition(
                job.infohash,
                TorrentState.RESOLVED,
                app=resolved.app,
                app_item_id=resolved.item_id,
                dest=resolved.path,
                error="",
            )
            record = await self._reload(job.infohash)

        if record.dest is None:  # pragma: no cover - resolution always sets it
            await self._fail_open(job, "resolution produced no destination")
            return
        if not path_within_roots(record.dest, self._settings.library_roots):
            await self._fail_open(job, f"destination {record.dest!r} is outside LIBRARY_ROOTS")
            return

        if self._settings.dry_run:
            log.info(
                "placement.dry_run",
                app=record.app,
                dest=record.dest,
                app_item_id=record.app_item_id,
            )
            await self._store.transition(job.infohash, TorrentState.DRY_RUN)
            PLACEMENTS.labels(app=record.app or "unknown", outcome="dry_run").inc()
            return

        if record.state in {TorrentState.RESOLVED, TorrentState.PLACED}:
            if record.state == TorrentState.RESOLVED:
                running = await self._torrent_is_started(job.infohash)
                if running is None:
                    await self._fail_open(job, "could not verify torrent is stopped")
                    return
                if running:
                    # An operator (or a crash after d.start) started it between
                    # resolution and placement. Never retarget a running torrent.
                    log.error("placement.already_started")
                    await self._store.transition(
                        job.infohash,
                        TorrentState.FAILED_OPEN,
                        error="torrent already started; refusing to retarget",
                    )
                    PLACEMENTS.labels(app=record.app or "unknown", outcome="fail_open").inc()
                    return
                if not await self._set_directory_with_recovery(record):
                    await self._fail_open(job, "rTorrent refused d.directory.set")
                    return
                await self._store.transition(job.infohash, TorrentState.PLACED)
                record = await self._reload(job.infohash)

            if not await self._start_with_recovery(record):
                await self._fail_open(job, "rTorrent refused d.start")
                return
            await self._store.transition(job.infohash, TorrentState.STARTED)
            elapsed = max((utcnow() - job.received_at).total_seconds(), 0.0)
            RETARGET_SECONDS.labels(app=record.app or "unknown").observe(elapsed)
            PLACEMENTS.labels(app=record.app or "unknown", outcome="placed").inc()
            log.info("placement.started", app=record.app, dest=record.dest)

    # ------------------------------------------------------------------ steps
    async def _torrent_is_started(self, infohash: str) -> bool | None:
        """Return None when rTorrent cannot answer after a short retry."""
        delay = 0.1
        for attempt in (0, 1, 2):
            try:
                return await self._torrent.is_started(infohash)
            except (xmlrpc.client.Fault, UpstreamError) as exc:
                log.warning(
                    "placement.state_check_failed",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt == 2:
                    return None
                await self._sleep(delay)
                delay *= 2
        return None

    async def _reload(self, infohash: str) -> TorrentRecord:
        record = await self._store.get(infohash)
        if record is None:  # pragma: no cover - row exists by construction
            msg = f"state row disappeared for {infohash}"
            raise RuntimeError(msg)
        return record

    async def _wait_for_hash(self, infohash: str) -> bool:
        deadline = asyncio.get_running_loop().time() + self._settings.load_timeout
        delay = 0.1
        while True:
            try:
                if await self._torrent.hash_exists(infohash):
                    return True
            except UpstreamError as exc:
                log.warning("placement.rtorrent_unreachable", error=str(exc))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await self._sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)

    async def _resolve(self, job: GrabJob) -> ResolvedItem | None:
        try:
            return await self._resolver.resolve(job)
        except ResolutionError as exc:
            log.error("placement.resolve_failed", error=str(exc))
            RESOLVE_FAILURES.labels(app=job.category or "unknown").inc()
            await self._fail_open(job, str(exc))
            return None

    async def _set_directory_with_recovery(self, record: TorrentRecord) -> bool:
        if record.dest is None:  # pragma: no cover - guarded by the caller
            return False
        for attempt in (0, 1):
            try:
                await self._torrent.set_directory(record.infohash, record.dest)
                return True
            except (xmlrpc.client.Fault, UpstreamError) as exc:
                log.warning(
                    "placement.set_directory_failed",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt == 1:
                    return False
                # The parent may not exist yet: an *arr scan creates it when
                # "create empty folders" is on. Give it a moment, then retry.
                app = self._apps.get(record.app or "")
                if app is not None and record.app_item_id is not None:
                    try:
                        await app.rescan(record.app_item_id, record.dest)
                    except ArrError as scan_exc:
                        log.warning("placement.ensure_folder_failed", error=str(scan_exc))
                await self._sleep(1.0)
        return False

    async def _start_with_recovery(self, record: TorrentRecord) -> bool:
        """Start, and on failure try the app's scan once before giving up.

        A missing destination folder may only surface at ``d.start``: rTorrent
        accepts ``d.directory.set`` for a path whose parent does not exist and
        then fails to create the release folder. The scan command is what
        creates it when "create empty folders" is enabled.
        """
        try:
            await self._torrent.start(record.infohash)
            return True
        except (xmlrpc.client.Fault, UpstreamError) as exc:
            log.warning("placement.start_failed", error=str(exc))
            app = self._apps.get(record.app or "")
            if app is None or record.app_item_id is None or record.dest is None:
                return False
            try:
                await app.rescan(record.app_item_id, record.dest)
            except ArrError as scan_exc:
                log.warning("placement.ensure_folder_failed", error=str(scan_exc))
                return False
            await self._sleep(1.0)
            try:
                await self._torrent.start(record.infohash)
            except (xmlrpc.client.Fault, UpstreamError) as retry_exc:
                log.warning("placement.start_retry_failed", error=str(retry_exc))
                return False
            return True

    async def _fail_open(self, job: GrabJob, reason: str) -> None:
        """Never leave a torrent paused with no owner."""
        log.error("placement.fail_open", error=reason)
        await self._store.ensure(job)
        try:
            await self._torrent.start(job.infohash)
        except (xmlrpc.client.Fault, UpstreamError) as exc:
            log.error("placement.fail_open_start_failed", error=str(exc))
        await self._store.transition(job.infohash, TorrentState.FAILED_OPEN, error=reason)
        PLACEMENTS.labels(app=job.category or "unknown", outcome="fail_open").inc()

    # ------------------------------------------------------------------ completion
    async def _watch_completions(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.check_completions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("completions.check_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._settings.rescan_poll)

    async def check_completions(self) -> None:
        records = await self._store.list_by_states(ACTIVE_STATES)
        ACTIVE_TORRENTS.set(len(records))
        for record in records:
            structlog.contextvars.bind_contextvars(infohash=record.infohash, app=record.app)
            try:
                await self._check_one(record)
            finally:
                structlog.contextvars.clear_contextvars()

    async def _check_one(self, record: TorrentRecord) -> None:
        try:
            complete = await self._torrent.is_complete(record.infohash)
        except xmlrpc.client.Fault:
            # A fault can also be a transient rTorrent restart. Only forget the
            # row when the torrent is genuinely gone, otherwise the rescan
            # could be lost and the file would wait for the 24 h refresh.
            try:
                still_there = await self._torrent.hash_exists(record.infohash)
            except (xmlrpc.client.Fault, UpstreamError):
                return
            if still_there:
                log.warning("completions.transient_fault")
                return
            log.warning("completions.torrent_missing")
            await self._store.delete(record.infohash)
            return
        except UpstreamError as exc:
            log.warning("completions.rtorrent_unreachable", error=str(exc))
            return

        if record.state == TorrentState.STARTED:
            if not complete:
                return
            await self._store.transition(record.infohash, TorrentState.COMPLETE)
            record = await self._reload(record.infohash)

        if not complete:
            return
        app = self._apps.get(record.app or "")
        if app is None or record.app_item_id is None or record.dest is None:
            log.error("completions.missing_resolution")
            await self._store.transition(
                record.infohash,
                TorrentState.ADOPTED,
                error="cannot rescan: app or destination missing",
            )
            return
        try:
            await app.rescan(record.app_item_id, record.dest)
        except ArrError as exc:
            RESCANS.labels(app=app.name, outcome="error").inc()
            log.error("completions.rescan_failed", error=str(exc), body=exc.body)
            return
        RESCANS.labels(app=app.name, outcome="ok").inc()
        await self._store.transition(record.infohash, TorrentState.ADOPTED)
        await self._store.transition(record.infohash, TorrentState.DONE)

    # ------------------------------------------------------------------ reconcile
    async def _reconcile_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile.failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.reconcile_interval
                )

    async def reconcile(self) -> None:
        """Resume interrupted work and adopt paused, unknown torrents.

        rTorrent distinguishes open/inactive (paused, ``state=1`` and
        ``is_active=0``) from open/active (started or seeding). Only paused,
        incomplete torrents are swept; a started or seeding torrent is never
        retargeted. Closed items (``state=0``) are left alone because they may
        have been stopped intentionally.
        """
        states = set(RESUMABLE_STATES)
        if not self._settings.dry_run:
            # A dry-run row left the torrent paused. Once DRY_RUN is turned
            # off, reconcile must place it rather than leave it stranded.
            states.add(TorrentState.DRY_RUN)
        for record in await self._store.list_by_states(states):
            await self.enqueue(
                GrabJob(
                    infohash=record.infohash,
                    category=record.category,
                    release_name=record.release_name,
                    is_raw=False,
                    received_at=record.created_at,
                )
            )

        try:
            torrents = await self._torrent.list_torrents()
        except (UpstreamError, xmlrpc.client.Fault) as exc:
            log.warning("reconcile.rtorrent_unavailable", error=str(exc))
            return

        present = {str(torrent.get("hash")) for torrent in torrents}
        # Only forget torrents that had already started or finished. A resumable
        # row may simply not be visible in rTorrent yet (the load response can
        # race the next reconcile), and deleting it would strand a paused grab.
        droppable = {
            TorrentState.STARTED,
            TorrentState.COMPLETE,
            TorrentState.ADOPTED,
            TorrentState.DONE,
            TorrentState.FAILED_OPEN,
            TorrentState.FAILED,
            TorrentState.DRY_RUN,
        }
        for record in await self._store.list_all():
            if record.infohash not in present and record.state in droppable:
                removed = await self._store.delete(record.infohash, expected_state=record.state)
                if removed:
                    log.info("reconcile.drop_removed_torrent", state=record.state.value)

        category_map = self._settings.configured_category_map
        for torrent in torrents:
            infohash = str(torrent.get("hash") or "")
            category = torrent.get("custom1")
            if not infohash or not isinstance(category, str) or category not in category_map:
                continue
            # Paused items are open but inactive; started/seeding items are
            # active. This is the crash signature from BRIEF.md §11.
            if _as_int(torrent.get("state")) != 1:
                continue
            if _as_int(torrent.get("is_active")) != 0:
                continue
            if _as_int(torrent.get("complete")) not in {0, None}:
                continue
            existing = await self._store.get(infohash)
            if existing is not None:
                continue
            log.warning("reconcile.sweep_stopped_torrent", category=category)
            await self.enqueue(
                GrabJob(
                    infohash=infohash,
                    category=category,
                    release_name=str(torrent.get("name") or "") or None,
                    is_raw=False,
                    received_at=utcnow(),
                )
            )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
