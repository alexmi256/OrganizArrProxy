# Servarr setup

These are not preferences; seedpath's design depends on every one of them.
`seedpath preflight` checks all the API-visible settings and exits non-zero on
failure.

## Global settings

### Settings → Media Management

| setting | value | why |
|---|---|---|
| Completed Download Handling | **off** | otherwise the importer moves/renames the seeding file |
| Create empty movie/series/artist/author folders | **on** | creates the destination before rTorrent starts |
| Delete Empty Folders | **off** | the scan must be able to create the folder |
| Set Permissions | **on** | without it `chmodFolder` is ignored |
| Folder chmod | **775** | rTorrent (different user) must write inside it |
| Import Extra Files | **off** | keeps `.nfo`, `.srt` in the release folder |
| Rename Movies/Episodes/Tracks/Books | **off** | a rename removes the file from the torrent |
| Auto Rename Folders (Radarr) | **off** | renames the folder the torrent seeds from |

### Settings → Download Clients → rTorrent

| field | value |
|---|---|
| Enable | on |
| Host | `seedpath` |
| Port | `8080` |
| URL Base | `/RPC2` |
| Username / Password | same as rTorrent's |
| Category | per app (`radarr`, `tv-sonarr`, `lidarr`, `readarr`, `whisparr`) |
| Directory | empty |
| Add Stopped | **on** |

Do not add a Remote Path Mapping. If you need one, the containers do not agree
on paths and placement will be wrong; fix the mount instead.

## App-by-app

### Radarr / Whisparr

- Root folder: inside `LIBRARY_ROOTS`.
- `chmodFolder` is `775` (`0775` is accepted by preflight).
- `createEmptyMovieFolders` on.
- `autoRenameFolders` off.
- Category default: `radarr` / `whisparr`.

### Sonarr

- Season folders are a **per-series** setting (`seasonFolder`), not global.
  When it is on, seedpath places the release in `<series>/Season NN`; when it
  is off, the destination is the series folder.
- If season folders are on, seedpath obtains the season from the grab history
  or, if absent, from the episode resource.
- `createEmptySeriesFolders` on.
- Category default: `tv-sonarr`.

### Lidarr

- `createEmptyArtistFolders` on.
- Rescan uses `RescanFolders` with the artist folder.
- Category default: `lidarr`.

### Readarr

- `createEmptyAuthorFolders` on.
- Rescan uses `RescanFolders` with the author folder.
- Category default: `readarr`.

## rTorrent / ruTorrent

- seedpath talks to the XML-RPC endpoint directly (for ruTorrent usually
  `http://rtorrent:8000/RPC2`, optionally behind nginx with Basic auth).
- rTorrent must be able to create the destination folder. The app creates the
  item folder; rTorrent creates the release folder inside it.
- Increase `network.xmlrpc.size_limit` if you have very many torrents. The
  queue poll asks for twelve fields for every torrent every minute. A 1 MB
  limit may truncate large responses; 4–8 MB is a common safe value.
- Do not use ruTorrent's "remove and delete data" on a placed torrent: its
  data **is** the library file.

## Verification

```bash
docker compose exec seedpath seedpath preflight
```

Expected output ends with `preflight passed`. Every `fail` names the exact
setting; `warn` entries are advisory (for example an empty `LIBRARY_ROOTS`).

Then perform one grab per shape:

1. **Single-file movie** — lands at `<movie folder>/<release>.mkv`, seeds, and
   appears in Radarr after the rescan.
2. **Folder release** — lands at `<movie folder>/<Release>/…` with `.nfo` and
   subtitles intact.
3. **TV episode** — lands in the series folder (or `Season NN` when enabled).

Check the hash in the logs:

```bash
docker compose logs seedpath | grep <infohash>
```

You should see states in order: `loaded → resolved → placed → started`, then
`complete → adopted → done`, and no `fail_open`.

## Things the API cannot verify

Preflight is read-only. Three properties still need a real grab:

1. rTorrent can write inside an *arr-created folder (permissions/umask).
2. Both containers see the same absolute path.
3. The retarget took effect — compare the app's queue `outputPath` with the
   `dest` in the seedpath log.

## Rolling back

Turn **Add Stopped** off, point the client back at rTorrent, and remove
seedpath. Torrents already placed keep seeding from the library; their files
are the library files.
