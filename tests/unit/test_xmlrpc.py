"""XML-RPC inspection and method rewriting."""

from __future__ import annotations

import xmlrpc.client
from typing import Any

import pytest

from seedpath.errors import RpcParseError, RpcRewriteError
from seedpath.proxy.xmlrpc import (
    analyze_load,
    ensure_safe_xml,
    extract_category,
    parse_request,
    response_is_fault,
    rewrite_method,
)
from tests.support import magnet_for, make_torrent


def _request(method: str, params: tuple[Any, ...]) -> bytes:
    return xmlrpc.client.dumps(params, methodname=method).encode()


def test_parse_request() -> None:
    body = _request("system.client_version", ())
    method, params = parse_request(body)
    assert method == "system.client_version"
    assert params == ()


def test_rewrite_changes_only_method_name() -> None:
    fixture = make_torrent()
    body = _request(
        "load.raw_start", ("", xmlrpc.client.Binary(fixture.data), "d.custom1.set=radarr")
    )
    rewritten = rewrite_method(body, "load.raw")
    assert b"<methodName>load.raw</methodName>" in rewritten
    assert b"load.raw_start" not in rewritten
    assert body.replace(b"load.raw_start", b"load.raw") == rewritten
    method, params = parse_request(rewritten)
    assert method == "load.raw"
    assert isinstance(params[1], xmlrpc.client.Binary)
    assert params[1].data == fixture.data


def test_rewrite_requires_exactly_one_method_name() -> None:
    with pytest.raises(RpcRewriteError):
        rewrite_method(b"<methodCall></methodCall>", "load.raw")
    with pytest.raises(RpcRewriteError):
        rewrite_method(
            b"<methodName>a</methodName><methodName>b</methodName>",
            "c",
        )


def test_analyze_raw_payload() -> None:
    fixture = make_torrent()
    payload = analyze_load(
        "load.raw_start",
        ("", xmlrpc.client.Binary(fixture.data), "d.custom1.set=radarr"),
    )
    assert payload.infohash == fixture.infohash
    assert payload.release_name == fixture.name
    assert payload.category == "radarr"
    assert payload.is_raw is True
    assert payload.is_magnet is False


def test_analyze_magnet_payload() -> None:
    fixture = make_torrent()
    payload = analyze_load("load.start", ("", magnet_for(fixture), "d.custom1.set=tv-sonarr"))
    assert payload.infohash == fixture.infohash
    assert payload.category == "tv-sonarr"
    assert payload.is_magnet is True
    assert payload.is_raw is False


def test_analyze_http_url_is_rejected() -> None:
    with pytest.raises(RpcParseError):
        analyze_load("load.start", ("", "https://example.invalid/file.torrent"))


def test_extract_category_ignores_other_commands() -> None:
    assert extract_category(("", "x", "d.priority.set=2", "d.custom1.set=radarr")) == "radarr"
    assert extract_category(("", "x")) is None


def test_fault_detection() -> None:
    fault = xmlrpc.client.dumps(xmlrpc.client.Fault(-501, "duplicate"), methodresponse=True)
    success = xmlrpc.client.dumps((0,), methodresponse=True)
    assert response_is_fault(fault.encode())
    assert not response_is_fault(success.encode())
    assert not response_is_fault(b"not xml")


def test_doctype_is_rejected() -> None:
    with pytest.raises(RpcParseError, match="DOCTYPE"):
        ensure_safe_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY y "z">]><methodCall/>')
