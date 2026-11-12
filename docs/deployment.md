# Deployment

## Prerequisites

- An existing Docker Compose stack with rTorrent (or ruTorrent) and at least
  one *arr app.
- **Identical absolute paths** inside the *arr and rTorrent containers. The
  library root that Radarr calls `/data/movies` must be the same `/data/movies`
  rTorrent writes to.
- A shared group and `umask 002` so rTorrent can create the release folder
  inside a directory created by *arr. In rTorrent: `system.umask.set = 0002`.
- The *arr app must be able to create empty item folders with mode `775`.

See [Servarr setup](servarr-setup.md) for the settings.

## Compose

[`compose.yaml`](../compose.yaml) is a complete example. Copy it next to your
stack (or merge the service), create the network if it does not exist, and
fill `.env`:

```bash
cp .env.example .env
docker network create arr_stack   # only if your stack does not have one
mkdir -p config && sudo chown 10001:10001 config
docker compose up -d --build
```

The bind-mounted `config` directory must be writable by UID 10001 because the
container is non-root. A named volume avoids the ownership step:

```yaml
volumes:
  - seedpath-config:/config
volumes:
  seedpath-config:
```

Use `image: ghcr.io/alexmi256/organizarrproxy:latest` instead of `build: .` once an
image is published.

The service deliberately publishes no ports. It holds API keys and download
credentials; keep it on the internal network. To reach it from another
compose project, mark that project's network `external: true` with the same
name.

### Why `read_only` and `no-new-privileges`

The daemon never writes outside `/config`. The example therefore sets:

- `read_only: true` with a `/tmp` tmpfs,
- `cap_drop: [ALL]`,
- `security_opt: [no-new-privileges:true]`,
- a non-root user (UID 10001) baked into the image.

If you use Docker secrets mounted as files, add a read-only mount; don't
switch the container back to root.

## Point the apps at it

In each app: **Settings → Download Clients → rTorrent** (the existing entry):

| field | value |
|---|---|
| Host | `seedpath` |
| Port | `8080` |
| URL Base | `/RPC2` |
| Username / Password | the rTorrent credentials (the proxy validates and forwards them) |
| Category | the app's category, e.g. `radarr` |
| Add Stopped | **on** |
| Directory | empty |

Do not create a second download client. Edit the existing one so old queue
items keep their category.

## Permissions recipe

On the host or in the arr container:

```bash
chgrp -R media /data
chmod -R g+rwX /data
```

In each *arr app: **Media Management → Set Permissions** on, folder chmod
`775`. In rTorrent's config:

```ini
system.umask.set = 0002
```

If `fs.protected_hardlinks=1` is set, it is irrelevant here — seedpath does
not create hardlinks. The requirement is ordinary directory write permission
for the rTorrent user.

## Health and shutdown

- `GET /livez` — process is up (used by the Docker healthcheck).
- `GET /healthz` — rTorrent version and every configured app's
  `/system/status`; returns 503 when any dependency is down.
- `GET /metrics` — Prometheus exposition.
- Compose sends SIGTERM; the workers cancel, the state database closes and
  uvicorn drains. `stop_grace_period` is 30 s in the example.

## Upgrades

- Tagged images use semver (`ghcr.io/alexmi256/organizarrproxy:0.2.0`); `latest`
  follows the newest release, `edge` follows `main`.
- Back up the state database before upgrading:

  ```bash
  docker compose stop seedpath
  cp config/state.db config/state.db.backup
  docker compose up -d seedpath
  ```

- A database written by a newer schema is refused rather than migrated down.
  Roll back the image to match.
- After upgrading, run `seedpath preflight` and watch the first grab's log
  lines by infohash.

## Multi-instance apps

Radarr 4K + Radarr HD, or two Sonarr instances, need distinct categories and
distinct instance names:

```bash
APPS_JSON='[
 {"name":"radarr","kind":"radarr","url":"http://radarr:7878","api_key":"..."},
 {"name":"radarr4k","kind":"radarr","url":"http://radarr4k:7878","api_key":"..."}
]'
CATEGORY_MAP='{"radarr":"radarr","radarr4k":"radarr4k"}'
```

Each download client entry uses its own category. Preflight checks that each
app's configured client category matches `CATEGORY_MAP`.

## Resource use

The image is Alpine + a Python virtualenv, roughly 150 MB. Runtime memory is
dominated by Python and the XML-RPC payloads, typically well under 128 MB for
a few hundred torrents. A `mem_limit: 256m` and `cpus: 0.5` are generous
starting points.

## TLS

Everything normally stays on an internal Docker network. If your rTorrent
endpoint is HTTPS with a private CA, mount the CA bundle and set
`SSL_CERT_FILE`; set `RT_VERIFY_TLS=false` only as a temporary debugging step.

## Uninstalling

1. Set **Add Stopped** off in each app.
2. Point the download client back at rTorrent.
3. Remove the seedpath service.
4. Keep `config/state.db` until the current queue drains, or delete it if you
   do not intend to roll back. Deleting state never deletes media: seedpath
   does not own the files.
