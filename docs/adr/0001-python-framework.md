# ADR 0001 — HTTP framework and XML-RPC strategy

- **Status:** Accepted
- **Date:** 2026-11-11
- **Deciders:** seedpath maintainers
- **Related:** [ADR 0002](0002-raw-xmlrpc-passthrough.md)

## Context

`seedpath` must accept an XML-RPC endpoint that *arr points at instead of
rTorrent, forward most calls untouched, rewrite two method names, and run
background workers. It must also expose `/livez`, `/healthz` and `/metrics`,
shut down gracefully, and stay testable without a real rTorrent.

The hard protocol requirements are:

- faults pass through with their original code and string;
- `.torrent` payloads survive as bytes;
- large `d.multicall2` responses are not re-encoded;
- the HTTP request is answered before any *arr API call starts.

The brief recommends Python 3.12 with the standard library. That remains true
for XML-RPC marshalling (`xmlrpc.client`), but the HTTP/operations surface is
where frameworks differ.

## Criteria

1. Raw request/response body access with no implicit JSON/XML parsing.
2. Async or at least concurrent request handling; outbound calls must not
   block the loop.
3. Lifespan hooks for starting/stopping workers and clients.
4. Cheap routing and middleware for health/metrics/auth.
5. First-class async test client and dependency-free mocking.
6. Small image, few dependencies, active maintenance.
7. Fit with Python type checking (`mypy --strict`).

## Options

### A. Standard library: `ThreadingHTTPServer` + `xmlrpc.server`

**Pros:** zero dependencies; native XML-RPC in both directions; matches the
brief literally; tiny image.

**Cons:** no router, lifespan, middleware or graceful-drain story; one thread
per connection; health/metrics need hand-written path dispatch; the XML-RPC
server performs typed unmarshalling by default and dispatching dotted method
names (`d.multicall2`) requires overriding internals; testing needs real
sockets or custom harnesses. The stdlib documentation explicitly warns that
`xmlrpc.client`/`server` are not secure against maliciously constructed data,
so a hand-rolled server still needs hardening.

Verdict: workable for a prototype, weak for an operated daemon.

### B. Starlette + uvicorn — **chosen**

**Pros:** ASGI concurrency; minimal and unopinionated; raw `Request.body()`;
`Route`/middleware/`BackgroundTask`; lifespan for the worker and clients;
`TestClient`; uvicorn is a production server with signal handling. FastAPI is
built on this same stack, so the foundations are proven.

**Cons:** XML-RPC must be handled manually — which is exactly what the raw
passthrough design wants anyway. A handful of dependencies (Starlette,
uvicorn, anyio, httpx).

### C. FastAPI + uvicorn

**Pros:** everything Starlette offers, plus type-driven request validation,
dependency injection and OpenAPI docs; very familiar to reviewers.

**Cons:** its value is in schema-driven JSON endpoints. Our main endpoint is an
XML document that must be forwarded byte for byte and must *not* be described
by OpenAPI. The four REST endpoints are trivial and do not need a validation
layer. FastAPI would add a layer and encourage exactly the typed XML handling
we are avoiding. Pydantic is still used directly for settings and models.

Verdict: unnecessary surface for this workload.

### D. Flask or Django (WSGI)

**Pros:** familiar; Django has batteries included.

**Cons:** synchronous WSGI; a slow *arr call occupies a thread; lifespan and
background workers are bolt-ons; Django is orders of magnitude too heavy for a
single-purpose proxy. Flask + a thread pool could work but gives nothing over
ASGI here.

### E. aiohttp

**Pros:** one library for client and server; mature async.

**Cons:** viable, but its request handling, test utilities and middleware
model are less aligned with the httpx/Starlette testing style already used for
the Servarr clients. No decisive advantage.

### F. Litestar

**Pros:** modern ASGI, typed, good performance.

**Cons:** larger opinionated surface (DI, DTOs, plugins) for four routes and
one raw XML endpoint; smaller ecosystem than Starlette.

### G. Twisted

**Pros:** mature async networking and XML-RPC support.

**Cons:** Deferred-based programming model, heavyweight, and a steep review
cost for a small proxy. The brief's protocol needs are met by
`xmlrpc.client`.

## Decision

Use **Starlette** as the ASGI toolkit and **uvicorn** as the server. Keep
`xmlrpc.client` from the standard library for XML-RPC marshalling, and use
**httpx** for both upstreams. Use **pydantic-settings** for configuration,
**aiosqlite** for state, **structlog** for logs, **typer/rich** for the CLI and
**prometheus-client** for metrics.

The XML-RPC endpoint is implemented as one raw ASGI route:
`Request.body()` → inspect → forward → return bytes. This is framework
independent, which keeps the door open to swapping the HTTP layer later.

## Consequences

- The protocol boundary is ours to test and secure. We reject
  `DOCTYPE`/`ENTITY`, cap body size, and only parse `load.*` calls. The ingress
  is an internal-network service, never a public XML-RPC gateway.
- We depend on the ASGI ecosystem and must keep uvicorn/Starlette current.
- One process is the deployment model; SQLite and in-memory queues make
  replicas unsafe until an external queue/store is introduced.
- FastAPI can be adopted later without rewriting the proxy core if typed REST
  endpoints grow.

## References

- Python docs, `xmlrpc.client` security warning:
  <https://docs.python.org/3/library/xmlrpc.client.html>
- Python XML security guidance:
  <https://docs.python.org/3/library/xml.html#xml-security>
- Starlette documentation: <https://www.starlette.io/>
- FastAPI documentation: <https://fastapi.tiangolo.com/>
- Honeybadger, "Starlette vs FastAPI: what FastAPI actually adds" (2026-07-20):
  <https://www.honeybadger.io/blog/starlette-vs-fastapi/>
- Encore.dev, "FastAPI vs Starlette in 2026: When to Use Each" (2026-10-22)
- aiohttp: <https://docs.aiohttp.org/>
- Litestar: <https://litestar.dev/>
