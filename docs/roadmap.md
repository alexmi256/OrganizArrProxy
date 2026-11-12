# Roadmap

## v0.1 (scaffold, current)

- [x] XML-RPC ingress with raw passthrough and paused-load rewrite
- [x] Infohash extraction from `.torrent` bytes and magnets (hex/base32)
- [x] SQLite state machine with restart recovery
- [x] History-first resolution with parse fallback
- [x] Radarr/Sonarr/Lidarr/Readarr/Whisparr adapters
- [x] Placement worker, completion watcher, reconciler
- [x] Fail-open behaviour and root containment
- [x] Preflight, health, metrics, structured logging
- [x] Docker image, compose example, CI, release workflows
- [x] Unit/integration suite (80%+)

## Before a stable release

- [ ] Run the full real-stack acceptance checklist in `docs/testing.md`
- [ ] Pin and document verified Servarr/rTorrent versions
- [ ] Decide the operational UX for `FAILED_OPEN` (metric exists; add a CLI
      audit command?)
- [ ] Load-test the queue poll with a few thousand torrents and set a
      recommended `network.xmlrpc.size_limit`
- [ ] Confirm arm64 image works on a real NAS
- [ ] Add a `seedpath doctor` command that checks rTorrent write permissions
      and path agreement with a probe torrent

## Open design questions from the brief

1. **Upgrades.** v1 documents "delete the old folder by hand". A flag that
   deletes the old `moviefile` and old torrent before placement is designed
   but not implemented.
2. **Magnet adds.** Supported when the infohash is derivable. The paused
   `<hash>.meta` path is treated like any other torrent; a magnet whose hash
   never appears becomes `FAILED`, visible in logs/metrics.
3. **Multi-instance apps.** Implemented through `APPS_JSON` and per-instance
   categories. Preflight checks category agreement, not global uniqueness
   across containers (impossible from one app's API).
4. **Staging disk.** Out of scope for v1. The state machine has `PLACED` before
   `STARTED`, so a future mover can insert a staging rename there.
5. **Naming collisions.** Two grabs resolving to the same folder are allowed;
   rTorrent will place them side by side. v1 does not refuse or suffix.
6. **HTTP `.torrent` URLs.** Not retargeted because the infohash is unknown
   until rTorrent fetches the file. Options: fetch the URL in seedpath (new
   outbound dependency), or ask the user to use magnet links.

## Explicit non-goals

- Modifying *arr or rTorrent.
- Usenet clients, cross-seed injection, transcoding, quality upgrades.
- Migrating or moving an existing library.
- Filesystem tricks (hardlinks, reflinks, unionfs policies).
