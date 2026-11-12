"""Bencode and infohash extraction."""

from __future__ import annotations

import pytest

from seedpath.errors import BencodeError
from seedpath.torrents.bencode import decode, infohash_from_torrent, name_from_torrent
from tests.support import bencode, make_torrent


def test_decode_scalars_and_containers() -> None:
    assert decode(b"i42e") == 42
    assert decode(b"4:spam") == b"spam"
    assert decode(b"l4:spami1ee") == [b"spam", 1]
    assert decode(b"d3:cow3:moo4:spam4:eggse") == {b"cow": b"moo", b"spam": b"eggs"}


def test_single_file_infohash_and_name() -> None:
    fixture = make_torrent()
    assert infohash_from_torrent(fixture.data) == fixture.infohash
    assert name_from_torrent(fixture.data) == fixture.name


def test_multi_file_infohash_and_name() -> None:
    fixture = make_torrent(name="Some.Show.S01.1080p", multifile=True)
    assert infohash_from_torrent(fixture.data) == fixture.infohash
    assert name_from_torrent(fixture.data) == fixture.name


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"i1e",
        b"d4:name1:ae",
        b"d4:infod4:name1:aeextra",
        b"d4:infod4:name1:ae4:infod4:name1:be",
        b"d4:infole",
        b"d4:info1:x",
    ],
)
def test_malformed_torrents(payload: bytes) -> None:
    with pytest.raises(BencodeError):
        infohash_from_torrent(payload)


def test_truncated_payload() -> None:
    fixture = make_torrent()
    with pytest.raises(BencodeError):
        infohash_from_torrent(fixture.data[:-3])


def test_nesting_limit() -> None:
    payload = b"l" * 100 + b"e" * 100
    with pytest.raises(BencodeError, match="nesting"):
        decode(payload)


def test_trailing_garbage_rejected() -> None:
    with pytest.raises(BencodeError, match="trailing"):
        decode(b"i1eextra")


def test_non_canonical_lengths_rejected() -> None:
    with pytest.raises(BencodeError):
        decode(b"03:abc")


def test_duplicate_keys_rejected() -> None:
    with pytest.raises(BencodeError, match="duplicate"):
        decode(b"d1:a1:x1:a1:ye")


def test_unicode_name_is_decoded_lossily() -> None:
    data = bencode({b"info": {b"name": "Amélie".encode(), b"length": 1}})
    assert name_from_torrent(data) == "Amélie"
