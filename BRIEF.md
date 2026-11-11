# Project brief — `seedpath`

**A proxy that makes *arr grabs download directly into the media library and seed from there.**

Working name: `seedpath` (placeholder — rename freely). This brief is self-contained: it explains the
problem, the constraints discovered in the Servarr and rTorrent source, the design that follows from
them, the exact protocols involved, and what "done" means. Someone who has never seen this project
should be able to implement it from this document.

Status: **design complete, nothing implemented.** Language recommendation in §13.

---

## 1. Problem

A typical Servarr stack (Radarr/Sonarr/Lidarr/Readarr + rTorrent/ruTorrent + Prowlarr) produces
**two copies of every release**:

1. the download client's copy, in the download directory, which keeps seeding;
2. the library copy, in the media folder, which the media server reads.

The standard answer is hardlinks, which works only when both paths are on the same filesystem, are
exposed to the container through a single mount point, sit on the same branch of a union filesystem,
and the importing user can write the source file. Get any of those four wrong and the import
silently degrades to a **full copy** (see §3.1). Union filesystems and multi-disk pools make this
fragile, and even when it works it leaves two names for every file.

**Goal:** one file, at one path, in the library, and that same file is what the torrent seeds.
`arr` stops being the importer and becomes a librarian that records what is already on disk.

That is achievable — but not through any configuration of `arr` alone, because `arr`'s importer is
structurally incapable of it (§3.2). It requires an external component that places the release and
then asks `arr` to *scan* the folder instead of importing the download.

`seedpath` is that component: a small daemon that sits between the *arr apps and rTorrent as an
XMLRPC proxy, learns the destination folder from the *arr API, and retargets each new torrent
before it starts downloading.

---

## 2. Naming and scope

- **In scope:** torrents grabbed by Radarr, Sonarr, Lidarr, Readarr (and Whisparr, which is a Radarr
  fork) through rTorrent/ruTorrent; placement into the library; triggering the rescan that makes
  `arr` adopt the file; a preflight check of the `arr` configuration.
- **Out of scope (v1):** usenet clients, cross-seed injection, transcoding, automating quality
  upgrades, migrating an existing library, any moving of already-downloaded torrents.
- **Explicit non-goal:** modifying `arr` or rTorrent. Everything happens over their public APIs.

---

## 3. Why `arr` cannot do this by itself (the findings that define the design)

All of the following was read from current `develop` sources of Radarr and Sonarr. Paths are given
so the implementer can verify them.

### 3.1 Hardlink failure is silent, and falls back to copying

`src/NzbDrone.Common/Disk/TransferMode.cs`:

```csharp
[Flags] public enum TransferMode { None = 0, Move = 1, Copy = 2, HardLink = 4, HardLinkOrCopy = Copy | HardLink }
```

`src/NzbDrone.Common/Disk/DiskTransferService.cs`, `TransferFile()`:

```csharp
if (mode.HasFlag(TransferMode.HardLink)) {
    var createdHardlink = _diskProvider.TryCreateHardLink(sourcePath, targetPath);
    if (createdHardlink) return TransferMode.HardLink;
    if (mode.HasFlag(TransferMode.Copy))
        _logger.Warn("Hardlinking from '{0}' to '{1}' failed, fallback to copy.", ...);
    ...
}
```

Diagnosis of a duplicate-storage complaint therefore starts with `grep "fallback to copy"` in the
app's log, not with theory. Causes, in the order they usually appear:

| cause | detail |
|---|---|
| different filesystems | `link()` → `EXDEV` |
| two sibling bind mounts of the same pool in one container | mergerfs docs: linking between two mounts of the same pool fails with `EXDEV` even though it works on the host. Mount a common ancestor once |
| path-preserving create policy on a union fs (`epmfs`, `epff`, `eplfs`, `eplus`, `msp`) | mergerfs must return `EXDEV` when the link target's directory lives on a different branch. Use `mfs`/`pfrd` |
| `fs.protected_hardlinks=1` + no write bit on the source | `link()` → `EPERM`; needs a shared group and umask 002 (files `0664`) |

### 3.2 The importer transfers and renames; the scanner does not

