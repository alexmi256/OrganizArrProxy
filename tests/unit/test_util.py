"""Tests for duration parsing and path containment."""

from __future__ import annotations

import pytest

from seedpath.util import normalize_posix_path, parse_duration, path_within_roots


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10, 10.0),
        (0, 0.0),
        ("10", 10.0),
        ("10s", 10.0),
        ("500ms", 0.5),
        ("1m", 60.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        (" 1.5S ", 1.5),
    ],
)
def test_parse_duration(value: object, expected: float) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "-5", "-1s", "1w", True, None])
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)


def test_normalize_absolute_path() -> None:
    assert normalize_posix_path("/data/movies//Big Buck Bunny/") == "/data/movies/Big Buck Bunny"
    assert normalize_posix_path("/data/./movies/../tv") == "/data/tv"
    assert normalize_posix_path("/") == "/"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "empty"),
        ("data/movies", "absolute"),
        ("../movies", "absolute"),
        ("/../etc", "escapes"),
    ],
)
def test_normalize_rejects_non_absolute_or_escaping(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_posix_path(value)


def test_path_within_roots() -> None:
    roots = ["/data/movies", "/data/tv"]
    assert path_within_roots("/data/movies/Movie (2020)", roots)
    assert path_within_roots("/data/movies", roots)
    assert path_within_roots("/data/tv/Show/Season 01", roots)
    assert not path_within_roots("/data/movies2/Movie", roots)
    assert not path_within_roots("/data/music/Artist", roots)


def test_path_within_empty_roots_fails_closed() -> None:
    assert not path_within_roots("/anywhere", [])


def test_root_slash_allows_every_absolute_path() -> None:
    assert path_within_roots("/data/movies/Movie", ["/"])
    assert path_within_roots("/", ["/"])


def test_path_within_roots_normalizes_traversal() -> None:
    assert path_within_roots("/data/movies/../tv/Show", ["/data/tv"])
