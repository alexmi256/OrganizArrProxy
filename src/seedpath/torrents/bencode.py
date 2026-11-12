"""Bencode parsing and infohash extraction.

Only two things are needed from a ``.torrent`` file: the exact byte span of
the top-level ``info`` dictionary (so its SHA-1 can be computed) and the
release name. A full decoder is included mainly to keep the tests honest and
to make ``info.name`` available for logging.
"""

from __future__ import annotations

import hashlib

from seedpath.errors import BencodeError

#: Guard rails against pathological payloads. Torrent files are normally a few
#: kilobytes; 64 levels of nesting is already absurd.
MAX_DEPTH = 64

type BencodeValue = bytes | int | list[BencodeValue] | dict[bytes, BencodeValue]


def _read_int(data: bytes, index: int) -> tuple[int, int]:
    end = data.find(b"e", index)
    if end == -1:
        msg = "unterminated integer"
        raise BencodeError(msg)
    raw = data[index:end]
    if not raw or raw == b"-" or (raw.startswith(b"-0")) or (raw.startswith(b"0") and len(raw) > 1):
        msg = f"invalid integer at byte {index}"
        raise BencodeError(msg)
    try:
        return int(raw), end + 1
    except ValueError as exc:
        msg = f"invalid integer at byte {index}"
        raise BencodeError(msg) from exc


def _read_bytes(data: bytes, index: int) -> tuple[bytes, int]:
    colon = data.find(b":", index)
    if colon == -1:
        msg = "unterminated byte string length"
        raise BencodeError(msg)
    raw_length = data[index:colon]
    if not raw_length.isdigit():
        msg = f"invalid byte string length at byte {index}"
        raise BencodeError(msg)
    if len(raw_length) > 1 and raw_length.startswith(b"0"):
        msg = f"non-canonical byte string length at byte {index}"
        raise BencodeError(msg)
    length = int(raw_length)
    start = colon + 1
    end = start + length
    if end > len(data):
        msg = "byte string runs past the end of the payload"
        raise BencodeError(msg)
    return data[start:end], end


def _skip_value(data: bytes, index: int, depth: int) -> int:
    """Return the index just past the value starting at *index*."""
    if depth > MAX_DEPTH:
        msg = "bencode nesting too deep"
        raise BencodeError(msg)
    if index >= len(data):
        msg = "unexpected end of payload"
        raise BencodeError(msg)
    marker = data[index : index + 1]
    if marker == b"i":
        _, end = _read_int(data, index + 1)
        return end
    if marker == b"l":
        index += 1
        while True:
            if index >= len(data):
                msg = "unterminated list"
                raise BencodeError(msg)
            if data[index : index + 1] == b"e":
                return index + 1
            index = _skip_value(data, index, depth + 1)
    if marker == b"d":
        index += 1
        while True:
            if index >= len(data):
                msg = "unterminated dictionary"
                raise BencodeError(msg)
            if data[index : index + 1] == b"e":
                return index + 1
            _, index = _read_bytes(data, index)
            index = _skip_value(data, index, depth + 1)
    if marker.isdigit():
        _, end = _read_bytes(data, index)
        return end
    msg = f"invalid bencode marker at byte {index}"
    raise BencodeError(msg)


def _decode(data: bytes, index: int, depth: int) -> tuple[BencodeValue, int]:
    if depth > MAX_DEPTH:
        msg = "bencode nesting too deep"
        raise BencodeError(msg)
    if index >= len(data):
        msg = "unexpected end of payload"
        raise BencodeError(msg)
    marker = data[index : index + 1]
    if marker == b"i":
        return _read_int(data, index + 1)
    if marker == b"l":
        items: list[BencodeValue] = []
        index += 1
        while True:
            if index >= len(data):
                msg = "unterminated list"
                raise BencodeError(msg)
            if data[index : index + 1] == b"e":
                return items, index + 1
            item, index = _decode(data, index, depth + 1)
            items.append(item)
    if marker == b"d":
        result: dict[bytes, BencodeValue] = {}
        index += 1
        while True:
            if index >= len(data):
                msg = "unterminated dictionary"
                raise BencodeError(msg)
            if data[index : index + 1] == b"e":
                return result, index + 1
            key, index = _read_bytes(data, index)
            if key in result:
                msg = f"duplicate dictionary key: {key!r}"
                raise BencodeError(msg)
            value, index = _decode(data, index, depth + 1)
            result[key] = value
    if marker.isdigit():
        return _read_bytes(data, index)
    msg = f"invalid bencode marker at byte {index}"
    raise BencodeError(msg)


def decode(data: bytes) -> BencodeValue:
    """Decode one bencoded document, rejecting trailing garbage."""
    value, end = _decode(data, 0, 0)
    if end != len(data):
        msg = f"trailing data after bencoded document at byte {end}"
        raise BencodeError(msg)
    return value


def _info_span(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"d"):
        msg = "torrent payload is not a bencoded dictionary"
        raise BencodeError(msg)
    index = 1
    span: tuple[int, int] | None = None
    while True:
        if index >= len(data):
            msg = "unterminated torrent dictionary"
            raise BencodeError(msg)
        if data[index : index + 1] == b"e":
            index += 1
            break
        key, index = _read_bytes(data, index)
        start = index
        index = _skip_value(data, index, 0)
        if key == b"info":
            if span is not None:
                msg = "torrent payload contains more than one info dictionary"
                raise BencodeError(msg)
            span = (start, index)
    if index != len(data):
        msg = f"trailing data after torrent payload at byte {index}"
        raise BencodeError(msg)
    if span is None:
        msg = "torrent payload has no info dictionary"
        raise BencodeError(msg)
    start, end = span
    if data[start : start + 1] != b"d":
        msg = "torrent info value is not a dictionary"
        raise BencodeError(msg)
    return start, end


def infohash_from_torrent(data: bytes) -> str:
    """Return the v1 infohash (lowercase hex) of a ``.torrent`` payload."""
    start, end = _info_span(data)
    return hashlib.sha1(data[start:end], usedforsecurity=False).hexdigest()


def name_from_torrent(data: bytes) -> str | None:
    """Return the release name from ``info.name`` when it is a UTF-8 string."""
    start, end = _info_span(data)
    info = decode(data[start:end])
    if not isinstance(info, dict):
        return None
    name = info.get(b"name")
    if not isinstance(name, bytes):
        return None
    return name.decode("utf-8", errors="replace")
