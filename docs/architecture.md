# Architecture

`seedpath` is a small daemon with three jobs:

1. **Proxy** rTorrent's XML-RPC endpoint to the *arr apps.
2. **Place** each new torrent in the folder the owning *arr item expects.
3. **Adopt** the completed file by asking the app to scan, never import.

It never touches the media filesystem. Paths are strings shared between
containers; rTorrent creates the bytes, the app records them.

```text
                       ┌──────────────────────────────────────┐
  arr ──XML-RPC───────►│ POST /RPC2                           │
                       │   parse method + load payload        │
                       │   rewrite load.start → load.normal   │
                       │   forward raw bytes ────────────────►│ rTorrent
                       │   return upstream bytes verbatim ◄───│
                       │   BackgroundTask: enqueue infohash   │
                       └───────────────┬──────────────────────┘
                                       │
                       ┌───────────────▼──────────────────────┐
                       │ PlacementWorker                      │
                       │  wait for hash → resolve → place →   │
                       │  start → wait complete → rescan      │
                       └───────────────┬──────────────────────┘
                                       │ REST
                       ┌───────────────▼──────────────────────┐
                       │ Radarr / Sonarr / Lidarr / Readarr   │
                       └──────────────────────────────────────┘
```

## Components

| Module | Responsibility |
|---|---|
| `api/app.py` | ASGI routes: `/RPC2`, `/livez`, `/healthz`, `/metrics`; Basic auth; body limit |
| `proxy/xmlrpc.py` | Request parsing, infohash/magnet extraction, safe method-name rewrite |
| `proxy/service.py` | Decision logic: forward untouched or pause + enqueue |
| `clients/rtorrent.py` | Raw passthrough and typed worker calls over XML-RPC |
| `clients/arr.py` | Servarr REST adapter, per-app API version and command names |
| `core/resolver.py` | History-first destination resolution, parse fallback, root checks |
| `core/worker.py` | Queue, placement pipeline, completion watcher, reconciler |
| `core/state.py` | SQLite state machine, one row per infohash |
| `preflight.py` | Read-only verification of every required app setting |
| `runtime.py` | Owns clients, store, resolver and worker lifecycles |
| `config.py` | Typed settings from environment variables |

## Request lifecycle

1. An app calls `load.raw_start` or `load.start` on `/RPC2`.
2. The body is parsed only far enough to obtain method name and parameters.
3. seedpath derives the infohash:
   - `.torrent` bytes → SHA-1 of the exact top-level `info` span;
   - magnet → `urn:btih`, hex or base32.
4. If the payload is identifiable **and** its category maps to a configured
   app, `load.start`/`load.raw_start` is rewritten to `load.normal`/`load.raw`
   by patching only the `<methodName>` text node.
5. The request is forwarded with its original bytes otherwise intact. The
   response (including faults) is returned verbatim and `arr` is done.
6. A Starlette `BackgroundTask` runs after the response is sent. It enqueues a
   `GrabJob` only if the upstream response was not a fault.
7. A worker waits for the hash to appear, resolves the destination, calls
   `d.directory.set` then `d.start`.
8. The completion watcher polls `d.complete`; when true it posts
   `RescanMovie` / `RescanSeries` / `RescanFolders`.
9. The app scans the folder with `newDownload=false`, records the existing
   file, and never moves or renames it.

## State machine

```text
RECEIVED → LOADED → RESOLVED → PLACED → STARTED → COMPLETE → ADOPTED → DONE
     │        │          │         │                     │
     │        │          │         └────────────────────► FAILED_OPEN
     │        │          └──────────────────────────────► FAILED_OPEN
     │        └─────────────────────────────────────────► FAILED
     └──────────────────────────────────────────────────► FAILED
`DRY_RUN` is terminal for the run and performs no retarget or start. Turning
`DRY_RUN` off makes the reconciler re-enqueue the row so the paused torrent is
placed on the next pass.
```

- `FAILED_OPEN` means seedpath gave up and called `d.start` in the client's
  own directory. A torrent is never left paused without an owner.
- `FAILED` means the infohash never appeared in rTorrent; there is nothing
  seedpath can start.
- States are persisted before the next outbound call, so a crash resumes at
  the last completed step.

## Concurrency model

- One uvicorn process. ASGI handlers are async; no blocking I/O on the loop.
- `PlacementWorker` runs `WORKER_COUNT` consumers plus a completion watcher
  and a reconciler. A per-infohash in-flight set prevents duplicate work.
- State writes are serialized by an `asyncio.Lock`; SQLite runs in WAL mode.
- The request path never waits for an *arr API call. The only work before the
  response is the upstream XML-RPC forward itself.

## Persistence

One SQLite database at `STATE_PATH`:

| column | meaning |
|---|---|
| `infohash` | primary key, lowercase hex |
| `state` | one of the states above |
| `category` | rTorrent category that selected the app |
| `release_name` | `info.name` or magnet `dn` |
| `app`, `app_item_id` | owning *arr instance and item |
| `dest` | resolved library folder |
| `error` | last failure reason |
| `created_at`, `updated_at` | ISO-8601 UTC |

Schema version is stored in `PRAGMA user_version`; a newer database is refused
rather than silently migrated.

## Failure handling

| situation | behaviour |
|---|---|
| History row not yet written | retry with exponential backoff up to `RESOLVE_TIMEOUT` |
| Resolution impossible / app down | fail open: `d.start`, log, `FAILED_OPEN` |
| Destination outside `LIBRARY_ROOTS` | refuse, fail open, log loudly |
| Destination folder missing | rescan command to create it, wait, retry `d.directory.set` once, then fail open |
| `d.directory.set` fault (hash not loaded) | retried once after the folder recovery path |
| Duplicate add (rTorrent fault) | forwarded untouched; no worker is run |
| Proxy restart mid-flight | resumable rows are re-enqueued; paused incomplete torrents with a mapped category are swept |
| Torrent removed in `arr` | state row dropped on the next reconcile |
| Re-grab of the same infohash | a successful new load restarts a terminal/started row and resolves it again |
| Torrent started before placement | never retargeted; recorded as `FAILED_OPEN` |
| Rescan rejected | logged with the API body; the next scheduled refresh still adopts the file |

## Deliberate deviations and limits

- **HTTP `.torrent` URLs are not retargeted in v1.** `arr` normally sends
  magnet links or raw `.torrent` bytes. When the payload is a plain URL, the
  infohash is not derivable without fetching it, so seedpath forwards the load
  unchanged and logs a warning. See `docs/roadmap.md`.
- **The reconciler only sweeps paused, incomplete torrents.** rTorrent reports
  paused items as open/inactive (`state=1`, `is_active=0`); started and
  seeding items are `is_active=1` and are never retargeted. Closed items
  (`state=0`) are left alone because they may have been stopped deliberately.
- **Single process only.** In-memory queues and SQLite make multiple replicas
  unsafe without an external queue and shared state.
- **No filesystem access.** There is no `mkdir`, `stat` or hardlink path in
  the codebase by design. Path existence is checked through the app's scan
  command, not the local filesystem.

## Security boundaries

- Ingress is optional Basic auth, using the same credentials as rTorrent.
- Ingress is not meant to be published to the host.
- XML parsing is limited to internal-network peers, rejects `DOCTYPE`/`ENTITY`
  declarations, and caps body size. `xmlrpc.client` remains a stdlib parser
  and is not a general-purpose hardened XML stack.
- API keys are `SecretStr` and are redacted from `seedpath config`.
- The container runs as UID 10001 with a read-only root filesystem and no
  capabilities; only `/config` is writable.
