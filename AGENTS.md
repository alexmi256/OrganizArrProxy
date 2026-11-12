# AGENTS.md

Instructions for coding agents (and humans) working in this repository. Read
this before changing code.

## What this project is

`seedpath` is an XML-RPC proxy between Servarr apps and rTorrent. It makes a
grab download directly into the media library and seed from there, then asks
the app to **scan** the folder instead of importing it. The full rationale and
source-level analysis live in [`BRIEF.md`](BRIEF.md) — treat that file as the
product specification and do not rewrite it unless the design changes.

The repository directory is `OrganizArrProxy`; the working package name is
`seedpath` (a placeholder from the brief). A rename touches the package name,
`pyproject.toml`, the Docker entrypoint and docs.

## Non-negotiable invariants

Breaking any of these is a design regression, not a style issue:

1. **Never modify `arr` or rTorrent.** Everything is public APIs.
2. **Never touch the media filesystem.** No `mkdir`, `stat`, hardlink or copy
   in production code. Paths are strings; rTorrent creates the files.
3. **Answer `arr` before doing follow-up work.** The upstream load response is
   returned first; placement runs in a background task.
4. **Pass non-`load.*` XML-RPC traffic through byte for byte.** Never decode
   and re-encode `d.multicall2` or faults.
5. **Only pause a torrent you can identify and place.** If the infohash or
   category is missing/unmapped, forward untouched and log it.
6. **Never leave a torrent paused with no owner.** Resolution failures call
   `d.start` and record `FAILED_OPEN`.
7. **Never retarget a torrent that is already seeding.** The reconciler only
   sweeps paused (`state=1`, `is_active=0`), incomplete, mapped-category
   torrents; started/seeding items have `is_active=1`.
8. **Never place outside `LIBRARY_ROOTS`.**
9. **Never log or commit secrets.** API keys are `SecretStr`; `seedpath config`
   must stay redacted.
10. **One process.** SQLite plus in-memory queues: do not add multi-replica
    assumptions without an external queue/store.

## Commands

```bash
just install     # uv sync --locked
just check       # format check + ruff + mypy + pytest (what CI runs)
just test        # pytest
just coverage    # coverage, fails under 80%
just lint        # ruff check
just fmt         # ruff format
just typecheck   # mypy (strict, src + tests)
just preflight   # CLI self-check against configured apps
just run         # local server on :8080
just docker      # build image
```

Use `uv run ...` for ad-hoc commands; never install into the system Python.

## Repository map

```text
src/seedpath/
  api/app.py          Starlette app, routes, Basic auth, body limit, lifespan
  cli.py              typer CLI: serve, preflight, config, version
  config.py           pydantic-settings; env -> Apps, categories, durations
  runtime.py          builds/closes clients, store and worker
  metrics.py          prometheus counters/histograms
  preflight.py        read-only *arr configuration checks + rich table
  proxy/xmlrpc.py     parse/rewrite/analyze XML-RPC bodies (the core protocol)
  proxy/service.py    forward decision + post-response enqueue
  clients/ports.py    protocols used by core (test seams)
  clients/rtorrent.py raw passthrough + typed d.* calls
  clients/arr.py      Servarr REST adapter
  core/models.py      GrabJob, TorrentRecord, ResolvedItem, states
  core/resolver.py    history -> parse fallback, destination composition
  core/state.py       SQLite repository
  core/worker.py      placement queue, completion watcher, reconciler
  torrents/           bencode + magnet parsing
tests/
  support.py          torrent builder, FakeTorrent, FakeArr, FakeWorker
  unit/               pure logic and HTTP clients
  integration/        resolver, worker, ASGI app, preflight, CLI, runtime
docs/                 architecture, config, deployment, ADRs, operations
```

## Code conventions

- Python 3.12+, `from __future__ import annotations`, full type annotations.
- `mypy --strict` must pass on both `src` and `tests`.
- Ruff: line length 100, `A,ANN,ASYNC,B,E,F,I,PT,RUF,S,SIM,T20,UP,W`. Use
  `raise ... from exc`, `msg = ...; raise X(msg)` (EM101/EM102 style).
- Async I/O only in the request/worker paths; synchronous SQLite access is
  wrapped by `aiosqlite`.
- Log with `structlog` events (`proxy.load_accepted`, `torrent.state`), not
  free-form messages. Bind `infohash` context for lifecycle lines.
- Settings belong in `config.py`; no `os.environ` reads elsewhere.
- Timeouts on every outbound call.
- Comments explain *why* (especially protocol quirks); do not restate code.

## Testing expectations

- Every bug fix gets a regression test.
- Protocol changes need a mock-stack test: `FakeTorrent` records calls; assert
  the exact `d.directory.set` path and call order.
- A new failure path must assert either fail-open (`d.start`) or the recorded
  terminal state.
- Prefer `httpx.MockTransport` over monkeypatching client internals.
- Tests must not require network, Docker, rTorrent or real *arr apps.
- Coverage floor is 80% and is enforced by `just coverage`/CI.

## Working with XML-RPC

- `xmlrpc.client.loads` returns `(params, method)`.
- `xmlrpc.client.Binary` carries `.torrent` bytes; accept `bytes` and
  `bytearray` too.
- A fault response is `xmlrpc.client.dumps(Fault(...), methodresponse=True)`.
- Method rewrite uses a regex over `<methodName>`; keep the "exactly one
  match" guard, and keep the byte-diff test.
- `loads` is documented as unsafe for hostile XML: keep the DOCTYPE/ENTITY
  rejection and the body-size cap.

## Deployment expectations

- Dockerfile: multi-stage uv build, `python:3.12-alpine`, non-root UID 10001,
  read-only root filesystem, `/config` volume, healthcheck on `/livez`.
- Compose: no published ports, `cap_drop: ALL`, `no-new-privileges`,
  `init: true`, bounded logs.
- Image is published to GHCR by `.github/workflows/docker.yml` on `main` and
  semver tags; releases are created by `release.yml`.

## Definition of done

- `just check` is green.
- `uv.lock` updated if dependencies changed.
- New configuration documented in `.env.example`,
  `docs/configuration.md`, and the README table if prominent.
- Behaviour changes reflected in `docs/architecture.md` (state machine,
  failure table) and `CHANGELOG.md`.
- No secrets, personal paths, magnet links or tracker passkeys in tests/docs.
- If the change affects the real-stack contract, add or update the acceptance
  checklist in `docs/testing.md`.

## Current status

Pre-alpha scaffold. The protocol core, placement pipeline, preflight, HTTP
surface, tests, packaging and CI exist. The real-stack acceptance run
(`docs/testing.md`) has **not** been performed. Do not describe it as
production-ready, and do not claim a stable compatibility matrix until it has.
