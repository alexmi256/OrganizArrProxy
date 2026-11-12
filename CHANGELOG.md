# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] — 2026-11-11

Initial scaffold. **Pre-alpha; no real-stack acceptance run yet.**

### Added

- XML-RPC proxy at `/RPC2` that forwards non-`load.*` traffic byte for byte,
  rewrites `load.start`/`load.raw_start` to their paused equivalents, and
  returns faults unchanged.
- Infohash extraction from `.torrent` payloads (exact `info` span SHA-1) and
  magnets (hex and base32), with release-name extraction.
- SQLite state machine (`RECEIVED` → `LOADED` → `RESOLVED` → `PLACED` →
  `STARTED` → `COMPLETE` → `ADOPTED` → `DONE`) with restart recovery.
- Destination resolution via *arr history, falling back to `parse?title=`,
  with Sonarr season-folder support and `LIBRARY_ROOTS` containment.
- Placement worker, completion watcher and conservative reconciler.
- Fail-open behaviour when resolution or placement fails, and refusal to
  retarget a torrent that has already started.
- Re-grabbing an infohash with a terminal state row resets the row and places
  the new torrent instead of leaving it paused.
- Crash recovery sweeps paused incomplete torrents (`state=1`, `is_active=0`),
  which is the state rTorrent uses for a paused-at-add item.
- `LIBRARY_ROOTS` fails closed when unset; `RT_PASS` is the documented
  environment name; chunked request bodies obey `MAX_BODY_BYTES`; Servarr
  redirects are not followed, so `X-Api-Key` cannot leak cross-origin.
- Servarr adapters for Radarr, Sonarr, Lidarr, Readarr and Whisparr.
- `seedpath preflight` with per-app checks, a rich table and `--json` output.
- `/livez`, `/healthz`, `/metrics`, structured JSON logs with infohash
  context.
- Typed configuration via environment variables, including `APPS_JSON` for
  multiple instances and duration strings.
- Dockerfile (multi-stage, non-root, read-only friendly) and an example
  Compose file.
- Test suite: 160+ unit/integration tests, `httpx.MockTransport` clients,
  mock rTorrent and *arr, 80% coverage floor.
- Tooling: uv, pytest, ruff, mypy strict, just, pre-commit.
- CI (lint, types, tests on 3.12–3.14, build, Docker), multi-arch GHCR
  publishing with provenance/SBOM, GitHub releases, CodeQL and Dependabot.
- Documentation: README, architecture, configuration, deployment, Servarr
  setup, operations, compatibility, testing, roadmap, and ADRs covering the
  framework choice and raw XML-RPC passthrough.

### Known limitations

- HTTP `.torrent` URLs are forwarded but not retargeted (hash unknown).
- Single instance per rTorrent; no external queue.
- No upgrade automation, usenet support, cross-seed or library migration.
