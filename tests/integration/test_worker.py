"""Worker placement, failure handling and completion rescan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from seedpath.core.models import TorrentState
from seedpath.core.resolver import Resolver
from seedpath.core.state import StateStore
from seedpath.core.worker import PlacementWorker
from tests.support import FakeArr, FakeTorrent, make_job, make_settings, make_torrent


class NoSleep:
    async def __call__(self, delay: float) -> None:
        return None


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    state = StateStore(tmp_path / "state.db")
    await state.connect()
    yield state
    await state.close()


def make_worker(
    tmp_path: Path,
    store: StateStore,
    *,
    app: FakeArr | None = None,
    torrent: FakeTorrent | None = None,
    **overrides: object,
) -> tuple[PlacementWorker, FakeTorrent, FakeArr]:
    settings = make_settings(tmp_path, **overrides)
    app = app or FakeArr()
    torrent = torrent or FakeTorrent()
    resolver = Resolver(
        {app.name: app},
        settings.configured_category_map,
        settings.library_roots,
        resolve_timeout=settings.resolve_timeout,
    )
    worker = PlacementWorker(
        settings,
        store,
        torrent,
        resolver,
        {app.name: app},
        sleep=NoSleep(),
    )
    return worker, torrent, app


def _calls(torrent: FakeTorrent) -> list[str]:
    return [method for method, _ in torrent.calls]


async def test_happy_path_places_then_starts(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED
    assert record.dest == "/data/movies/Big Buck Bunny (2008)"
    assert _calls(torrent) == [
        "d.name",
        "d.state",
        "d.is_active",
        "d.directory.set",
        "d.start",
    ]
    assert app.rescans == []


async def test_completion_rescans_and_finishes(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)

    torrent.complete = True
    await worker.check_completions()

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.DONE
    assert app.rescans == [(1, "/data/movies/Big Buck Bunny (2008)")]


async def test_resolution_failure_fails_open(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, _ = make_worker(tmp_path, store)
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN
    assert "d.start" in _calls(torrent)


async def test_dry_run_never_retargets_or_starts(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store, dry_run=True)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.DRY_RUN
    assert _calls(torrent) == ["d.name"]


async def test_destination_outside_roots_fails_open(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/mnt/elsewhere/Movie"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN
    assert "d.start" in _calls(torrent)


async def test_missing_hash_marks_failed(tmp_path: Path, store: StateStore) -> None:
    torrent = FakeTorrent()
    torrent.hash_present = False
    worker, _, _ = make_worker(tmp_path, store, torrent=torrent, load_timeout=0)
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED
    assert "d.start" not in _calls(torrent)


async def test_set_directory_fault_recovers_via_rescan(tmp_path: Path, store: StateStore) -> None:
    torrent = FakeTorrent()
    torrent.set_directory_faults = 1
    worker, _, app = make_worker(tmp_path, store, torrent=torrent)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED
    assert app.rescans == [(1, "/data/movies/Big Buck Bunny (2008)")]


async def test_set_directory_fault_twice_fails_open(tmp_path: Path, store: StateStore) -> None:
    torrent = FakeTorrent()
    torrent.set_directory_faults = 2
    worker, _, app = make_worker(tmp_path, store, torrent=torrent)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN
    assert len(app.rescans) == 1


async def test_processing_is_idempotent(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    before = len(torrent.calls)

    await worker.process(job)

    added = [method for method, _ in torrent.calls[before:]]
    assert "d.directory.set" not in added
    assert "d.start" not in added


async def test_regrab_after_done_is_placed_again(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    torrent.complete = True
    await worker.check_completions()
    completed = await store.get(job.infohash)
    assert completed is not None
    assert completed.state is TorrentState.DONE

    # The old torrent was removed and the same hash was grabbed again: the new
    # one is paused, so seedpath must place it instead of trusting the DONE row.
    torrent.started = False
    torrent.complete = False
    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED
    assert record.dest == "/data/movies/Big Buck Bunny (2008)"
    assert _calls(torrent).count("d.directory.set") == 2


async def test_done_row_is_not_reset_while_the_torrent_is_still_seeding(
    tmp_path: Path, store: StateStore
) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    await store.transition(job.infohash, TorrentState.DONE)
    assert torrent.started is True
    before = len(torrent.calls)

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.DONE
    added = [method for method, _ in torrent.calls[before:]]
    assert "d.directory.set" not in added
    assert "d.start" not in added


async def test_resumed_row_never_retargets_a_started_torrent(
    tmp_path: Path, store: StateStore
) -> None:
    worker, torrent, _ = make_worker(tmp_path, store)
    job = make_job(make_torrent())
    await store.ensure(job)
    await store.transition(
        job.infohash,
        TorrentState.RESOLVED,
        app="radarr",
        app_item_id=1,
        dest="/data/movies/Big Buck Bunny (2008)",
    )
    torrent.started = True

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN
    assert "d.directory.set" not in _calls(torrent)


async def test_transient_state_fault_is_retried(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    torrent.state_faults = 1

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED
    assert _calls(torrent).count("d.state") >= 2


async def test_transient_unknown_state_does_not_reset_a_done_row(
    tmp_path: Path, store: StateStore
) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    await store.transition(job.infohash, TorrentState.DONE)
    torrent.started = True
    torrent.state_faults = 3
    before = len(torrent.calls)

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.DONE
    added = [method for method, _ in torrent.calls[before:]]
    assert "d.directory.set" not in added
    assert "d.start" not in added


async def test_persistent_unknown_state_on_regrab_fails_open(
    tmp_path: Path, store: StateStore
) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    await store.transition(job.infohash, TorrentState.DONE)
    torrent.started = True
    torrent.state_faults = 6
    before = len(torrent.calls)

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN
    added = [method for method, _ in torrent.calls[before:]]
    assert "d.directory.set" not in added
    assert "d.start" in added


async def test_unknown_state_does_not_retarget_a_resolved_row(
    tmp_path: Path, store: StateStore
) -> None:
    worker, torrent, _ = make_worker(tmp_path, store)
    job = make_job(make_torrent())
    await store.ensure(job)
    await store.transition(
        job.infohash,
        TorrentState.RESOLVED,
        app="radarr",
        app_item_id=1,
        dest="/data/movies/Big Buck Bunny (2008)",
    )
    torrent.state_faults = 3

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN
    assert "d.directory.set" not in _calls(torrent)


async def test_reconcile_places_dry_run_rows_once_live(tmp_path: Path, store: StateStore) -> None:
    dry_worker, torrent, app = make_worker(tmp_path, store, dry_run=True)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await dry_worker.process(job)
    dry_record = await store.get(job.infohash)
    assert dry_record is not None
    assert dry_record.state is TorrentState.DRY_RUN
    torrent.torrents = [
        {
            "hash": job.infohash,
            "custom1": "radarr",
            "state": 1,
            "is_active": 0,
            "complete": 0,
        }
    ]

    live_worker, _, _ = make_worker(tmp_path, store, torrent=torrent, app=app)
    await live_worker.reconcile()

    assert job.infohash in live_worker._inflight
    await live_worker.process(job)
    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED


async def test_torrent_erased_drops_state(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    torrent.hash_present = False

    await worker.check_completions()

    assert await store.get(job.infohash) is None


async def test_transient_completion_fault_keeps_state(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, app = make_worker(tmp_path, store)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())
    await worker.process(job)
    torrent.complete_fault = True

    await worker.check_completions()

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED


async def test_reconcile_resumes_and_sweeps(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, _ = make_worker(tmp_path, store)
    fixture = make_torrent()
    resumed = make_job(fixture)
    await store.ensure(resumed)
    await store.transition(resumed.infohash, TorrentState.PLACED, dest="/data/movies/X")

    swept = "b" * 40
    completed = "c" * 40
    closed = "d" * 40
    torrent.torrents = [
        {
            "hash": resumed.infohash,
            "custom1": "radarr",
            "state": 1,
            "is_active": 0,
            "complete": 0,
        },
        {
            "hash": swept,
            "name": "Swept.Release",
            "custom1": "radarr",
            "state": 1,
            "is_active": 0,
            "complete": 0,
        },
        {
            "hash": completed,
            "custom1": "radarr",
            "state": 1,
            "is_active": 1,
            "complete": 1,
        },
        {
            "hash": closed,
            "custom1": "radarr",
            "state": 0,
            "is_active": 0,
            "complete": 0,
        },
    ]

    await worker.reconcile()

    assert worker._inflight == {resumed.infohash, swept}
    assert worker._queue.qsize() == 2


async def test_reconcile_does_not_delete_a_row_restarted_after_its_snapshot(
    tmp_path: Path, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, torrent, _ = make_worker(tmp_path, store)
    job = make_job(make_torrent())
    await store.ensure(job)
    await store.transition(job.infohash, TorrentState.DONE)
    original_list_all = store.list_all

    async def stale_list_all():
        rows = await original_list_all()
        # Simulate a new load restarting the row after reconcile took its
        # snapshot but before it applies the delete.
        await store.restart(job.infohash)
        await store.ensure(job)
        return rows

    monkeypatch.setattr(store, "list_all", stale_list_all)
    torrent.torrents = []

    await worker.reconcile()

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.RECEIVED


async def test_reconcile_drops_removed_torrents(tmp_path: Path, store: StateStore) -> None:
    worker, torrent, _ = make_worker(tmp_path, store)
    job = make_job(make_torrent())
    await store.ensure(job)
    await store.transition(job.infohash, TorrentState.DONE)
    torrent.torrents = []

    await worker.reconcile()

    assert await store.get(job.infohash) is None


async def test_start_fault_fails_open(tmp_path: Path, store: StateStore) -> None:
    torrent = FakeTorrent()
    torrent.start_faults = 2
    worker, _, app = make_worker(tmp_path, store, torrent=torrent)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.FAILED_OPEN


async def test_start_fault_recovers_after_rescan(tmp_path: Path, store: StateStore) -> None:
    torrent = FakeTorrent()
    torrent.start_faults = 1
    worker, _, app = make_worker(tmp_path, store, torrent=torrent)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.process(job)

    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.STARTED
    assert app.rescans == [(1, "/data/movies/Big Buck Bunny (2008)")]


async def test_worker_lifecycle_processes_the_queue(tmp_path: Path, store: StateStore) -> None:
    import asyncio

    worker, _, app = make_worker(tmp_path, store, worker_count=1)
    app.history = {"movieId": 1}
    app.item = {"id": 1, "path": "/data/movies/Big Buck Bunny (2008)"}
    job = make_job(make_torrent())

    await worker.start()
    try:
        await worker.enqueue(job)
        state = None
        for _ in range(100):
            record = await store.get(job.infohash)
            state = record.state if record is not None else None
            if state is TorrentState.STARTED:
                break
            await asyncio.sleep(0.005)
        assert state is TorrentState.STARTED
    finally:
        await worker.stop()
