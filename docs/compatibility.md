# Compatibility

## Servarr

| app | API prefix | destination source | rescan command |
|---|---|---|---|
| Radarr | `/api/v3` | `movie.path` | `RescanMovie` |
| Whisparr | `/api/v3` | `movie.path` | `RescanMovie` |
| Sonarr | `/api/v3` | `series.path` (+ `Season NN`) | `RescanSeries` |
| Lidarr | `/api/v1` | `artist.path` | `RescanFolders` |
| Readarr | `/api/v1` | `author.path` | `RescanFolders` |

All findings this project depends on were read from the `develop` branches of
Radarr and Sonarr. No specific release has completed the acceptance run yet.
Pin the versions you validate in your own deployment and open an issue with the
app version if a field changes.

Relevant API resources:

- `GET /history?downloadId=<hash>&eventType=1` (Grabbed = 1)
- `GET /movie/{id}`, `/series/{id}`, `/artist/{id}`, `/author/{id}`
- `GET /episode/{id}` (only when Sonarr season folders are enabled and the
  history record lacks `seasonNumber`)
- `GET /parse?title=<name>` fallback
- `POST /command` with `RescanMovie`, `RescanSeries` or `RescanFolders`

## rTorrent

| requirement | notes |
|---|---|
| XML-RPC endpoint | usually `/RPC2`; Basic auth supported |
| `d.start` | rTorrent 0.9.6+; all supported ruTorrent images have it |
| `d.multicall2` | used for the queue poll and reconciler |
| `d.directory.set`, `d.base_path` semantics | as documented in `BRIEF.md` §5.3 |
| `network.xmlrpc.size_limit` | raise it for large libraries; the queue poll is 12 fields × every torrent |
| methods proxied | `load.*`, `d.*`, `system.client_version`; unknown methods pass through |

seedpath does not require a particular rTorrent build or patch set.

## Browsers / clients

Only *arr's rTorrent download client is in scope. ruTorrent's own UI does not
talk to seedpath.

## Python

- Minimum: 3.12.
- Tested in CI on 3.12, 3.13 and 3.14.
- The container is built on `python:3.12-alpine`.

## Docker

- Compose file format: modern Compose v2 (`docker compose`).
- Image supports `linux/amd64` and `linux/arm64`.
- The image is stateless except `/config`.

## Known limitations in v1

- HTTP `.torrent` URLs are forwarded but not retargeted (no infohash until the
  client fetches them).
- One instance of seedpath per rTorrent; no horizontal scaling.
- No usenet client, no cross-seed injection, no moving existing torrents.
- No upgrade automation; the old release must be removed manually.
- No support for rTorrent's `<hash>.meta` magnet edge cases beyond normal
  paused magnet loads. If a magnet never materialises as a hash, it is marked
  `FAILED` and the operator sees it.
