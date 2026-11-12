# ADR 0002 — Forward XML-RPC bodies, patch only the method name

- **Status:** Accepted
- **Date:** 2026-11-11
- **Related:** [ADR 0001](0001-python-framework.md)

## Context

The proxy must make `load.start` and `load.raw_start` arrive at rTorrent as
`load.normal` and `load.raw`, while preserving:

- faults (code and string) exactly;
- base64-encoded `.torrent` payloads byte for byte after decoding;
- very large `d.multicall2` responses without decoding or re-encoding;
- arbitrary methods that seedpath does not know about.

There are two plausible implementations:

1. Decode every request into Python values, mutate the method, re-encode;
   decode every response, detect faults, re-encode.
2. Locate the single `<methodName>` text node and replace its contents; send
   the original bytes; return the upstream response bytes unchanged.

## Decision

Use option 2.

- Non-`load.*` methods are forwarded exactly as received.
- `load.start` / `load.raw_start` bodies are forwarded with only the
  `<methodName>` text replaced. The parser is used read-only to learn the
  infohash and category; it never produces the forwarded body.
- Responses are returned byte for byte. Fault detection parses only the small
  `load.*` responses.
- If the body cannot be understood, or the payload is not an identifiable
  `.torrent`/magnet, the original request is forwarded untouched and no
  worker is started. seedpath never pauses something it cannot place.

## Consequences

- `d.multicall2` traffic stays O(bytes copied), not O(parse + marshal), and
  exotic or future XML-RPC types survive untouched.
- Faults cannot be accidentally reshaped by a Python value round trip.
- The rewrite is text-based and depends on the standard `<methodName>` node.
  The implementation requires exactly one match and raises otherwise; the
  request is not forwarded partially rewritten.
- Every rewrite has a byte-diff test proving nothing else changed.
- seedpath must still parse `load.*` bodies to derive the hash. That parse is
  bounded by `MAX_BODY_BYTES`, rejects DTD/entities, and is never used to
  regenerate the payload.

## Alternatives rejected

- **Typed round trip for everything:** simpler to read, but re-encodes large
  responses and can subtly change types/formatting; it also risks faults
  losing their exact shape.
- **Full XML DOM rewrite:** more "correct" XML handling, but allocates a tree
  for every load and still has to reproduce the document. The method name is
  the only field we own; patching that span is the smallest possible change.
