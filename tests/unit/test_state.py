"""State store behaviour, including restart persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from seedpath.core.models import GrabJob, TorrentState
from seedpath.core.state import StateStore
from seedpath.util import utcnow


def _job(infohash: str = "a" * 40) -> GrabJob:
    return GrabJob(
        infohash=infohash,
        category="radarr",
        release_name="Big.Buck.Bunny",
        is_raw=True,
        received_at=utcnow(),
    )


@pytest.fixture
async def store(tmp_path: Path):
    state = StateStore(tmp_path / "state.db")
    await state.connect()
    yield state
    await state.close()


async def test_insert_and_read(store: StateStore) -> None:
    job = _job()
    record = await store.ensure(job)
    assert record.state is TorrentState.RECEIVED
    assert record.category == "radarr"
    assert record.release_name == "Big.Buck.Bunny"
    assert await store.get("unknown") is None


async def test_ensure_preserves_existing_state(store: StateStore) -> None:
    job = _job()
    await store.ensure(job)
    await store.transition(job.infohash, TorrentState.STARTED)
    again = await store.ensure(job)
    assert again.state is TorrentState.STARTED


async def test_transition_fields(store: StateStore) -> None:
    job = _job()
    await store.ensure(job)
    await store.transition(
        job.infohash,
        TorrentState.RESOLVED,
        app="radarr",
        app_item_id=7,
        dest="/data/movies/Movie",
        error="",
    )
    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.RESOLVED
    assert record.app == "radarr"
    assert record.app_item_id == 7
    assert record.dest == "/data/movies/Movie"


async def test_list_and_delete(store: StateStore) -> None:
    first = _job("a" * 40)
    second = _job("b" * 40)
    await store.ensure(first)
    await store.ensure(second)
    await store.transition(first.infohash, TorrentState.STARTED)
    active = await store.list_by_states([TorrentState.STARTED])
    assert [record.infohash for record in active] == [first.infohash]
    assert len(await store.list_all()) == 2
    await store.delete(first.infohash)
    assert await store.get(first.infohash) is None


async def test_conditional_delete_ignores_a_changed_row(store: StateStore) -> None:
    job = _job()
    await store.ensure(job)
    await store.transition(job.infohash, TorrentState.DONE)

    assert await store.delete(job.infohash, expected_state=TorrentState.RECEIVED) is False
    record = await store.get(job.infohash)
    assert record is not None
    assert record.state is TorrentState.DONE

    assert await store.delete(job.infohash, expected_state=TorrentState.DONE) is True
    assert await store.get(job.infohash) is None


async def test_persistence_across_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    job = _job()
    first = StateStore(path)
    await first.connect()
    await first.ensure(job)
    await first.transition(job.infohash, TorrentState.PLACED, dest="/data/movies/Movie")
    await first.close()

    second = StateStore(path)
    await second.connect()
    record = await second.get(job.infohash)
    await second.close()
    assert record is not None
    assert record.state is TorrentState.PLACED
    assert record.dest == "/data/movies/Movie"


async def test_prune_removes_terminal_states(store: StateStore) -> None:
    done = _job("c" * 40)
    running = _job("d" * 40)
    await store.ensure(done)
    await store.ensure(running)
    await store.transition(done.infohash, TorrentState.DONE)
    await store.transition(running.infohash, TorrentState.STARTED)
    removed = await store.prune(keep=[TorrentState.STARTED])
    assert removed == 1
    assert await store.get(done.infohash) is None
    assert await store.get(running.infohash) is not None


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    import aiosqlite

    path = tmp_path / "state.db"
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA user_version=99")
    await db.commit()
    await db.close()
    with pytest.raises(Exception, match="newer"):
        await StateStore(path).connect()