`src/NzbDrone.Core/MediaFiles/MovieImport/ImportApprovedMovie.cs`:

```csharp
if (newDownload)
{
    oldFiles = _movieFileUpgrader.UpgradeMovieFile(movieFile, localMovie, copyOnly).OldFiles;   // moves + renames
}
else
{
    // delete stale DB rows only
}
movieFile = _mediaFileService.Add(movieFile);                                                   // records whatever exists
```

- A **completed download** arrives with `newDownload: true` → `MovieFileMovingService` →
  `DiskTransferService` → takes **only the video file**, renames it to the naming scheme, moves it.
  Consequences: a seeding torrent's file list no longer matches disk (re-download), and a
  multi-file release is gutted (video removed, `.nfo`/subs/sample left behind).
- A **library scan** arrives with `newDownload: false` → `_mediaFileService.Add()` only. **No
  transfer, no rename.** This is the only "`arr` does not touch the files" code path that exists.

`src/NzbDrone.Core/MediaFiles/DiskScanService.cs` calls `_importApprovedMovies.Import(decisions, false)`.

**Therefore the only viable shape is:** something else places the release inside the library folder,
then `arr` is made to *scan* that folder.

### 3.3 Why not simply let `arr` download into the library?

Because with Completed Download Handling (CDH) enabled, the importer then runs on a file that is
already at its destination:

- **Single-file release, already at `<movie folder>/<release>.mkv`, renaming off** → computed target
  equals the source → `DiskTransferService.TransferFile` throws
  `IOException("Source and destination can't be the same ...")`; with `HardLinkOrCopy` the
  `PathEquals` branch throws too. The import never completes and the queue item re-errors.
- **Folder release placed at `<movie folder>/<Release>/…`** → the decision maker picks the video
  inside, the target is `<movie folder>/<video>`, and `arr` **moves the video up one level**,
  leaving the rest of the release behind. Silently breaks the seed.

Hence **CDH off is a hard requirement**, not a preference (§9).

### 3.4 Prowlarr is not in this path

Prowlarr distributes indexer definitions into the *arr apps (`Settings → Apps → Sync App Indexers`)
and is a search UI. After sync, each app queries the trackers directly. Prowlarr is never involved in
grabbing, downloading, or importing, and `seedpath` has no reason to talk to it.

---

## 4. Approaches considered

| # | approach | verdict |
|---|---|---|
| A | **Hardlinks** (the standard advice): torrent tree on the same filesystem as the library, `arr` imports normally | Zero code and `arr` keeps its full import pipeline, but requires identical filesystem, single mount, correct union-fs policy, shared group with write permission — four independent failure modes, each of which silently degrades to copying. Also leaves two names per file. Keep as documented alternative for users who can satisfy all four conditions |
| B | **OnGrab custom script**: `arr`'s Connect → Custom Script fires on grab with `Movie_Id` and the infohash; the script calls `d.directory.set`; a completion hook calls the rescan | ~25 lines and no new service, but requires shell access inside each *arr container, duplicates logic per app, and has no view of the rTorrent protocol (faults, payloads) |
| C | **XMLRPC proxy** (this project) | Client- and app-agnostic: one implementation serves four apps and any download client that takes a directory at add time. No code inside `arr`, no code inside rTorrent. Costs: a new hard dependency in the grab path, and no filesystem-level tricks required at all |
| D | Copy + delete (status quo) | Double storage, rejected |

The proxy wins on portability, which is the whole point of this project. It also removes *all*
filesystem/policy preconditions from §3.1: nothing is linked, so nothing can silently become a copy.

---

## 5. How the pieces actually talk (protocol reference)

### 5.1 Sequence, one grab

```
arr → proxy        XMLRPC load.raw_start("", <torrent bytes>, "d.custom1.set=<category>")
                                  or load.start("", "magnet:?…", "d.custom1.set=<category>")
proxy → rTorrent   the same call, method rewritten to a paused variant
proxy → arr        rTorrent's response, byte-faithful (including faults). arr is finished here.
worker             resolve destination via arr API, then d.directory.set(<hash>, <path>), d.start(<hash>)
rTorrent           downloads into the library folder and keeps seeding it
worker (timer)     d.complete <hash> == 1 → POST arr command {RescanMovie|RescanSeries|RescanFolders}
arr                scan adopts the file in place (newDownload=false); no move, no rename
```

