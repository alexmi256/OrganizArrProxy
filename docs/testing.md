# Testing

```bash
just test        # fast suite
just coverage    # coverage report (fails under 80%)
just check       # format + lint + mypy + test, what CI runs
```

## Layers

### Unit

- `tests/unit/test_bencode.py` — infohash on single-file and multi-file
  torrents, malformed payloads, depth/size guard rails.
- `tests/unit/test_magnet.py` — hex and base32 `btih`, display name, invalid
  hashes.
- `tests/unit/test_xmlrpc.py` — method rewrite preserves every other byte,
  binary payloads survive, faults are detected, DTDs are rejected.
- `tests/unit/test_config.py` — env parsing, durations, app merging,
  category-map validation, secret redaction.
- `tests/unit/test_state.py` — schema, transitions, restart persistence.
- `tests/unit/test_clients.py` — rTorrent and Servarr clients against
  `httpx.MockTransport`.

### Integration

- `tests/integration/test_resolver.py` — history-first resolution, parse
  fallback, Sonarr season folders, root containment.
- `tests/integration/test_worker.py` — the full placement pipeline with fakes,
  fail-open paths, set-directory recovery, rescan, reconcile, idempotency.
- `tests/integration/test_api.py` — real Starlette app with fake rTorrent and
  app: rewrite, byte passthrough, duplicate-fault passthrough, auth, body
  limit, health, metrics.
- `tests/integration/test_preflight.py` — every check against a mocked API.
- `tests/integration/test_cli.py` — CLI smoke tests.

## Design of the fakes

`tests/support.py` provides:

- `make_torrent()` — builds a real bencoded single-file or multi-file torrent
  and the expected infohash.
- `FakeTorrent` — records every `d.*` call, injectable faults, configurable
  complete/hash state.
- `FakeArr` — configurable history/item/parse responses and recorded rescans.
- `FakeWorker` — captures enqueued jobs.

The protocols in `src/seedpath/clients/ports.py` exist so tests can use these
without patching modules.

## Adding a test

- A protocol change should get a mock-stack test in `test_api.py` or
  `test_worker.py`.
- A resolver branch needs a `FakeArr` variant; do not call a real app.
- A new failure path must assert the torrent is either started (fail open) or
  left in an explainable state.
- Keep tests fast: inject `sleep` where the production code allows it; use
  `resolve_timeout=0`/`load_timeout=0` for timeout branches.

## Coverage

CI enforces 80% line/branch coverage. Coverage is a floor, not a goal: the
critical paths are the raw-byte passthrough, the fail-open behaviour, and the
reconciler's "never retarget a seeding torrent" rule.

## Acceptance on a real stack

These cannot be proven with mocks; run them before calling a release stable.
From `BRIEF.md` §12:

1. Single-file movie grab lands at `<library>/<Movie (Year)>/<release>.mkv`,
   seeds, appears in Radarr, and `stat` shows one inode with link count 1.
2. Folder release keeps `.nfo` and subtitles and seeds without re-downloading.
3. `arr` logs contain no import attempts, no "fallback to copy", no
   "Source and destination can't be the same".
4. Killing seedpath between load and placement leaves no torrent paused
   forever after restart.
5. `arr` can pause/resume/remove through its UI; `d.erase` still works.
6. Removing the torrent without deleting data leaves the library file playable.

Also verify the three API-invisible properties:

- rTorrent can write inside an *arr-created folder;
- both containers resolve the same absolute paths;
- the app's queue `outputPath` equals the `dest` in the log.

## CI

`.github/workflows/ci.yml` runs lint, mypy, tests on Python 3.12–3.14, a
distribution build, and a no-push Docker build. `.github/workflows/docker.yml`
publishes multi-arch images to GHCR on `main` and tags.
