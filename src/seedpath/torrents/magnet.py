"""Magnet URI helpers."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from seedpath.errors import MagnetError

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class MagnetInfo:
    """What seedpath needs to know about a magnet link."""

    infohash: str
    name: str | None = None


def _normalize_hash(value: str) -> str:
    if len(value) == 40:
        if not set(value).issubset(_HEX_DIGITS):
            msg = f"magnet infohash is not hexadecimal: {value!r}"
            raise MagnetError(msg)
        return value.lower()
    if len(value) == 32:
        try:
            decoded = base64.b32decode(value.upper(), casefold=True)
        except (binascii.Error, ValueError) as exc:
            msg = f"magnet infohash is not valid base32: {value!r}"
            raise MagnetError(msg) from exc
        if len(decoded) != 20:
            msg = f"magnet infohash has the wrong length: {value!r}"
            raise MagnetError(msg)
        return decoded.hex()
    msg = f"magnet infohash must be 40 hex or 32 base32 characters, got {len(value)}: {value!r}"
    raise MagnetError(msg)


def is_magnet(uri: str) -> bool:
    """True for ``magnet:?`` URIs."""
    return uri.strip().lower().startswith("magnet:?")


def parse_magnet(uri: str) -> MagnetInfo:
    """Extract the BitTorrent v1 infohash and display name from a magnet URI."""
    parts = urlsplit(uri.strip())
    if parts.scheme.lower() != "magnet":
        msg = f"not a magnet URI: {uri!r}"
        raise MagnetError(msg)
    query = parse_qs(parts.query, keep_blank_values=False)
    for xt in query.get("xt", []):
        prefix = "urn:btih:"
        if xt.lower().startswith(prefix):
            raw = xt[len(prefix) :]
            name_values = query.get("dn")
            name = name_values[0] if name_values else None
            return MagnetInfo(infohash=_normalize_hash(raw), name=name)
    msg = f"magnet URI has no urn:btih infohash: {uri!r}"
    raise MagnetError(msg)
