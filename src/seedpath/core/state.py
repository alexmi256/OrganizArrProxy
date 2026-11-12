"""Durable state.

SQLite (WAL) is a deliberate choice over a JSON file: transitions happen from
several worker tasks, and a crash mid-write must never truncate the map that
tells seedpath which torrents are still owed a retarget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from seedpath.core.models import GrabJob, TorrentRecord, TorrentState
from seedpath.errors import StateError
from seedpath.logging import get_logger
from seedpath.util import utcnow_iso

log = get_logger(__name__)

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
    infohash     TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    category     TEXT,
    release_name TEXT,
    app          TEXT,
    app_item_id  INTEGER,
    dest         TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_torrents_state ON torrents(state);
"""

_COLUMNS = (
    "infohash, state, category, release_name, app, app_item_id, dest, error, created_at, updated_at"
)


def _row_to_record(row: aiosqlite.Row) -> TorrentRecord:
    return TorrentRecord(
        infohash=row["infohash"],
        state=TorrentState(row["state"]),
        category=row["category"],
        release_name=row["release_name"],
        app=row["app"],
        app_item_id=row["app_item_id"],
        dest=row["dest"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class StateStore:
    """Async repository for per-torrent state."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "StateStore.connect() has not been called"
            raise StateError(msg)
        return self._db

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        try:
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            cursor = await self._db.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0]) if row is not None else 0
            if version > _SCHEMA_VERSION:
                msg = (
                    f"state database schema {version} is newer than this build ({_SCHEMA_VERSION})"
                )
                raise StateError(msg)
            await self._db.executescript(_SCHEMA)
            if version < _SCHEMA_VERSION:
                await self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            await self._db.commit()
            log.info("state.opened", path=str(self._path), schema=_SCHEMA_VERSION)
        except Exception:
            await self._db.close()
            self._db = None
            raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def ensure(self, job: GrabJob) -> TorrentRecord:
        """Insert a grab if unknown; never rewind an existing state."""
        async with self._lock:
            now = utcnow_iso()
            await self.connection.execute(
                """
                INSERT INTO torrents
                    (infohash, state, category, release_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(infohash) DO UPDATE SET
                    category = COALESCE(excluded.category, torrents.category),
                    release_name = COALESCE(excluded.release_name, torrents.release_name)
                """,
                (
                    job.infohash,
                    TorrentState.RECEIVED.value,
                    job.category,
                    job.release_name,
                    now,
                    now,
                ),
            )
            await self.connection.commit()
        record = await self.get(job.infohash)
        if record is None:  # pragma: no cover - the INSERT above guarantees it
            msg = f"state row disappeared for {job.infohash}"
            raise StateError(msg)
        return record

    async def restart(self, infohash: str) -> None:
        """Reset a row for a fresh load of the same infohash.

        This is only called after rTorrent accepted a new load, so the previous
        torrent for this hash is gone. Destination and ownership are cleared so
        resolution runs again from scratch.
        """
        async with self._lock:
            cursor = await self.connection.execute(
                """
                UPDATE torrents
                SET state = ?, app = NULL, app_item_id = NULL, dest = NULL,
                    error = NULL, updated_at = ?
                WHERE infohash = ?
                """,
                (TorrentState.RECEIVED.value, utcnow_iso(), infohash),
            )
            if cursor.rowcount == 0:
                log.warning("state.restart_missing", infohash=infohash)
            await self.connection.commit()
        log.info("torrent.restarted", infohash=infohash)

    async def get(self, infohash: str) -> TorrentRecord | None:
        cursor = await self.connection.execute(
            f"SELECT {_COLUMNS} FROM torrents WHERE infohash = ?",  # noqa: S608
            (infohash,),
        )
        row = await cursor.fetchone()
        return _row_to_record(row) if row is not None else None

    async def transition(
        self,
        infohash: str,
        state: TorrentState,
        *,
        error: str | None = None,
        app: str | None = None,
        app_item_id: int | None = None,
        dest: str | None = None,
    ) -> None:
        """Move a torrent to *state*, filling only the fields supplied."""
        async with self._lock:
            fields: list[str] = ["state = ?", "updated_at = ?"]
            values: list[Any] = [state.value, utcnow_iso()]
            for name, value in (("error", error), ("app", app), ("dest", dest)):
                if value is not None:
                    fields.append(f"{name} = ?")
                    values.append(value)
            if app_item_id is not None:
                fields.append("app_item_id = ?")
                values.append(app_item_id)
            values.append(infohash)
            cursor = await self.connection.execute(
                f"UPDATE torrents SET {', '.join(fields)} WHERE infohash = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount == 0:
                log.warning("state.transition_missing", infohash=infohash, state=state.value)
            await self.connection.commit()
        log.info("torrent.state", infohash=infohash, state=state.value)

    async def list_by_states(self, states: Iterable[TorrentState]) -> list[TorrentRecord]:
        values = [state.value for state in states]
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        cursor = await self.connection.execute(
            f"SELECT {_COLUMNS} FROM torrents WHERE state IN ({placeholders}) "  # noqa: S608
            "ORDER BY created_at",
            values,
        )
        return [_row_to_record(row) for row in await cursor.fetchall()]

    async def list_all(self) -> list[TorrentRecord]:
        cursor = await self.connection.execute(f"SELECT {_COLUMNS} FROM torrents")  # noqa: S608
        return [_row_to_record(row) for row in await cursor.fetchall()]

    async def delete(self, infohash: str, *, expected_state: TorrentState | None = None) -> bool:
        """Delete a row, optionally only if it is still in *expected_state*.

        The reconciler uses the state it observed in its snapshot: if a new
        load has restarted the row meanwhile, the delete must be a no-op.
        """
        async with self._lock:
            if expected_state is None:
                cursor = await self.connection.execute(
                    "DELETE FROM torrents WHERE infohash = ?", (infohash,)
                )
            else:
                cursor = await self.connection.execute(
                    "DELETE FROM torrents WHERE infohash = ? AND state = ?",
                    (infohash, expected_state.value),
                )
            await self.connection.commit()
        removed = cursor.rowcount > 0
        if removed:
            log.info("torrent.forgotten", infohash=infohash)
        return removed

    async def prune(self, keep: Iterable[TorrentState]) -> int:
        """Delete terminal rows, returning how many were removed."""
        keep_values = [state.value for state in keep]
        placeholders = ",".join("?" for _ in keep_values) or "''"
        async with self._lock:
            cursor = await self.connection.execute(
                f"DELETE FROM torrents WHERE state NOT IN ({placeholders})",  # noqa: S608
                keep_values,
            )
            await self.connection.commit()
        return cursor.rowcount