### 5.2 What the *arr rTorrent client actually sends

`src/NzbDrone.Core/Download/Clients/rTorrent/RTorrentProxy.cs` — the complete method surface:

| method | arguments | notes |
|---|---|---|
| `system.client_version` | — | used by "Test" |
| `load.start` / `load.normal` | `""`, `<url or magnet>`, then command strings | `normal` = paused |
| `load.raw_start` / `load.raw` | `""`, `<torrent file bytes>`, then command strings | `raw` = paused |
| `d.custom1.set` | `<hash>`, `<label>` | the category/label |
| `d.views.push_back_unique` | `<hash>`, `<view>` | post-import view |
| `d.erase` | `<hash>` | remove torrent (data stays) |
| `d.name` | `<hash>` | existence check |
| `d.multicall2` | `""`, `""`, 12 field strings | the queue poll, see below |

`load.*` command strings are generated in this order (`GetCommands`), **after** the payload argument:

```
["", <payload>, "d.custom1.set=<Category>", "d.priority.set=<n>", "d.directory.set=<Directory>"]
```

The paused variants are chosen by the per-client checkbox `AddStopped`
(`load.normal`/`load.raw` when on, `load.start`/`load.raw_start` when off).

The queue poll is a **targetless multicast over every torrent in the client**, every ~60 s:

```
d.multicall2("", "", "d.name=", "d.hash=", "d.base_path=", "d.custom1=", "d.size_bytes=",
             "d.left_bytes=", "d.down.rate=", "d.ratio=", "d.is_open=", "d.is_active=",
             "d.complete=", "d.timestamp.finished=")
```

`arr` then filters in its own code: torrents whose `d.custom1` is not its category are ignored, and
torrents with an empty `d.base_path` are skipped with a warning. Queue items get
`OutputPath = d.base_path` (after Remote Path Mapping) and `Completed` status from `d.complete` —
note it checks `d.complete` **before** active/paused, so pausing does not stop `arr` treating a
finished torrent as complete.

**Proxy requirements implied by the above:**

1. **Faults must pass through unchanged.** `arr` maps rTorrent faults to its own behaviour (duplicate
   torrent → "possible duplicate" rejection; dead URL → release unavailable). A proxy that reshapes or
   swallows faults silently changes `arr`'s semantics.
2. **Binary payloads must not be re-encoded.** `load.raw*` carries the `.torrent` file inside an
   XMLRPC base64 value. In Python this is `xmlrpc.client.Binary`; decode and re-encode as bytes.
3. **Large responses must pass through.** Hundreds of torrents × 12 fields, every minute. Do not
   parse what you are only forwarding. Check the rTorrent `network.xmlrpc.size_limit` (1 MB is a
   common default) against the largest torrent you expect.
4. **Basic auth.** ruTorrent's nginx fronts the SCGI socket; the proxy must accept the credentials
   `arr` was told to use and present them upstream. It may also need to tolerate the health-check
   behaviour of the image.
5. **`load.normal`/`load.raw` are legal and native** — that is exactly what `arr`'s own "Add Stopped"
   option uses, so pausing at add is a supported rTorrent operation, not a hack. Upstream help text
   warns that stopped **magnet** adds can break; if the proxy rewrites magnet adds to paused it must
   handle the `<hash>.meta` pseudo-entry state.

### 5.3 rTorrent path semantics (the part that bites)

From the rTorrent command reference (`d.directory`, `d.base_path`):

> **'single' file items:** `d.directory.set` → `d.name` is **never** appended (only in `d.base_path`);
> after start, `d.base_path := d.directory/d.name`
> **'multi' items:** `d.directory.set` → `d.name` **is appended**; after start, `d.base_path := d.directory`
> `d.base_path` always remains unchanged (and the item is closed) until `d.start` re-derives it.

Consequences for the implementation:

