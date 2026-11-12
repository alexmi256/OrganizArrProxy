# Operations and troubleshooting

## Logs

Logs are structured (JSON by default). Every lifecycle line carries the
infohash, so a single torrent can be followed across workers:

```bash
docker compose logs seedpath | grep d3217788d25911116715890b10eadf9c01d9b7c9
```

Typical sequence:

```json
{"event":"proxy.load_accepted","infohash":"…","method":"load.raw_start","paused_as":"load.raw","category":"radarr"}
{"event":"torrent.state","infohash":"…","state":"loaded"}
{"event":"torrent.state","infohash":"…","state":"resolved"}
{"event":"torrent.state","infohash":"…","state":"placed"}
{"event":"torrent.state","infohash":"…","state":"started"}
```

Set `LOG_FORMAT=console` for human-readable local development.

## Metrics

`GET /metrics` exposes:

| metric | labels | meaning |
|---|---|---|
| `seedpath_grabs_total` | `category` | accepted load calls |
| `seedpath_placements_total` | `app`, `outcome` | `placed`, `fail_open`, `failed`, `dry_run` |
| `seedpath_retarget_seconds` | `app` | load → `d.start` latency |
| `seedpath_resolve_failures_total` | `app` | history/parse failures |
| `seedpath_rescans_total` | `app`, `outcome` | rescan command results |
| `seedpath_worker_queue_depth` | — | pending placements |
| `seedpath_active_torrents` | — | torrents not yet adopted |

Useful alerts: `rate(seedpath_placements_total{outcome="fail_open"}[1h]) > 0`
and `seedpath_worker_queue_depth > 50`.

Rows re-enqueued by the reconciler after downtime use their original
`created_at` as `received_at`, so `seedpath_retarget_seconds` includes the
outage and will show large outliers after a restart. Compare against
`seedpath_placements_total` before treating those as slow placements.

## Health

- `/livez` — process only; safe for the container healthcheck.
- `/healthz` — checks every dependency and returns 503 on failure:

  ```json
  {"status":"ok","version":"0.1.0","checks":[{"name":"rtorrent","ok":true,"detail":"0.9.8"}]}
  ```

Health uses `system.client_version` and each app's `/system/status`; it does
not run the full preflight on every poll.

## Inspecting state

```bash
docker compose exec seedpath python - <<'PY'
import sqlite3, os
db = sqlite3.connect(os.environ["STATE_PATH"])
for row in db.execute("select infohash,state,app,dest,error from torrents order by updated_at desc limit 20"):
    print(row)
PY
```

Rows are kept while the torrent exists. The reconciler deletes rows for
torrents that are no longer present in rTorrent; delete `STATE_PATH` to start
fresh. That does not touch media or torrents.

## Recovery

| situation | action |
|---|---|
| Torrent paused forever, `FAILED_OPEN` | it should have been started. Check rTorrent reachability and `/healthz`; start it manually, then investigate the error field |
| `FAILED` (hash never appeared) | rTorrent rejected or has not loaded it; check its log and the original 'save' directory |
| Wrong destination | compare the app's item path with `LIBRARY_ROOTS`; check that both containers use the same absolute path |
| Rescan rejected | the file stays where it is; the app's scheduled refresh adopts it within 24 h. Fix the app's API key/permissions |
| Proxy restarted mid-grab | the reconciler resumes resumable rows and sweeps paused incomplete torrents with mapped categories |
| Duplicate add | rTorrent's fault is returned to the app unchanged; no retarget occurs |

## Troubleshooting matrix

| symptom | likely cause |
|---|---|
| `arr` says "Unable to connect" | wrong host/port, `/RPC2` missing, or Basic auth mismatch |
| Loads succeed but torrents stay paused | category not in `CATEGORY_MAP`, missing app credentials, or Add Stopped without a worker |
| No `dest` in logs | `RESOLVE_TIMEOUT` too short while *arr writes history; check app API key |
| `outside LIBRARY_ROOTS` | root folder not listed in `LIBRARY_ROOTS`, or path mapping mismatch |
| `d.directory.set` faults repeatedly | destination folder missing and scan cannot create it (permissions or create-empty-folders off) |
| Two copies still appear | this design should prevent it; check that CDH is off and the download client is seedpath, not rTorrent directly |
| Queue poll times out | `network.xmlrpc.size_limit` too low for the number of torrents |
| 401 from seedpath | `arr` credentials differ from `RT_USER`/`RT_PASS`; or set `INGRESS_AUTH=false` on a trusted network |

## The one destructive mistake

The torrent's data **is** the library file. In ruTorrent, "remove torrent and
delete data" deletes the library file. Always remove the torrent only. This is
worth pinning in your operator notes and in any ruTorrent theme/docs.

## Backups

Back up `config/state.db` (and its `-wal`/`-shm` files while stopped). Media is
not backed up by seedpath; use your normal library backup. State loss is
recoverable: unfinished torrents are swept and placed again on the next
reconcile only if they are stopped and incomplete.

## Security checklist

- [ ] No published ports; internal network only.
- [ ] `read_only`, `cap_drop: ALL`, `no-new-privileges`.
- [ ] `.env` is `chmod 600` and not committed.
- [ ] API keys rotated when a container image or config is shared.
- [ ] Ingress Basic auth left on unless a trusted reverse proxy authenticates.
- [ ] Logs shipped somewhere access-controlled; they contain paths and hashes.
