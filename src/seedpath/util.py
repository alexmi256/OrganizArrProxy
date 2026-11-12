"""Small shared helpers: durations, UTC timestamps, path containment."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BeforeValidator

_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h|d)?\s*$", re.IGNORECASE
)
_MULTIPLIERS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value: object) -> float:
    """Parse ``10s`` / ``500ms`` / ``1m`` / ``2`` into seconds.

    A bare number means seconds, matching the rest of the config surface.
    """
    if isinstance(value, bool):
        msg = f"invalid duration: {value!r}"
        raise ValueError(msg)
    if isinstance(value, (int, float)):
        if value < 0:
            msg = "duration must not be negative"
            raise ValueError(msg)
        return float(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match is None:
            msg = f"invalid duration: {value!r} (expected e.g. 500ms, 10s, 1m, 2h)"
            raise ValueError(msg)
        number = float(match.group("value"))
        unit = (match.group("unit") or "s").lower()
        return number * _MULTIPLIERS[unit]
    msg = f"invalid duration: {value!r}"
    raise ValueError(msg)


Duration = Annotated[float, BeforeValidator(parse_duration)]


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp used in state records and logs."""
    return utcnow().isoformat(timespec="milliseconds")


def normalize_posix_path(value: str) -> str:
    """Normalize an absolute POSIX path without resolving it on the host.

    ``seedpath`` deliberately never touches the library filesystem: paths are
    strings shared between the *arr and rTorrent containers. Lexical
    normalization is enough to reject traversal and compare roots.
    """
    if not value:
        msg = "path is empty"
        raise ValueError(msg)
    # PurePosixPath keeps paths portable even when tests run on another OS.
    path = PurePosixPath(value)
    if not path.is_absolute():
        msg = f"path must be absolute: {value!r}"
        raise ValueError(msg)
    parts: list[str] = []
    for part in path.parts:
        if part in ("/", "."):
            continue
        if part == "..":
            if not parts:
                msg = f"path escapes its root: {value!r}"
                raise ValueError(msg)
            parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def path_within_roots(path: str, roots: list[str]) -> bool:
    """Return True when *path* is inside one of *roots*.

    An empty root list fails closed: LIBRARY_ROOTS is a placement guard, and
    "no roots configured" must not silently mean "anywhere on disk".
    """
    if not roots:
        return False
    normalized = normalize_posix_path(path)
    for root in roots:
        normalized_root = normalize_posix_path(root)
        prefix = normalized_root if normalized_root.endswith("/") else normalized_root + "/"
        if normalized == normalized_root or normalized.startswith(prefix):
            return True
    return False