- **One destination value is correct for both torrent shapes.** Setting `d.directory` to the *movie /
  series folder* yields `<folder>/<Release>/…` for folder torrents and `<folder>/<file>` for single-file
  torrents. No branching on `d.is_multi_file` is required to place files.
- **`d.base_path` is empty while a torrent is stopped.** `arr` skips such torrents with a warning; that
  is expected during the (sub-second to few-second) retarget window and is harmless.
- **Read `d.directory` or `d.data_path`, never `d.base_path`, immediately after setting it.**
- **`d.start` after `d.directory.set`**: on stop, incomplete chunks are discarded; for a torrent we
  paused at add, nothing has been written, so retarget-and-start is free. (This is why pausing at add
  is the right mechanism rather than moving files later.)
- **rTorrent does not create missing parent directories.** rakshasa/rtorrent#907: setting the download
  directory to a path whose parent does not exist fails with
  `Could not create directory '<path>/<name>': No such file or directory` and the torrent stalls at 0 %.
  The library folder must therefore exist before `d.start`. It is created by `arr` (§9) — and if it is
  ever missing, the recovery is to call the app's scan command, not to `mkdir` from the proxy.

### 5.4 Custom fields and views (what *not* to use)

- `d.custom1` **is** the label: `arr` writes its Category there and reads it back to filter its queue.
  It must be left alone — never repurposed as a correlation tag.
