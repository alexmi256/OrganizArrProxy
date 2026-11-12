"""Magnet URI parsing."""

from __future__ import annotations

import pytest

from seedpath.errors import MagnetError
from seedpath.torrents.magnet import is_magnet, parse_magnet
from tests.support import magnet_for, make_torrent


def test_hex_magnet() -> None:
    fixture = make_torrent()
    info = parse_magnet(magnet_for(fixture))
    assert info.infohash == fixture.infohash
    assert info.name == fixture.name


def test_base32_magnet() -> None:
    fixture = make_torrent()
    info = parse_magnet(magnet_for(fixture, base32=True))
    assert info.infohash == fixture.infohash


def test_uppercase_hex_is_normalized() -> None:
    fixture = make_torrent()
    uri = f"magnet:?xt=urn:btih:{fixture.infohash.upper()}"
    assert parse_magnet(uri).infohash == fixture.infohash


def test_magnet_without_display_name() -> None:
    fixture = make_torrent()
    info = parse_magnet(f"magnet:?xt=urn:btih:{fixture.infohash}")
    assert info.name is None


def test_magnet_without_btih() -> None:
    with pytest.raises(MagnetError, match="urn:btih"):
        parse_magnet("magnet:?dn=Something&tr=udp://tracker.invalid:80")


def test_magnet_with_bad_hash_length() -> None:
    with pytest.raises(MagnetError, match="40 hex or 32 base32"):
        parse_magnet("magnet:?xt=urn:btih:abc")


def test_magnet_with_bad_hex() -> None:
    with pytest.raises(MagnetError, match="hexadecimal"):
        parse_magnet("magnet:?xt=urn:btih:" + "z" * 40)


def test_magnet_with_bad_base32() -> None:
    with pytest.raises(MagnetError, match="base32"):
        parse_magnet("magnet:?xt=urn:btih:" + "1" * 32)


def test_not_a_magnet() -> None:
    assert not is_magnet("https://example.invalid/file.torrent")
    with pytest.raises(MagnetError, match="not a magnet"):
        parse_magnet("https://example.invalid/file.torrent")
