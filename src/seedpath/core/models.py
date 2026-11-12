"""Core data types shared by the proxy, resolver, state store and workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TorrentState(StrEnum):
    """Lifecycle of one grab.

    ``FAILED`` means seedpath could not even identify the torrent in rTorrent;
    ``FAILED_OPEN`` means seedpath gave up and let rTorrent start it in its
    default location. ``DRY_RUN`` is terminal for that run in dry-run mode.
    """

    RECEIVED = "received"
    LOADED = "loaded"
    RESOLVED = "resolved"
    PLACED = "placed"
    STARTED = "started"
    COMPLETE = "complete"
    ADOPTED = "adopted"
    DONE = "done"
    FAILED_OPEN = "failed_open"
    FAILED = "failed"
    DRY_RUN = "dry_run"


#: States that a restarted process should pick up again.
RESUMABLE_STATES: frozenset[TorrentState] = frozenset(
    {
        TorrentState.RECEIVED,
        TorrentState.LOADED,
        TorrentState.RESOLVED,
        TorrentState.PLACED,
    }
)

#: States that still need the completion watcher.
ACTIVE_STATES: frozenset[TorrentState] = frozenset({TorrentState.STARTED, TorrentState.COMPLETE})


@dataclass(frozen=True, slots=True)
class GrabJob:
    """Enough information to place a torrent, captured at load time."""

    infohash: str
    category: str | None
    release_name: str | None
    is_raw: bool
    received_at: datetime


@dataclass(frozen=True, slots=True)
class TorrentRecord:
    """Persisted row for one torrent."""

    infohash: str
    state: TorrentState
    category: str | None
    release_name: str | None
    app: str | None
    app_item_id: int | None
    dest: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    """The *arr item that owns a grab and the folder the release belongs in."""

    app: str
    item_id: int
    kind: str
    path: str
    season_number: int | None = None
    source: str = "history"
