"""Destination resolution: history first, parse fallback, root checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from seedpath.core.resolver import Resolver, compose_destination
from seedpath.errors import ResolutionError
from tests.support import FakeArr, make_job, make_torrent


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def _resolver(
    *apps: FakeArr,
    roots: list[str] | None = None,
    timeout: float = 0.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> Resolver:
    mapping = {app.name: app for app in apps or (FakeArr(),)}
    return Resolver(
        mapping,
        {"radarr": "radarr", "tv-sonarr": "sonarr"},
        roots if roots is not None else ["/data/movies", "/data/tv"],
        resolve_timeout=timeout,
        sleep=sleep or RecordingSleep(),
    )


async def test_history_resolution() -> None:
    app = FakeArr()
    app.history = {"movieId": 12}
    app.item = {"id": 12, "path": "/data/movies/Big Buck Bunny (2008)"}
    resolved = await _resolver(app).resolve(make_job(make_torrent()))
    assert resolved.app == "radarr"
    assert resolved.item_id == 12
    assert resolved.path == "/data/movies/Big Buck Bunny (2008)"
    assert resolved.source == "history"


async def test_parse_fallback_when_history_is_empty() -> None:
    app = FakeArr()
    app.parse = {
        "movie": {"id": 9, "path": "/data/movies/Another Movie (2020)"},
        "parsedMovieInfo": {},
    }
    resolved = await _resolver(app).resolve(make_job(make_torrent()))
    assert resolved.item_id == 9
    assert resolved.path == "/data/movies/Another Movie (2020)"
    assert resolved.source == "parse"


async def test_parse_fallback_fetches_item_by_id() -> None:
    app = FakeArr()
    app.parse = {"movieId": 9}
    app.item = {"id": 9, "path": "/data/movies/Another Movie (2020)"}
    resolved = await _resolver(app).resolve(make_job(make_torrent()))
    assert resolved.path == "/data/movies/Another Movie (2020)"


async def test_history_retries_before_falling_back() -> None:
    sleep = RecordingSleep()
    app = FakeArr()
    app.parse = {"movie": {"id": 9, "path": "/data/movies/Another Movie (2020)"}}
    resolved = await _resolver(app, timeout=0.05, sleep=sleep).resolve(make_job(make_torrent()))
    assert resolved.source == "parse"
    assert app.find_calls > 1
    assert sleep.calls


async def test_sonarr_season_folder() -> None:
    app = FakeArr(name="sonarr", kind="sonarr")
    app.history = {"seriesId": 4, "seasonNumber": 3}
    app.item = {"id": 4, "path": "/data/tv/Some Show", "seasonFolder": True}
    job = make_job(make_torrent(), category="tv-sonarr")
    resolved = await _resolver(app).resolve(job)
    assert resolved.path == "/data/tv/Some Show/Season 03"
    assert resolved.season_number == 3


async def test_sonarr_season_from_episode_when_history_lacks_it() -> None:
    app = FakeArr(name="sonarr", kind="sonarr")
    app.history = {"seriesId": 4, "episodeId": 77}
    app.item = {"id": 4, "path": "/data/tv/Some Show", "seasonFolder": True}
    app.episode = {"seasonNumber": 7}
    job = make_job(make_torrent(), category="tv-sonarr")
    resolved = await _resolver(app).resolve(job)
    assert resolved.path == "/data/tv/Some Show/Season 07"


async def test_sonarr_without_season_folders() -> None:
    app = FakeArr(name="sonarr", kind="sonarr")
    app.history = {"seriesId": 4, "seasonNumber": 3}
    app.item = {"id": 4, "path": "/data/tv/Some Show", "seasonFolder": False}
    job = make_job(make_torrent(), category="tv-sonarr")
    resolved = await _resolver(app).resolve(job)
    assert resolved.path == "/data/tv/Some Show"


async def test_outside_library_roots_rejected() -> None:
    app = FakeArr()
    app.history = {"movieId": 12}
    app.item = {"id": 12, "path": "/mnt/elsewhere/Movie"}
    with pytest.raises(ResolutionError, match="LIBRARY_ROOTS"):
        await _resolver(app).resolve(make_job(make_torrent()))


async def test_empty_library_roots_refuses_placement() -> None:
    app = FakeArr()
    app.history = {"movieId": 12}
    app.item = {"id": 12, "path": "/data/movies/Movie"}
    with pytest.raises(ResolutionError, match="LIBRARY_ROOTS is empty"):
        await _resolver(app, roots=[]).resolve(make_job(make_torrent()))


async def test_unknown_category_rejected() -> None:
    with pytest.raises(ResolutionError, match="CATEGORY_MAP"):
        await _resolver(FakeArr()).resolve(make_job(make_torrent(), category="unknown"))


async def test_missing_category_rejected() -> None:
    with pytest.raises(ResolutionError, match="no category"):
        await _resolver(FakeArr()).resolve(make_job(make_torrent(), category=None))


async def test_no_resolution_at_all() -> None:
    with pytest.raises(ResolutionError, match="could not resolve"):
        await _resolver(FakeArr()).resolve(make_job(make_torrent()))


async def test_history_id_missing_falls_back_to_parse() -> None:
    app = FakeArr()
    app.history = {"someOtherId": 1}
    app.parse = {"movie": {"id": 5, "path": "/data/movies/Fallback (2020)"}}
    resolved = await _resolver(app).resolve(make_job(make_torrent()))
    assert resolved.path == "/data/movies/Fallback (2020)"


def test_compose_destination_validation() -> None:
    with pytest.raises(ResolutionError, match="no path"):
        compose_destination("radarr", {"id": 1}, None)
    with pytest.raises(ResolutionError, match="season number"):
        compose_destination("sonarr", {"path": "/data/tv/Show", "seasonFolder": True}, None)
    with pytest.raises(ResolutionError, match="unusable path"):
        compose_destination("radarr", {"path": "relative/path"}, None)