- `d.custom2…5` are per-torrent free-text slots. The rTorrent docs advise against the numbered slots
  ("collisions with other uses are quite likely. *ruTorrent* for example uses #1 for its label, and
  the other slots for various other purposes").
- `d.views` are RAM-only named sets of torrents, not persisted, and every new torrent is tested
  against every existing view — one fixed name is fine, one name per grab is not.
- **None of them are needed** — see §5.5.

### 5.5 Identifying the torrent that was just added

The infohash is derivable from the request payload, so no tagging, no list diffing, no correlation:

| payload | how to get the hash |
|---|---|
| `load.raw` / `load.raw_start` (bytes) | `sha1(<bencoded bytes of the info dictionary>)` — requires walking the bencode far enough to find the info value's byte span (~20–30 lines) |
| `load.start` / `load.normal` (magnet) | `xt=urn:btih:<value>`; value is 40-char hex, or 32-char base32 which must be decoded to hex. rTorrent always reports 40-char hex in `d.hash` |

The same payload also yields the release name (`info.name`, or the magnet's `dn=` parameter) if the
implementation wants it for logging. `d.name` and `d.is_multi_file` are available from rTorrent
after the load if needed — no decoding required for those.

If the hash is not yet loaded when the worker calls `d.directory.set(<hash>, …)`, rTorrent returns a
fault; retry briefly. For magnets the hash exists immediately (the torrent appears as `<hash>.meta`),
which is ideal: retarget while it is still empty.

---

## 6. `arr` API contract

Version prefixes differ: Radarr/Sonarr/Whisparr use `/api/v3`, Lidarr/Readarr use `/api/v1`.
Authentication is the `X-Api-Key` header.

| purpose | endpoint | fields used |
|---|---|---|
| resolve destination | `GET /api/v3/parse?title=<d.name>` → `movie.id` / `series.id` | id only |
| | `GET /api/v3/movie/{id}` / `GET /api/v3/series/{id}` | `.path`; for Sonarr also `seasonFolder` |
| | `GET /api/v1/artist/{id}` / `GET /api/v1/author/{id}` | `.path` |
| identify the grab (authoritative) | `GET /api/v3/history?downloadId=<hash>&eventType=1` | `movieId` / `seriesId` / `artistId` / `authorId` (`eventType` is an int: `Grabbed = 1`, `DownloadFolderImported = 3`, `DownloadFailed = 4`, `MovieFileDeleted = 6`, `MovieFileRenamed = 8`) |
| adopt the file | `POST /api/v3/command {"name":"RescanMovie","movieId":N}` | |
| | `POST /api/v3/command {"name":"RescanSeries","seriesId":N}` | |
| | `POST /api/v1/command {"name":"RescanFolders","folders":["<path>"]}` | Lidarr/Readarr — folder-based, no id needed |
| preflight (§10) | `GET /api/v3/config/downloadclient`, `/config/mediamanagement`, `/config/naming` | |
| preflight | `GET /api/v3/downloadclient`, `/remotepathmapping`, `/rootfolder`, `/health` | |
| test connectivity | `POST /api/v3/downloadclient/test` | connectivity only, not paths |

**Ordering constraint:** the history row is written from `MovieGrabbedEvent`, which
`DownloadService.DownloadReport` publishes **after** `downloadClient.Download()` returns. So at the
moment the proxy sees the load call, the grab is not yet in history. The worker must retry for a few
seconds. `parse?title=` works immediately and is the documented fallback, at the cost of being
name-heuristic rather than authoritative — prefer history, fall back to parse.

Command handlers, for verification: `DiskScanService : IExecute<RescanMovieCommand>` (Radarr) and
`IExecute<RescanSeriesCommand>` (Sonarr) — meaning the rescan command *both* creates a missing folder
(§9) and adopts an existing file. Lidarr/Readarr expose `RescanFoldersCommand` with a `folders` list.

---

## 7. Architecture

```
        ┌──────────────┐   XMLRPC (proxied)   ┌───────────────┐
        │ *arr apps    │◄────────────────────►│  seedpath     │◄──► rTorrent / ruTorrent
        │ (download    │                      │               │      (XMLRPC + basic auth)
        │  client =    │   REST (preflight,   │  ┌─────────┐  │
        │  seedpath)   │◄────────────────────►│  │ workers │  │
        └──────────────┘                      │  └────┬────┘  │
                                              │       │ state │
        ┌──────────────┐    media files       │  state file   │
        │ rTorrent     │──────────────────────►│  (JSON/SQLite)│
        │ writes into  │   (no FS access      └───────────────┘
        │ the library  │    needed by seedpath)
        └──────────────┘
```

- **Ingress:** an XMLRPC HTTP server. `arr`'s download client points at
  `http://seedpath:<port>/RPC2` instead of `http://rtorrent:8000/RPC2`.
- **Egress:** rTorrent XMLRPC (with basic auth), plus each app's REST API.
- **No filesystem access required.** `seedpath` never touches media files. It only computes path
  strings and asks rTorrent to use them. (An optional read-only library mount is useful only for a
  "does this path exist" sanity check; the API-based check in §9 is preferable.)
- **State:** a small persistent map `hash → {app, appId, dest, state, timestamps}`. Needed to
  survive restarts mid-grab and to know which torrents need a completion rescan.
- **Workers:** a bounded queue plus a periodic reconciler. The request thread must never block on an
  `arr` API call; it forwards the load, responds to `arr`, and enqueues the follow-up.

### 7.1 Per-torrent state machine

```
RECEIVED      request parsed, hash extracted, load forwarded (paused) and answered
LOADED        hash confirmed present in rTorrent
RESOLVED      destination folder known from arr API
PLACED        d.directory.set accepted
STARTED       d.start accepted
COMPLETE      d.complete == 1
ADOPTED       rescan command accepted
DONE
```

Transitions carry deadlines. Failure handling in §11.

---

## 8. Deployment

Docker Compose service alongside the existing stack. Illustrative:

```yaml
services:
  seedpath:
    build: ./seedpath            # or image: <registry>/seedpath:<tag>
    container_name: seedpath
    restart: unless-stopped
    environment:
      RT_URL: http://rtorrent:8000/RPC2
      RT_USER: rtorrent
      RT_PASS: <secret>
      RADARR_URL: http://radarr:7878
      RADARR_KEY: <api key>
      SONARR_URL: http://sonarr:8989
      SONARR_KEY: <api key>
      # optional, per app
      LIDARR_URL: http://lidarr:8686
      LIDARR_KEY: <api key>
      READARR_URL: http://readarr:8787
      READARR_KEY: <api key>
      LIBRARY_ROOTS: '["/data/movies","/data/tv"]'   # refuse to place outside these
      CATEGORY_MAP: '{"radarr":"radarr","tv-sonarr":"sonarr","lidarr":"lidarr","readarr":"readarr"}'
      RESOLVE_TIMEOUT: 10s
      RESCAN_POLL: 30s
      DRY_RUN: "false"
    volumes:
      - ./config:/config          # state file, logs
    networks: [arr_stack]
    # no ports published — reachable only inside the compose network
```

Then, in each app: Settings → Download Clients → rTorrent → **Host** = `seedpath`, same port,
URL base `/RPC2`, same username/password, same **Category**.

Operational requirements:

- **Not published to the host.** It holds app API keys and client credentials; keep it on the
  internal network only.
- Run as a non-root user, read-only root filesystem, tiny image.
- Healthcheck endpoint (e.g. `GET /healthz`) that verifies it can reach rTorrent and each configured app.
- Structured logs with the infohash on every line of a torque's lifecycle — this is the only
  observability the operator will have.
- Optional metrics: grabs, retarget latency, resolve failures, fail-open count.
- A `--preflight` / `DRY_RUN` mode that reports configuration problems and exits (§10).

---

## 9. Required configuration in each *arr app

These are the contract. Every one of them, with its default, as verified in source:

| setting | required | default | why |
|---|---|---|---|
| Completed Download Handling (`enableCompletedDownloadHandling`) | **off** | on | stops `arr` importing over the placement (§3.3) |
| Create empty movie/series/artist/author folders | **on** | off | creates the destination folder at add time; with CDH off nothing else will |
| Delete empty folders | **off** | off | otherwise the scan refuses to create folders and removes empty ones |
| Set permissions (`setPermissionsLinux`) | **on** | off | without it `chmodFolder` is ignored |
| `chmodFolder` | **775** | 755 | rTorrent (a different user) must be able to create the release folder *inside* an `arr`-created folder |
| Rename Movies/Episodes/Tracks/Books | **off** | off | a rename moves a file out of the torrent |
| Import Extra Files | **off** | off | the scan would rename `.srt`/`.nfo` out of the release folder |
| Auto Rename Folders (Radarr) | **off** | off | renames the folder the torrent seeds from |
| Download client: Add Stopped | **on** | off | the torrent must not start before retargeting |
| Download client: Category | per app (`radarr`, `tv-sonarr`, `lidarr`, `readarr`) | — | the proxy keys off it; `arr` filters its queue by it |
| Download client: Directory | empty | empty | irrelevant — the proxy overrides the location |
| Remote Path Mappings | **none** | — | container paths must agree; mappings are a symptom, not a fix |
| umask | `002` in every container; `system.umask.set = 0002` in rTorrent | | shared-group write access |

Sonarr specifics:

- `SeasonFolder` is a **per-series** setting, not global. If the operator wants no season folders,
  it must be disabled per series; the destination is then the series folder. The proxy must read
  `seasonFolder` from `/api/v3/series/{id}` and only append `Season NN` when it is on.
- Season folders are otherwise created at import (`EnsureEpisodeFolder`), which never runs with CDH
  off — another reason the destination is the series folder when seasons are disabled.

Folder-rotation behaviour worth documenting for users: `RefreshMovieCommand` runs every **24 h**
(`Interval = 24 * 60`), which is why the proxy triggers the rescan itself instead of waiting for
`RescanAfterRefresh`. A *new* movie, however, is force-scanned regardless of `RescanAfterRefresh`
(`RefreshMovieService.RescanMovie`: `if (isNew) { "Forcing rescan" }`), so its folder is created
within seconds of being added.

---

## 10. Preflight (self-check)

`seedpath --preflight` should query every configured app and print a pass/fail table, exiting
non-zero on failure. Checks:

```
GET {app}/api/{v}/config/downloadclient     → enableCompletedDownloadHandling == false
GET {app}/api/{v}/config/mediamanagement     → createEmpty*Folders == true
                                              setPermissionsLinux == true
                                              chmodFolder in ("775","0775")
                                              importExtraFiles == false
                                              deleteEmptyFolders == false
                                              (Radarr) autoRenameFolders == false
GET {app}/api/{v}/config/naming              → every boolean key matching /rename/i == false
GET {app}/api/{v}/downloadclient             → the rTorrent entry: enable == true,
                                              field addStopped == true,
                                              category == CATEGORY_MAP value
GET {app}/api/{v}/remotepathmapping          → length == 0
GET {app}/api/{v}/rootfolder                 → path(s) inside LIBRARY_ROOTS
GET {app}/api/{v}/health                     → only expected warning is CDH disabled
```

Field names are stable across apps except for the app-specific ones listed above
(`renameMovies` / `renameEpisodes` / `renameTracks` / `renameBooks`;
`createEmptyMovieFolders` / `createEmptySeriesFolders` / `createEmptyArtistFolders` /
`createEmptyAuthorFolders`).

Three things the API cannot tell you, which need a probe:

1. rTorrent can actually write inside an `arr`-created folder (permission/umask effective in the
   client container).
2. Both containers resolve the same absolute paths (`arr`'s `movie.path` vs rTorrent's `d.directory`
   after placement).
3. The retarget actually took effect (`GET /api/v3/queue` → `outputPath` matches `d.directory`).

`DRY_RUN=true` should perform the full resolution and log the intended `d.directory.set`, without
calling it or starting anything.

---

## 11. Failure handling

| situation | behaviour |
|---|---|
| history row not yet present | retry with backoff up to `RESOLVE_TIMEOUT`; then fall back to `parse?title=<d.name>` |
| resolution impossible (no movie/series match, app down) | **fail open**: `d.start` in the client's configured/default directory, log an error, mark the torrent so the operator can see it. Never leave a torrent paused with no owner |
| destination outside `LIBRARY_ROOTS` | refuse to place, fail open, log loudly |
| destination folder missing | call the app's scan command (it creates the folder when the setting is on), wait briefly, retry once, then fail open |
| `d.directory.set` faults (hash not yet loaded) | retry for a few seconds |
| duplicate add (rTorrent fault) | pass the fault through untouched; do not run a worker for it |
| proxy restarts with torrents mid-flight | reconcile from the state file; additionally sweep for torrents whose category is in `CATEGORY_MAP`, `d.state == 1` and `d.is_active == 0` and which are absent from state — that is the signature of a retarget left unfinished |
| torrent removed in `arr` (`d.erase`) | drop its state entry |
| rescan command rejected | log with the API error body; the file will still be adopted by the next scheduled refresh (24 h) |

Operational hazard to document: in this design the torrent's data **is** the library file. Removing a
torrent *with its data* (ruTorrent "remove and delete data") deletes the library file. Only remove
the torrent, never the data.

---

## 12. Testing

### Unit
- bencode: hash extraction on single-file and multi-file `.torrent` fixtures; magnet hash in hex and
  base32; malformed payload handling.
- XMLRPC rewrite: method mapping (`load.start`↔`load.normal`, `load.raw_start`↔`load.raw`), argument
  order preserved, payload bytes byte-identical, faults passed through verbatim.
- State machine transitions and deadlines.

### Integration (mock stack, no real apps required)
- A mock XMLRPC server that registers dotted method names (`d.multicall2` etc.) and records calls.
- Mock *arr REST endpoints: `parse`, `history`, `movie`/`series`, `command`.
- Assertions: correct `d.directory.set` target per shape; `d.start` only after placement; the
  response to `arr` is sent *before* any follow-up call; duplicate faults pass through; idempotency
  across a simulated restart.
- Fixtures needed: a single-file `.torrent`, a folder `.torrent`, a magnet in hex and in base32, and a
  duplicate-add fault response.

### Acceptance (on a real stack)
1. A single-file movie grab lands at `<library>/<Movie (Year)>/<release>.mkv`, seeds, and appears in
   Radarr after the rescan. No copy exists anywhere else; `stat` on the library file and the torrent's
   file shows one inode with link count 1.
2. A folder release lands at `<library>/<Movie (Year)>/<Release>/…` with `.nfo`/subs intact and seeds
   without re-downloading anything.
3. `arr`'s log contains no import attempts, no `fallback to copy`, no
   `Source and destination can't be the same`.
4. Killing the proxy between load and placement leaves no torrent paused forever after restart.
5. `arr` can still be paused/resumed/removed through its own UI; the proxy does not interfere with
   `d.erase` or queue state.
6. Removing a torrent from the client (without deleting data) leaves the library file playable.

---

## 13. Language and dependencies (recommendation)

**Python 3.12, standard library only.** Rationale:

- XMLRPC exists in the stdlib in both directions, including `Binary` payloads and `Fault` objects —
  the hardest requirement in this project (§5.2) is free. Go and Rust have no stdlib XMLRPC; Go has
  thin third-party implementations, and proxying *arbitrary* dynamic XMLRPC faithfully is
  significantly more work in a statically typed language.
- `http.server.ThreadingHTTPServer` + `xmlrpc.client` cover both sides; `urllib` covers the REST calls.
- The reference mock stack and test suite are already Python.
- Deployment cost is negligible: `python:3.12-alpine` plus one module.

Hard requirements regardless of language: timeouts on every outbound call; threading (a slow `arr`
call must not block the queue poll); atomic state writes (temp file + rename); no blocking work on
the request thread.

Go is a defensible second choice if a single static binary matters more than protocol fidelity —
in that case, prefer raw-body rewriting (patch the method name and command list in the XML, forward
the response body verbatim) over typed decoding.

---

## 14. Open questions for the implementer

1. **Upgrade handling.** On an upgrade grab, the new release is placed beside the old one and the
   rescan sees two files for one item. Options: (a) document "delete the old folder by hand", (b)
   call `GET /api/v3/moviefile?movieId=` → `DELETE /api/v3/moviefile/{id}` before placing and drop the
   old torrent. v1 should ship (a) and leave (b) behind a flag.
2. **Magnet adds.** Whether to support `load.start` magnets in v1 at all, given upstream's warning
   that stopped magnet adds "may break". If supported, handle the `<hash>.meta` state.
3. **Multi-instance apps.** Radarr+Radarr-4K etc.: one adapter per configured instance, keyed by
   category. Confirm category uniqueness across instances as a preflight check.
4. **Staging on a fast disk.** The current design downloads directly into the library. A variant that
   downloads to a scratch disk first needs a mover and a completion rename (same filesystem → instant)
   plus extra care that partial files never appear in the library. Explicitly out of scope for v1 but
   worth designing the state machine to allow it.
5. **Naming collision.** Two releases mapping to the same destination folder (e.g. a re-grab of the
   same movie while the old torrent still exists). Decide: refuse, or place beside with a suffix.
6. **`arr` version compatibility.** All findings are from current `develop`. Pin the verified
   versions in the README and include a compatibility note for the API version prefixes.

---

## 15. Reference reading (for verification, not required)

- Radarr: `src/NzbDrone.Common/Disk/DiskTransferService.cs`, `TransferMode.cs`,
  `src/NzbDrone.Core/Download/Clients/rTorrent/RTorrentProxy.cs`, `RTorrent.cs`, `RTorrentSettings.cs`,
  `src/NzbDrone.Core/Download/DownloadService.cs`, `CompletedDownloadService.cs`,
  `DownloadProcessingService.cs`, `src/NzbDrone.Core/MediaFiles/ImportApprovedMovie*.cs`,
  `DiskScanService.cs`, `MovieFileMovingService.cs`, `src/NzbDrone.Core/Movies/RefreshMovieService.cs`,
  `src/NzbDrone.Core/Jobs/TaskManager.cs`, `src/NzbDrone.Core/Configuration/ConfigService.cs`.
- Sonarr equivalents under `src/`.
- Lidarr/Readarr: `RescanFoldersCommand`, `CreateEmptyArtistFolders` / `CreateEmptyAuthorFolders`.
- rTorrent command reference (d.directory / d.base_path / d.custom / d.views), mergerfs
  `rename_and_link` and hardlink FAQ, rakshasa/rtorrent issue #907.
- Prior art for alternative approaches: the Servarr Docker guide, TRaSH Guides' "Hardlinks and Instant
  Moves" section, and `autobrr`/`unpackerr`/`cross-seed` for packaging conventions.
