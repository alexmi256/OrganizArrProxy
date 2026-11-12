"""XML-RPC wire helpers.

The proxy is deliberately split in two halves:

* **Passthrough** — every method other than ``load.*`` is forwarded byte for
  byte, request and response. Large ``d.multicall2`` results are never decoded.
* **Rewrite** — ``load.start`` / ``load.raw_start`` are retargeted to their
  paused equivalents by patching only the ``<methodName>`` text node. The
  base64-encoded torrent payload is therefore never re-encoded.

Requests are decoded only far enough to learn the infohash, which seedpath
needs in order to place the torrent later.
"""

from __future__ import annotations

import re
import xmlrpc.client
from dataclasses import dataclass
from typing import Any

from seedpath.errors import RpcParseError, RpcRewriteError
from seedpath.torrents.bencode import infohash_from_torrent, name_from_torrent
from seedpath.torrents.magnet import is_magnet, parse_magnet

#: Methods that start a download, and their paused equivalents.
PAUSE_MAP: dict[str, str] = {
    "load.start": "load.normal",
    "load.raw_start": "load.raw",
}

LOAD_METHODS = frozenset({"load.start", "load.raw_start", "load.normal", "load.raw"})

_METHOD_RE = re.compile(rb"<methodName(\s*)>(.*?)</methodName\s*>", re.DOTALL)
_FORBIDDEN_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LoadPayload:
    """Location-independent description of a ``load.*`` call."""

    infohash: str
    release_name: str | None
    category: str | None
    is_raw: bool
    is_magnet: bool


def ensure_safe_xml(body: bytes) -> None:
    """Reject DTD / entity constructs before handing bytes to expat.

    ``xmlrpc.client`` is documented as unsafe for untrusted input. XML-RPC
    payloads never legitimately use a DTD, so a cheap pre-check is enough and
    keeps the hot path allocation-free.
    """
    if _FORBIDDEN_XML.search(body):
        msg = "XML-RPC payload must not contain a DOCTYPE or ENTITY declaration"
        raise RpcParseError(msg)


def parse_request(body: bytes) -> tuple[str, tuple[Any, ...]]:
    """Return ``(method_name, params)`` for a request body."""
    ensure_safe_xml(body)
    try:
        params, method = xmlrpc.client.loads(body)
    except xmlrpc.client.Fault as exc:
        msg = f"request body is an XML-RPC fault: {exc.faultString}"
        raise RpcParseError(msg) from exc
    except Exception as exc:  # expat raises a family of exceptions
        msg = f"invalid XML-RPC request: {exc}"
        raise RpcParseError(msg) from exc
    if not isinstance(method, str):
        msg = "XML-RPC request has no method name"
        raise RpcParseError(msg)
    return method, tuple(params)


def rewrite_method(body: bytes, new_method: str) -> bytes:
    """Replace the single ``<methodName>`` text node and nothing else."""
    matches = list(_METHOD_RE.finditer(body))
    if len(matches) != 1:
        msg = f"expected exactly one <methodName>, found {len(matches)}"
        raise RpcRewriteError(msg)
    match = matches[0]
    return body[: match.start(2)] + new_method.encode("ascii") + body[match.end(2) :]


def build_request(method: str, params: tuple[Any, ...] | list[Any]) -> bytes:
    """Serialize a typed XML-RPC call for rTorrent."""
    return xmlrpc.client.dumps(tuple(params), methodname=method, allow_none=True).encode("utf-8")


def response_is_fault(body: bytes) -> bool:
    """Whether a response body is an XML-RPC fault.

    Only used for small ``load.*`` responses. Unparseable responses are
    treated as success so a torrent is not left paused forever by a harmless
    protocol quirk.
    """
    try:
        xmlrpc.client.loads(body)
    except xmlrpc.client.Fault:
        return True
    except Exception:
        return False
    return False


def extract_category(params: tuple[Any, ...]) -> str | None:
    """Pull ``d.custom1.set=<category>`` out of the load command list."""
    for raw in params[2:]:
        if isinstance(raw, str) and raw.startswith("d.custom1.set="):
            value = raw.split("=", 1)[1].strip()
            return value or None
    return None


def analyze_load(method: str, params: tuple[Any, ...]) -> LoadPayload:
    """Derive the infohash and metadata of a ``load.*`` request.

    Raises :class:`RpcParseError` when the payload shape is not one seedpath
    can identify. Callers must forward such a request untouched rather than
    pausing a torrent they cannot later retarget.
    """
    if method not in LOAD_METHODS:
        msg = f"not a load method: {method}"
        raise RpcParseError(msg)
    if len(params) < 2:
        msg = f"{method} has too few arguments"
        raise RpcParseError(msg)
    payload = params[1]
    category = extract_category(params)

    raw: bytes | None
    if isinstance(payload, xmlrpc.client.Binary):
        raw = payload.data
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raw = None

    if raw is not None:
        try:
            return LoadPayload(
                infohash=infohash_from_torrent(raw),
                release_name=name_from_torrent(raw),
                category=category,
                is_raw=True,
                is_magnet=False,
            )
        except Exception as exc:
            msg = f"could not read torrent payload: {exc}"
            raise RpcParseError(msg) from exc

    if isinstance(payload, str) and is_magnet(payload):
        info = parse_magnet(payload)
        return LoadPayload(
            infohash=info.infohash,
            release_name=info.name,
            category=category,
            is_raw=False,
            is_magnet=True,
        )

    msg = "load payload is neither a .torrent body nor a magnet URI"
    raise RpcParseError(msg)
