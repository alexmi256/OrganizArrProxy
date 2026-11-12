"""Domain exceptions.

The rule of thumb: exceptions that cross a boundary (XML-RPC, HTTP, CLI) are
translated there; internal code raises the narrowest error it can.
"""

from __future__ import annotations


class SeedpathError(Exception):
    """Base class for every error raised by seedpath itself."""


class ConfigError(SeedpathError):
    """Configuration is missing or internally inconsistent."""


class BencodeError(SeedpathError, ValueError):
    """A bencoded document could not be parsed."""


class MagnetError(SeedpathError, ValueError):
    """A magnet URI did not contain a usable BitTorrent v1 infohash."""


class RpcParseError(SeedpathError, ValueError):
    """An XML-RPC request body could not be understood."""


class RpcRewriteError(SeedpathError, ValueError):
    """A request body could not be rewritten safely."""


class UpstreamError(SeedpathError):
    """The rTorrent XML-RPC endpoint is unreachable or returned a transport error."""


class ArrError(SeedpathError):
    """An *arr REST call failed."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ResolutionError(SeedpathError):
    """The destination folder for a grab could not be determined."""


class StateError(SeedpathError):
    """Persistent state could not be read or written."""
