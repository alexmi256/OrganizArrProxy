# seedpath

**One file. One path. In the library, and seeding from there.**

`seedpath` is a small XML-RPC proxy that sits between the Servarr apps
(Radarr, Sonarr, Lidarr, Readarr, Whisparr) and rTorrent/ruTorrent. It pauses
each grab at add time, asks the *arr app where the release belongs, tells
rTorrent to download directly into that library folder, and then asks the app
to **scan** the folder instead of importing the download.

The result: the media server reads the same file the torrent seeds. No
hardlinks, no second copy, no filesystem-policy roulette.

> **Status: pre-alpha scaffold.** The protocol core, configuration, state
> machine, proxy, health endpoint, preflight and test suite exist. Real-stack
> acceptance (BRIEF.md §12) has not been run yet. Do not point this at a
> library you care about until that is done.

`seedpath` is the working name from the brief; the repository is
`OrganizArrProxy`. Renaming is a packaging-only change (package, image name,
docs).

## Why

A normal Servarr setup stores every release twice: the download client's copy
keeps seeding, and the importer creates a second copy in the library. The
usual answer is hardlinks, which silently degrades to a full copy when the
filesystems differ, when a union filesystem uses a path-preserving policy, or
when permissions deny `link()`.

`seedpath` removes the transfer entirely. Nothing is linked; nothing can
silently become a copy. `arr` is reduced to the librarian that records what
is already on disk.

See [BRIEF.md](BRIEF.md) for the source-level analysis behind the design.

## How it works

```text
arr ── load.raw_start / load.start ──► seedpath ── paused load ──► rTorrent
                                        │
                                        │ after the response is sent
                                        ▼
                              resolve via arr REST API
                                        │
                              d.directory.set(hash, library folder)
                              d.start(hash)
                                        │
                         rTorrent downloads + seeds in place
                                        │
                              d.complete == 1 → RescanMovie/Series/Folders
                                        │
                              arr scans and adopts the file in place
```

The proxy forwards everything else byte for byte: duplicate-add faults, the
`d.multicall2` queue poll, and binary `.torrent` payloads are never reshaped.
Only `load.start` → `load.normal` and `load.raw_start` → `load.raw` are
rewritten, and only for payloads whose infohash seedpath can derive.

### Why Starlette?

The hard requirement is XML-RPC fidelity, not JSON schema validation. seedpath
keeps the standard library's `xmlrpc.client` for marshalling and uses
Starlette/uvicorn for the HTTP surface: lifespan-managed workers, health,
metrics and graceful shutdown. FastAPI's value — typed JSON parsing, DI and
OpenAPI — does not apply to a body that must be forwarded verbatim, and stdlib
`xmlrpc.server` lacks a production HTTP lifecycle and encourages typed
unmarshalling of everything. The full comparison, with sources, is in
[ADR 0001](docs/adr/0001-python-framework.md).

## Quick start

```yaml
# compose.yaml
services:
  seedpath:
    image: ghcr.io/alexmi256/organizarrproxy:latest
    container_name: seedpath
    restart: unless-stopped
    environment:
      RT_URL: http://rtorrent:8000/RPC2
      RT_USER: rtorrent
      RT_PASS: ${RT_PASS}
      RADARR_URL: http://radarr:7878
      RADARR_API_KEY: ${RADARR_API_KEY}
      SONARR_URL: http://sonarr:8989
      SONARR_API_KEY: ${SONARR_API_KEY}
      LIBRARY_ROOTS: '["/data/movies","/data/tv"]'
      CATEGORY_MAP: '{"radarr":"radarr","tv-sonarr":"sonarr"}'
      STATE_PATH: /config/state.db
      LOG_FORMAT: json
    volumes:
      - ./config:/config
    networks: [arr_stack]
    security_opt: [no-new-privileges:true]
    read_only: true
    tmpfs: [/tmp]
```

There are no published ports. Then, in each app, point the rTorrent download
client at `http://seedpath:8080/RPC2`, keeping the same category and
credentials.

Before letting it place anything, run the self-check:

```bash
docker compose exec seedpath seedpath preflight
```

It verifies every setting seedpath depends on: completed download handling
off, empty folders created, renaming off, `addStopped` on, no remote path
mappings, categories mapped, and root folders inside `LIBRARY_ROOTS`.

## Required *arr settings

| Setting | Required | Why |
|---|---|---|
| Completed Download Handling | **off** | the importer would move the seeding file |
| Create empty movie/series folders | **on** | creates the destination before rTorrent starts |
| Delete empty folders | **off** | a scan must be able to create the folder |
| Set permissions (`setPermissionsLinux`) | **on** | without it `chmodFolder` is ignored |
| `chmodFolder` | **775** | rTorrent runs as a different user |
| Rename movies/episodes/tracks/books | **off** | a rename removes the file from the torrent |
| Import extra files | **off** | keeps `.nfo`/`.srt` in the release folder |
| Auto rename folders (Radarr) | **off** | renames the folder being seeded |
| Download client: Add Stopped | **on** | no bytes before retargeting |
| Your torrent client's Category | per app | how seedpath chooses the app |
| Remote Path Mappings | **none** | container paths must agree |

Full per-app instructions: [docs/servarr-setup.md](docs/servarr-setup.md).

## ⚠️ Operational hazard

The torrent's data **is** the library file. Removing a torrent *and deleting
its data* deletes the library file. In ruTorrent, remove the torrent only —
never "remove and delete data".

## Configuration

Environment variables only; see [.env.example](.env.example) and
[docs/configuration.md](docs/configuration.md). The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `RT_URL` | `http://rtorrent:8000/RPC2` | rTorrent XML-RPC endpoint |
| `RT_USER` / `RT_PASS` | empty | Basic auth, used upstream and for ingress |
| `RADARR_URL` / `RADARR_API_KEY` | unset | one pair per configured app |
| `APPS_JSON` | unset | multiple instances / custom names |
| `LIBRARY_ROOTS` | `[]` | JSON array; placement is refused unless set |
| `CATEGORY_MAP` | per-app defaults | JSON object: category → app instance |
| `RESOLVE_TIMEOUT` | `10s` | history/parse retry budget |
| `RESCAN_POLL` | `30s` | completion poll interval |
| `DRY_RUN` | `false` | resolve and log, never retarget or start |
| `STATE_PATH` | `./data/state.db` | SQLite state file |

## Development

Requires [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just).

```bash
just install     # uv sync
just check       # format check + ruff + mypy + pytest
just run         # start on :8080
just preflight   # run the config self-check
just docker      # build the image
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for the
architecture map and conventions.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Deployment](docs/deployment.md)
- [Servarr setup](docs/servarr-setup.md)
- [Operations and troubleshooting](docs/operations.md)
- [Compatibility](docs/compatibility.md)
- [Testing and acceptance](docs/testing.md)
- [Roadmap](docs/roadmap.md)
- [ADRs](docs/adr/README.md) — including why Starlette rather than FastAPI or
  stdlib `xmlrpc.server`

## Licence

MIT. See [LICENSE](LICENSE).
