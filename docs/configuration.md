# Configuration

Everything is an environment variable. `seedpath config` prints the effective
values with secrets redacted; `seedpath --preflight` verifies the *arr side.

## rTorrent

| Variable | Default | Notes |
|---|---|---|
| `RT_URL` | `http://rtorrent:8000/RPC2` | Full endpoint, including `/RPC2` |
| `RT_USER` | empty | Basic auth user; also validates ingress when set |
| `RT_PASS` | empty | Basic auth password |
| `RT_VERIFY_TLS` | `true` | Set false only for self-signed HTTPS |
| `UPSTREAM_TIMEOUT` | `30s` | Timeout for every outbound rTorrent/arr call |
| `LOAD_TIMEOUT` | `30s` | How long to wait for a new infohash to appear |

If rTorrent is behind an nginx that uses a different Basic auth pair than the
one configured in *arr, put a reverse proxy in front of seedpath or disable
ingress auth and rely on the internal network. By default the proxy accepts
exactly the `RT_USER`/`RT_PASS` credentials and presents them upstream.

## *arr instances

Set `URL` and `API_KEY` together for each app:

```bash
RADARR_URL=http://radarr:7878
RADARR_API_KEY=...
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=...
LIDARR_URL=http://lidarr:8686
LIDARR_API_KEY=...
READARR_URL=http://readarr:8787
READARR_API_KEY=...
WHISPARR_URL=http://whisparr:6969
WHISPARR_API_KEY=...
```

API versions are derived: Radarr/Sonarr/Whisparr use `/api/v3`, Lidarr/Readarr
use `/api/v1`.

### Multiple instances or custom names

`APPS_JSON` is a JSON array. It is merged with the convenience variables:

```bash
APPS_JSON='[
  {"name":"radarr4k","kind":"radarr","url":"http://radarr4k:7878","api_key":"..."},
  {"name":"radarr","kind":"radarr","url":"http://radarr:7878","api_key":"..."}
]'
```

Instance names must be unique. The name is what `CATEGORY_MAP` points at.

## Categories

`CATEGORY_MAP` is a JSON object mapping rTorrent category (`d.custom1`) to an
app instance name. It is how a grab chooses its owner.

```bash
CATEGORY_MAP='{"radarr":"radarr","tv-sonarr":"sonarr","radarr4k":"radarr4k"}'
```

Default categories are derived from the configured apps:

| app | default category |
|---|---|
| Radarr | `radarr` |
| Sonarr | `tv-sonarr` |
| Lidarr | `lidarr` |
| Readarr | `readarr` |
| Whisparr | `whisparr` |

Every mapped app must be configured, and every category must be unique. A
category that is not in the map is forwarded unchanged and logged; seedpath
will not pause a torrent it cannot place.

## Placement policy

| Variable | Default | Notes |
|---|---|---|
| `LIBRARY_ROOTS` | `[]` | JSON array. Empty fails closed: no placement is allowed |
| `DRY_RUN` | `false` | Resolve and log; no `d.directory.set`, no `d.start` |
| `RESOLVE_TIMEOUT` | `10s` | History retry budget before parse fallback |
| `RESCAN_POLL` | `30s` | Completion poll and rescan interval |
| `RECONCILE_INTERVAL` | `300s` | Restart/recovery sweep interval |

Durations accept a bare number of seconds or `500ms`, `10s`, `1m`, `2h`, `1d`.

Example:

```bash
LIBRARY_ROOTS='["/data/movies","/data/tv","/data/music","/data/books"]'
```

Paths are POSIX paths as seen **inside both containers**. seedpath only does
lexical normalization and prefix checks; it does not resolve symlinks.

## HTTP and operations

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address inside the container |
| `PORT` | `8080` | Internal port |
| `RPC_PATH` | `/RPC2` | Path pointed at by the *arr download client |
| `INGRESS_AUTH` | `true` | Validate Basic auth when `RT_USER` is set |
| `MAX_BODY_BYTES` | `16777216` | 16 MiB ingress request limit |
| `STATE_PATH` | `./data/state.db` | SQLite path; image uses `/config/state.db` |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `WORKER_COUNT` | `2` | Placement consumers |
| `WORKER_QUEUE_SIZE` | `1000` | Bounded queue; overflow fails open |

## Secrets

Use Docker secrets or a `.env` file excluded from version control. The image
does not bake any secret in, `seedpath config` redacts them, and logs never
print the values. Avoid putting API keys on the command line (visible in
`ps`); use the environment.

## Example `.env`

See [`.env.example`](../.env.example) for a copy-paste template.
