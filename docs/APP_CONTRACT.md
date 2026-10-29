# Zemer static backend contract (v1: music + lyrics)

What the Zemer Android app expects from a server, taken from the extracted sources under
`/home/claude/contract`. The aim is a backend made only of static files: (a) the content mirror JSON
and (b) the offline-subset snapshot. The app reads that snapshot through its on-device read layer for
`/search`, `/artist`, `/album`, `/home-rows` and `/zemer-playlists`.

Citations are `path:line`. Paths are relative to `app/src/main/kotlin/com/jtech/zemer/` unless they start
with `app/src/test/` or `docs/`. When the code does not settle a point, this document says so instead of
guessing.

---

## 0. Summary of findings

1. **Shards are gzipped JSON, not a binary format.** Each shard is a gzip stream holding UTF-8 JSON:
   either a bare array of positional rows or an object (`offline/SubsetDecoder.kt:21-26,288-291`).
   There are no varints and no byte-order concerns.
2. **The app has two different hosts, and both are fixed at build time:**
   - **Content mirror.** `BuildConfig.CONTENT_MIRROR_URL`, normally `content.zemer.io`
     (`utils/ZemerContentClient.kt:26-27,39`). It is a gradle build-config value. The gradle file is not in
     the extracted set.
   - **Subset snapshot.** `GET https://search.zemer.io/subset/...`. `SubsetSyncClient.BASE_URL` is set to
     `ZemerSearchClient.BASE_URL`, which is a Kotlin `const` holding `"https://search.zemer.io"`
     (`offline/SubsetSyncClient.kt:57`, `search/ZemerSearchClient.kt:625`). Moving the snapshot to your
     host therefore needs a one-line code change or DNS control of `search.zemer.io`.
3. **The read layer serves nothing unless several gates pass.** A synced, committed snapshot must exist.
   The last successful sync must be at most 14 days old. Every shard must match its manifest hash. Sync
   only runs when the user has opted in, and the opt-in defaults to off
   (`offline/OfflineReadProvider.kt:68-83`, `offline/SubsetLiveWhitelist.kt:11-15`,
   `offline/OfflineSubsetSyncer.kt:99-100,159-165`).
4. **The `/whitelist` ids must cover every artist in the snapshot.** At load time the snapshot is overlaid
   with the live whitelist. Any artist id missing from the synced whitelist is dropped, together with all
   of its tracks and albums (`offline/SubsetLiveWhitelist.kt:32-89`).
5. **To always use the offline layer, change one function plus three configuration points** (§4). Do not
   simply point `BASE_URL` at a static host: a 404 from `/album`, `/artist` or `/zemer-playlists?id=`
   counts as "not found", so the app would not fall back to the snapshot.
6. **LRCLIB works without `search.zemer.io`.** The Zemer resolver is a separate, first-in-chain
   provider. If its host is unreachable it only adds an error report per song, but it should still be
   repointed or disabled (§5).

---

## 1. Content mirror

### 1.1 Transport (all mirror endpoints)

| Aspect | Behaviour | Source |
|---|---|---|
| Base URL | `BuildConfig.CONTENT_MIRROR_URL` with trailing `/` trimmed. Empty means the mirror is disabled and every call throws. | `utils/ZemerContentClient.kt:38-40` |
| Method | `GET "$baseUrl$path"`. No query string, no auth, no custom headers. | `ZemerContentClient.kt:59-63`, `:34` |
| Gzip | The Ktor `ContentEncoding { gzip() }` plugin is installed, so the client sends `Accept-Encoding: gzip` and decompresses the reply transparently. Plain bodies also work. | `ZemerContentClient.kt:54-55` |
| Timeouts | Connect 3 s. Request 4 s by default. `/whitelist` and `/podcastChannelsWhitelist` get 8 s. | `ZemerContentClient.kt:50-52,62,164-169` |
| Status | Any non-2xx throws `IOException`. The body is read with `bodyAsText()`. Redirects follow Ktor's default (not configured in these files). | `ZemerContentClient.kt:64-67` |
| JSON parsing | `ignoreUnknownKeys = true`, `isLenient = true`, `coerceInputValues = true`. | `ZemerContentClient.kt:42-46` |
| Charset | Not set explicitly. Names contain Hebrew, so serve `Content-Type: application/json; charset=utf-8`. Ktor's `bodyAsText` uses the declared charset. | (recommendation) |
| Failure handling | `mirrorFirst`: **any** throw (network, non-2xx, parse error, or the client's own empty-list check) falls back to the Firebase/Firestore SDK path. **A broken mirror therefore makes the app read the original Firebase project silently.** | `ZemerContentClient.kt:243-257`, `utils/WhitelistFetcher.kt:17-31` |

### 1.2 Endpoints

The music features need the first three. The others are listed so nothing surprises you.

#### `GET /whitelist/version` (required)

- **DTO:** `ContentVersionDto` (`ZemerContentClient.kt:172-178`).
- **Shape:** `{ "gate": Long?, "update": String?, "updatedAtMs": Long? }`. All fields are optional in the
  DTO, but **only `gate` is read**. A missing `gate` throws, which triggers the Firebase fallback
  (`ZemerContentClient.kt:71-76`).
- **Version comparison:** in `SyncUtils.syncArtistWhitelist`, the stored version is
  `LastWhitelistVersionKey` (default `0L`) (`utils/SyncUtils.kt:840-841`).
  - The fast path (no `/whitelist` download) is taken only when `remoteVersion <= localVersion`, the local
    table is non-empty, fewer than 25 artists lack thumbnails, and `DisplayNamesBackfilledKey` is `true`
    (`SyncUtils.kt:859-872,1119-1132,1106`).
  - `gate` must therefore increase **monotonically**. A lower value is never fetched until it exceeds
    the stored one.
  - The version is stored only after a successful full fetch (`SyncUtils.kt:937-938`).
  - A failed version probe (`null`) forces a full fetch every time (`SyncUtils.kt:1128`).
  - Epoch milliseconds are fine (`Long`).
- **Pitfall:** `DisplayNamesBackfilledKey` is set only when at least one fetched doc has a non-blank
  `displayName` (`SyncUtils.kt:943-945,1155-1157`). If **no** whitelist doc has `displayName`, the fast
  path is never eligible and the full `/whitelist` is downloaded on every sync. Give every doc a
  `displayName`.
- **Minimal example:** `{"gate": 1761696000000}`

#### `GET /whitelist` (required)

- **Shape:** a JSON **array** of `ContentWhitelistDoc` (`ZemerContentClient.kt:200-224`).
- **An empty array throws.** That triggers the Firebase fallback (`ZemerContentClient.kt:82-87`).
- **Mapping to `ArtistWhitelistEntity`** (`utils/WhitelistFetcher.kt:45-64`):

| Field | Type | Required | Default / use |
|---|---|---|---|
| `id` | string | yes (or `artistId`) | YouTube channel id `UC…`. Fallback is `artistId`. A doc with neither is **skipped**. Must equal the subset `artists` ids (§2.7). |
| `artistId` | string? | no | Alternative to `id`. |
| `name` | string? | yes (or `artistName`) | Legacy name. Fallback is `artistName`. A doc with neither is **skipped**. |
| `artistName` | string? | no | Alternative to `name`. |
| `displayName` | string? | recommended | Clean single-script name. Authoritative for the rendered artist name (`SyncUtils.kt:911,926`). Blank is treated as null. |
| `altName` | string? | no | Same name in the other script (search alias). |
| `isFemale` | bool? | no → `false` | **Overrides the snapshot's female flag** (§2.7). |
| `isChasid`, `isGenZ`, `isKids`, `isKidZone` | bool? | no → `false` | `isKidZone` drives the Kid Zone browse. `isKids` excludes the artist from the ranked home rows (`docs/whitelist/README.md:23-24`). |
| `isAmerican`, `isFamous`, `isDJ`, `isGroup` | bool? | no | Parsed. Kept nullable for `HomeArtistProfile` (`ZemerContentClient.kt:194-199`). That consumer is not in the extracted files. |
| `thumbnail` | string? | recommended | Artist avatar URL (yt3 or lh3). Written fill-only to artist rows. With 25 or more missing, the version fast path is bypassed (`SyncUtils.kt:850-851`). |

- **An empty parse is never applied** (`SyncUtils.kt:886-895`). Artists that vanish from a non-empty list
  are **deleted locally**, including their songs, albums and history (`SyncUtils.kt:897-903,1052-1098`).
- **Minimal example:**
  ```json
  [
    {"id":"UCexampleArtistAAAAAAAAA","name":"Avraham Example","displayName":"Avraham Example",
     "isFemale":false,"isKidZone":false,"thumbnail":"https://yt3.ggpht.com/exampleA=s800"},
    {"id":"UCexampleArtistBBBBBBBBB","name":"Example Choir","displayName":"Example Choir",
     "isFemale":false,"isChasid":true,"thumbnail":"https://yt3.ggpht.com/exampleB=s800"}
  ]
  ```

#### `GET /blockedContentIds` (required)

- **Shape:** `ContentBlockedDto { "global": [String], "female": [String] }`. Both default to `[]`
  (`ZemerContentClient.kt:180-185`).
- **Processing:** ids are trimmed and blanks dropped. The result is a map of id to reason, where `female`
  is filled first and `global` overwrites it, so `global` wins on a duplicate (`ZemerContentClient.kt:114-122`).
- **Matching:** ids are compared against the videoId, playlistId, browseId or channelId of any item
  (`docs/whitelist/README.md:164`).
- **Semantics** (`docs/whitelist/README.md:171-175`):
  - `global` hides the item for everyone whenever filters are enabled.
  - `female` hides it only when `allowFemaleSingers == false`.
- **An empty map is ignored** and the previous blocklist is kept (`SyncUtils.kt:1033-1045`). You cannot
  clear the list by serving an empty body.
- **Refresh timing:** fetched on every whitelist sync, including the fast path (`SyncUtils.kt:869,892,929`).
- **Keep in sync with the `blocked` shard.** The online mapper uses this cache (`dropBlocked`,
  `search/ZemerResultMapper.kt:120-123`). The offline read layer uses the subset `blocked` shard
  (`offline/SubsetReadLayer.kt:139-141`). Publish the same data in both places.
- **Minimal example:** `{"global":[],"female":[]}`

#### `GET /israeliArtists` (recommended: serve `[]`)

- **Shape:** an array of either `"id"` strings or `{"id"|"artistId": "…"}` objects. Both forms are
  accepted. An empty array is legitimate (`ZemerContentClient.kt:124-141`).
- **Consumer:** `IsraeliArtistRegistry` (`ZemerContentClient.kt:32-34`). Its source file is not in the
  extracted set, so its fallback behaviour is unknown.
- **Where it is used:**
  - `artistMatchesFilters`, only when the allowed-id set is empty (`utils/WhitelistFilter.kt:291-300`).
  - The ranked home-row gate (`docs/home_rows/README.md:73-77`).
- **Minimal example:** `[]`

#### `GET /podcastChannelsWhitelist/version` and `GET /podcastChannelsWhitelist` (not needed for music)

- **Shapes:** `{"gate": Long}` and an array of `ContentPodcastDoc { id, name, thumbnailUrl?, isFemale,
  isKidZone, isVerified, showCount }` (`ZemerContentClient.kt:89-107,226-241`).
- **An empty array throws**, which triggers the Firebase fallback (`ZemerContentClient.kt:104`).
- **If you skip podcasts:** leave both 404. The podcast sync then fails softly (`SyncUtils.kt:1018-1021`).
  An empty podcast whitelist makes the offline podcast overlay a no-op (`offline/SubsetLiveWhitelist.kt:99-100`).

#### `GET /status-sources/version` and `GET /status-sources` (not needed for music)

- **Shapes:** `{"version": Long, "updatedAt": String?}` plus a raw config consumed by the `statuses`
  package, which is not extracted (`ZemerContentClient.kt:143-162,187-192`).
- **On failure** the feature stays hidden (`ZemerContentClient.kt:146-148`).

### 1.3 When the mirror is called

- `SyncUtils.syncArtistWhitelist(forceSync)` calls `fetchVersion()`, then `fetchWhitelist()` when
  needed, then `refreshBlockedIds()` (`SyncUtils.kt:831-953`).
- Triggers (per `docs/whitelist/README.md:206-219`; `MainActivity` and `LoadingScreen` are not extracted):
  - startup in `MainActivity`
  - the forced sync in `LoadingScreen`
  - pull-to-refresh in the library, artists and Kid Zone screens

---

## 2. Offline subset snapshot

### 2.1 Transport

| Aspect | Behaviour | Source |
|---|---|---|
| Manifest | `GET {BASE_URL}/subset/manifest`. Body read as text and decoded with `Json { ignoreUnknownKeys = true }`. **Not lenient:** strict JSON only. | `offline/SubsetSyncClient.kt:34,37-43` |
| Shard | `GET {BASE_URL}/subset/<name>` (no extension). `<name>` must match `^[a-z0-9-]+$` or the call is refused. | `SubsetSyncClient.kt:46-53`, `offline/SubsetManifest.kt:47-49` |
| Shard body | `readRawBytes()`. **No `ContentEncoding` plugin**, so the bytes the client receives are what gets hashed. | `SubsetSyncClient.kt:17-22,52` |
| Timeouts | Connect 10 s. Request 60 s per call (manifest and each shard). | `SubsetSyncClient.kt:27-32,58-59` |
| Status | Any non-2xx throws `IOException`. The sync becomes `FAILED` and the previous snapshot is kept. | `SubsetSyncClient.kt:39-41,49-51`, `offline/OfflineSubsetSyncer.kt:145-149` |
| Base URL | Hard-coded `https://search.zemer.io`. | `SubsetSyncClient.kt:57`, `search/ZemerSearchClient.kt:625` |

**Serving rule for shards.** Serve each shard file as the stored gzip bytes, with
`Content-Type: application/gzip` (or `application/octet-stream`) and **without** a
`Content-Encoding: gzip` header.

- The reason: if a CDN marks the object `Content-Encoding: gzip`, it may decompress or re-compress it
  for a client that did not advertise gzip. The bytes the client hashes would then differ, and you get a
  hash-mismatch `FAILED`.
- This is an inference from `SubsetSyncClient.kt:17-22` and `OfflineSubsetSyncer.kt:117-123`.
- Static-host note: the paths have no extension (`/subset/manifest`, `/subset/tracks-0`). Make sure the
  host serves extensionless files and does not rewrite them to `.html`.

### 2.2 Manifest format

`SubsetManifest` (`offline/SubsetManifest.kt:25-44`):

```json
{
  "v": 1,
  "builtAt": "2026-10-29T00:00:00Z",
  "schema": 1,
  "shards": [ {"name": "artists", "hash": "393cd2792e9d7017", "bytes": 114} ]
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | **Int (32-bit)** | yes | Manifest version counter, bumped on every rebuild (`SubsetManifest.kt:27-28`). See the note below the table. |
| `builtAt` | string | yes | ISO-8601 build time, informational (`SubsetManifest.kt:29-30`). |
| `shards` | array | yes | The complete set of shards (`SubsetManifest.kt:31`). |
| `schema` | Int | no, defaults to `1` | Wire-format generation. **Must equal `1`** (`SUPPORTED_SUBSET_SCHEMA`). Any other value rejects the whole manifest and keeps the last-good snapshot (`SubsetManifest.kt:16-23,32-33`, `OfflineSubsetSyncer.kt:108-112`). |
| `shards[].name` | string | yes | Shard id and file stem. Must match `^[a-z0-9-]+$` (`SubsetManifest.kt:38-39,47`). |
| `shards[].hash` | string | yes | **The first 16 lowercase hex characters of SHA-256 over the exact gzipped bytes served** (`offline/SubsetHash.kt:5-22`). Compared with exact string inequality (`OfflineSubsetSyncer.kt:118-121`). |
| `shards[].bytes` | Long | yes | Gzipped size, informational (`SubsetManifest.kt:42-43`). |

Notes on `v`:

- `v` is a Kotlin `Int`, so **epoch milliseconds do not fit**. Use a counter, or epoch seconds (which fit
  until 2038).
- `v` is also the key for the in-memory corpus cache (`OfflineReadProvider.kt:76`). If you change shards
  without bumping `v`, a running process can keep serving the old decoded corpus.

### 2.3 Sync and integrity rules

**Plan.** `subsetSyncPlan(local, remote)` (`SubsetManifest.kt:68-74`):

- Download every remote shard whose `name` is new or whose `hash` differs from the local one.
- Delete every local shard whose `name` is absent from the remote manifest.
- A shard is **replaced whole, never merged** (`SubsetManifest.kt:10-14`). `v` plays no part in the diff.

**Sync** (`OfflineSubsetSyncer.kt:96-151`):

1. Fetch the manifest and check its schema (`:107-112`).
2. Clear leftover staged files (`:115`).
3. For each shard to download: fetch it, compute `subsetShardHash`, and throw on a mismatch. Then stage
   it as `<name>.gz.staged` (`:116-123`, `offline/SubsetStore.kt:54-57`).
4. Once **all** shards verify, promote them (rename over `<name>.gz`), delete removed shards, and prune
   orphan `*.gz` files (`:124-127`).
5. Commit `manifest.json` atomically with temp file plus rename (`:128`, `SubsetStore.kt:82-90`).
6. Record `OfflineSubsetLastSyncedAtKey = now`, **even when nothing changed** (`:130-131`). This keeps
   the snapshot fresh as long as the manifest stays reachable.

**Storage.** Files live under `filesDir/subset/` as `<name>.gz` plus `manifest.json`
(`SubsetStore.kt:9-14,18,24-27,109-113`).

**Read-time integrity.** `SubsetDecoder.loadCorpus` re-hashes **every** shard on every corpus load. Any
missing shard, hash mismatch, decode exception or OOM makes the result `null`, meaning "no snapshot"
(`SubsetDecoder.kt:32-34,55-63,93-103`).

**Gating before the corpus is served** (`OfflineReadProvider.kt:68-83`):

- The snapshot must be fresh: `lastSyncedAt > 0` and `now - lastSyncedAt <= 14 days`
  (`SubsetLiveWhitelist.kt:11-15`).
- A committed manifest must exist.
- The corpus is then overlaid with the live whitelist (§2.7).

**When sync runs:**

- `maybeSync()` runs at app start. It does nothing unless `OfflineSubsetEnabledKey == true` and 24 h
  have passed since the last sync (`OfflineSubsetSyncer.kt:159-165,188`, `docs/offline/README.md:106-107`).
- `sync()` with `force = false` returns `DISABLED` while the opt-in is off. The opt-in defaults to
  `false` (`OfflineSubsetSyncer.kt:99-100`).
- The user turns it on in Settings, then Search backup, or during onboarding (`docs/offline/README.md:114-119`).

### 2.4 Shard encoding (applies to every shard)

**Container:**

- A gzip stream (`java.util.zip.GZIPInputStream`). Its content is UTF-8 text holding **one JSON value**,
  with no per-shard wrapper or version (`SubsetDecoder.kt:21-26,290-291`).
- Parsed with `Json { ignoreUnknownKeys = true }`, which is strict, not lenient (`SubsetDecoder.kt:29`).

**Row arrays:** "positional rows" are `[[col0, col1, …], …]` (`SubsetDecoder.kt:288`). Columns are read
by index, as follows:

| Accessor | Behaviour | Source |
|---|---|---|
| `r[i]` | Direct index access. **A row shorter than a required column throws, and the whole snapshot is then discarded.** Only the columns read with `getOrNull` may be missing. | |
| `asString()` | `jsonPrimitive.content`. **A JSON `null` decodes as the literal string `"null"`, so never put null in a required string column.** | `SubsetDecoder.kt:296` |
| `asStringOrNull()` | JSON `null` (or a missing key in an object) becomes `null`. | `SubsetDecoder.kt:297` |
| `asInt()` | Required integer. Emit plain JSON integers with no decimal point. | `SubsetDecoder.kt:298` |
| `asIntOrNull()`, `asLongOrNull()`, `asDoubleOrNull()` | Nullable numbers. A value that fails to parse also becomes `null`. | `SubsetDecoder.kt:299-301` |

**Shard-name dispatch** (`SubsetDecoder.kt:65-85`):

- Exact names: `artists`, `playlists`, `community`, `communitytracks`, `homerank`, `zemer`,
  `podcastchannels`, `podcasts`, `blocked`, `synonyms`, `genrecatalog`, `lyricsflags`, `realvideos`.
- Prefixes: `tracks-`, `albums-`, `albumtracks-`, `podcastepisodes-`, `radio-`.
- **Prefixed shards must carry the dash.** A shard named `tracks`, with no suffix, is silently ignored.
- Partitions are concatenated in **manifest order**.
- Unknown names are ignored (`SubsetDecoder.kt:84`).

**Ids must be unique across partitions.** The by-id maps use `associateBy`, so the last occurrence wins
(`offline/SubsetCorpus.kt:40-42`).

### 2.5 Per-shard wire layout

A "Req." of **yes** means the decoder throws if the column or key is missing. "Nullable" means JSON
`null` is allowed.

#### `artists` (exact name). Each row is `[id, name, thumbnail, flags]`, decoded into `SubArtist` (`SubsetDecoder.kt:108-118`, `SubsetCorpus.kt:82-89`).

| # | Field | Type | Req. | Notes |
|---|---|---|---|---|
| 0 | `id` | string | yes | Channel id `UC…`. Must appear in `/whitelist` (§2.7). |
| 1 | `name` | string | yes | Display and search name. Also the female-credit matcher source (`offline/SubsetFemale.kt:51-65`). |
| 2 | `thumbnail` | string | nullable | Artist avatar on search, artist and home cards. |
| 3 | `flags` | int | yes | bit0 = `isFemale`, bit1 = `isChasid`, bit2 = `isKidZone` (`SubsetDecoder.kt:114-116`). |

#### `tracks-<n>` (prefix). Each row is `[videoId, title, artistId, flags, durationSec, playCount, uploadDate]`, decoded into `SubTrack` (`SubsetDecoder.kt:120-131`, `SubsetCorpus.kt:91-99`).

| # | Field | Type | Req. | Notes |
|---|---|---|---|---|
| 0 | `videoId` | string | yes | The YouTube videoId. Playback goes through InnerTube with this id. |
| 1 | `title` | string | yes | |
| 2 | `artistId` | string | yes | Must exist in `artists`, or the track is orphaned and dropped by the overlay. |
| 3 | `flags` | int | yes | bit0 = `isVideo`. bit1 is an unused "explicit" bit (`app/src/test/.../SubsetDecoderTest.kt:43-44`). |
| 4 | `durationSec` | int | nullable | Column must be present. |
| 5 | `playCount` | long | nullable | Column must be present. |
| 6 | `uploadDate` | string | nullable | Column must be present. ISO date. Year-rule playlists use the first 4 characters (`SubsetReadLayer.kt:501`). Radio requires a year between 1900 and 2100 (`offline/SubsetRadio.kt:92`). |

#### `albums-<n>` (prefix). Each row is `[id, playlistId, title, artistId, type, year, thumbnail, uploadDate]`, decoded into `SubAlbum` (`SubsetDecoder.kt:133-144`, `SubsetCorpus.kt:101-111`).

| # | Field | Type | Req. | Notes |
|---|---|---|---|---|
| 0 | `id` | string | yes | Album browseId `MPRE…`. |
| 1 | `playlistId` | string | nullable | `OLAK…` playlist. Forwarded to the album header (`SubsetReadLayer.kt:183-186`). |
| 2 | `title` | string | yes | |
| 3 | `artistId` | string | yes | Primary artist. Gates the whole album. |
| 4 | `type` | string | yes | `"album"`, `"single"` or `"ep"`. **Only `"single"` is split out.** Everything else lists under Albums (`SubsetReadLayer.kt:252-253`, `offline/SubsetCategories.kt:391-392`). |
| 5 | `year` | int | nullable | Present but nullable. |
| 6 | `thumbnail` | string | nullable | Square album art. Also used as every track's art on the album page (`search/ZemerResultMapper.kt:283-284`). |
| 7 | `uploadDate` | string | nullable | Present but nullable. |

#### `albumtracks-<n>` (prefix). Each row is `[albumId, videoId, pos]`, decoded into `SubAlbumTrack` (`SubsetDecoder.kt:146-148`).

- `pos` is a 0-based int. The album page sorts by it and reports `trackNumber = pos + 1`
  (`SubsetCorpus.kt:44-47`, `SubsetReadLayer.kt:166-177`).
- `videoId` must be a `tracks-*` row; members without one are skipped (`SubsetReadLayer.kt:167`).

#### `playlists` (exact name). Each row is `[id, title, artistId, thumbnail]`, decoded into `SubArtistPlaylist` (`SubsetDecoder.kt:150-152`).

- Artist-owned YouTube playlists, shown on the artist page and in search (`SubsetReadLayer.kt:254-256`,
  `SubsetCategories.kt:155-159`).
- `thumbnail` is nullable.
- **Opening one uses the live-only `/playlist` endpoint** (§6).

#### `community` (exact name). Each row is `[id, title, author, thumbnail, total, whitelisted, viewCount]`, decoded into `SubCommunity` (`SubsetDecoder.kt:154-164`).

- `author` and `thumbnail` are nullable. `total` and `whitelisted` are required ints. `viewCount` is a
  nullable long, but the column must be present.
- `thumbnail` is never displayed. Covers are derived from the first member's `mqdefault` frame
  (`SubsetCategories.kt:340`, `SubsetReadLayer.kt:358`).

#### `communitytracks` (exact name). Each row is `[playlistId, videoId, pos, artistId]`, decoded into `SubCommunityTrack` (`SubsetDecoder.kt:166-168`).

- `artistId` is nullable. It is the discovery-resolved artist for a member that has no corpus track.

#### `homerank` (exact name). An **array of objects**, decoded into `SubHomeRank` (`SubsetDecoder.kt:170-180`).

| Key | Type | Req. | Notes |
|---|---|---|---|
| `row` | string | yes | `"top-albums"`, `"top-videos"` or `"top-artists"`. `"top-community"` rows are decoded but **unused**: that row is computed from the `community` shards (`SubsetReadLayer.kt:262-316`). |
| `kind` | string | yes | `"album"`, `"video"` or `"artist"`. Only the whitelist overlay reads it (`SubsetLiveWhitelist.kt:71-79`). |
| `refId` | string | yes | Album id, videoId or artist id. |
| `artistId` | string | nullable / omittable | Card owner. Rows whose `artistId` is missing from the live whitelist are dropped (`SubsetLiveWhitelist.kt:79`). |
| `pos` | int | yes | Order within the row. Sorted ascending (`SubsetCorpus.kt:57-59`). |
| `score` | double | nullable / omittable | Decoded but **not used** by the read layer. |

#### `zemer` (exact name). **One object**: `{"playlists":[…], "items":[…]}`. Both keys may be omitted, meaning empty (`SubsetDecoder.kt:182-193`).

- `playlists[]` = `{id: string, title: string, pos: int, year: int|null}` (decoded into
  `SubZemerPlaylist`).
  - `id` is a slug, for example `auto-top-50`, and must also be a valid URL path part.
  - A non-null `year` makes the playlist a **dynamic year rule**: every track whose release date falls in
    that year (`SubsetReadLayer.kt:496-504`).
- `items[]` = `{playlistId: string, kind: "track"|"album", refId: string, pos: int}` (decoded into
  `SubZemerItem`). An album item expands to the album's tracks (`SubsetReadLayer.kt:505-518`).

#### `blocked` (exact name). **One object**: `{"global":[ids], "female":[ids]}`. Both keys are optional (`SubsetDecoder.kt:240-244`).

#### Optional additive shards (2026-09-11) (`docs/offline/README.md:27-45`)

| Shard | JSON | Decoder | Populates |
|---|---|---|---|
| `synonyms` | `[["form","form",…],…]`. Groups with fewer than 2 forms are ignored. | `SubsetDecoder.kt:248-249`, `offline/SubsetSynonyms.kt:53-64` | `synonymGroups`. If absent, falls back to 3 built-in groups (`SubsetSynonyms.kt:24-28`). |
| `genrecatalog` | `{genres:[[slug,title,kind]], kinds:[[id]], podcastGenres:[[slug,title,kind|null]], podcastKinds:[[id,title]]}` | `SubsetDecoder.kt:254-266` | `genreCatalog`. Music entries have **no offline consumer** (`docs/offline/README.md:37-39`). |
| `lyricsflags` | `[[videoId, bits]]`. bit0 = verified text, bit1 = line timings, bit2 = certified timings. | `SubsetDecoder.kt:269-270`, `SubsetCorpus.kt:35` | `lyricsFlags`. Only a lyrics-prefetch hint for downloads (`docs/offline/README.md:40-43`). |
| `realvideos` | `["videoId",…]` | `SubsetDecoder.kt:273-274` | `realVideos`, which sets `ZemerTrack.realVideo` on home top-videos and radio (`SubsetReadLayer.kt:303,903`). |
| `radio-<n>` | `[[videoId, pop|null, [[id,w],…], [[id,w],…]]]`. Columns 2 and 3 are optional, but **each weight `w` must be a number** (it is asserted non-null). | `SubsetDecoder.kt:278-284` | `radioRows` (co-occurrence and popularity for offline radio). |

#### Podcast shards (not needed for music)

| Shard | Row layout | Source |
|---|---|---|
| `podcastchannels` | `[id, name, thumbnail, flags(bit0 female, bit1 kidZone, bit2 verified), showCount, episodeCount]` | `SubsetDecoder.kt:195-209` |
| `podcasts` | `[id, name, author, channelId, thumbnail, episodeCountText, genres?, isKidZone?]` | `SubsetDecoder.kt:211-226` |
| `podcastepisodes-<n>` | `[videoId, showId, title, thumbnail, durationSec, publishedAt]` | `SubsetDecoder.kt:228-238` |

### 2.6 Required and optional shards

- **The decoder requires no shard.** A manifest with `"shards": []` produces an empty but non-null
  corpus (`SubsetDecoder.kt:35-92`), and every read then returns empty results or 404.
- **What is mandatory:** every shard the manifest lists must be present on disk and hash-correct
  (`SubsetDecoder.kt:56-63`).

| Feature | Needs |
|---|---|
| Search (artists, songs, videos, albums, singles) | `artists`, `tracks-*`, `albums-*` |
| Artist page | `artists`, `tracks-*`, `albums-*`; `playlists` is optional |
| Album page | `albums-*`, `albumtracks-*`, `tracks-*`, `artists` |
| Home: Featured Albums, Videos, Artists | `homerank` plus the referenced `albums-*`, `tracks-*` and `artists` rows |
| Home: Featured Playlists | `community` and `communitytracks` (with `tracks-*` durations), plus engagement lists in `zemer`. Opening them is live-only (§6). |
| Zemer Playlists (list and detail) | `zemer`, `tracks-*`, `artists`, and `albums-*`/`albumtracks-*` for album items or year rules |
| Offline radio | `tracks-*` and `artists` (popularity falls back to `playCount`); `radio-*` is optional (`SubsetRadio.kt:81-107`) |
| Id-level hides | `blocked` |
| Better synonyms, real-video hero, lyrics prefetch | `synonyms`, `realvideos`, `lyricsflags` |

### 2.7 How `SubsetCorpus` is assembled

**Shard to corpus field** (`SubsetDecoder.kt:87-92`, `SubsetCorpus.kt:12-39`):

| Shard | Corpus field |
|---|---|
| `artists` | `artists` |
| `tracks-*` | `tracks` |
| `albums-*` | `albums` |
| `albumtracks-*` | `albumTracks` |
| `playlists` | `artistPlaylists` |
| `community` | `community` |
| `communitytracks` | `communityTracks` |
| `homerank` | `homeRank` |
| `zemer` | `zemerPlaylists` and `zemerItems` |
| `blocked` | `blocked` |
| `podcastchannels`, `podcasts`, `podcastepisodes-*` | `podcastChannels`, `podcasts`, `podcastEpisodes` |
| `synonyms` | `synonymGroups` |
| `genrecatalog` | `genreCatalog` |
| `lyricsflags` | `lyricsFlags` |
| `realvideos` | `realVideos` |
| `radio-*` | `radioRows` |

Derived maps are built lazily (`SubsetCorpus.kt:40-80`).

**Live-whitelist overlay at load** (`SubsetLiveWhitelist.kt:32-89`, called from `OfflineReadProvider.kt:71,77`).
The `live` map is `WhitelistCache` (artist id to `isFemale`), filled from `/whitelist`:

- **If `live` is empty** (whitelist never synced), the overlay does nothing.
- **An artist not in `live` is dropped**, along with:
  - its tracks, albums and album memberships
  - its artist playlists
  - community members resolved to it
  - `homerank` rows that reference dropped content, or whose `artistId` is not in `live`
  - `zemer` items that reference dropped tracks or albums
- **`isFemale` is replaced by the live flag.** `isChasid` and `isKidZone` always come from the shard.

The live whitelist is therefore the source of truth for both membership and the female flag.

### 2.8 Minimal valid example: 2 artists, 1 album, 3 tracks

Generated with Python's `gzip.compress(raw, compresslevel=9, mtime=0)`.

- **The hash depends on the exact gzip bytes.** A different compressor gives different bytes and needs
  its own hashes. Always hash the file you actually serve.
- JSON is minified, UTF-8, `ensure_ascii=False`.

**Shard contents (before gzip):**

```
artists        [["UCexampleArtistAAAAAAAAA","Avraham Example","https://yt3.ggpht.com/exampleA=s800",0],["UCexampleArtistBBBBBBBBB","Example Choir","https://yt3.ggpht.com/exampleB=s800",2]]
tracks-0       [["vid00000001","Song One","UCexampleArtistAAAAAAAAA",0,215,120000,"2024-03-01T00:00:00Z"],["vid00000002","Song Two","UCexampleArtistAAAAAAAAA",0,190,80000,"2024-03-01T00:00:00Z"],["vid00000003","Live Video","UCexampleArtistBBBBBBBBB",1,300,null,"2025-01-10T00:00:00Z"]]
albums-0       [["MPREb_example01","OLAK5uy_example01","First Album","UCexampleArtistAAAAAAAAA","album",2024,"https://lh3.googleusercontent.com/exampleAlbum=w544-h544","2024-03-01T00:00:00Z"]]
albumtracks-0  [["MPREb_example01","vid00000001",0],["MPREb_example01","vid00000002",1]]
homerank       [{"row":"top-albums","kind":"album","refId":"MPREb_example01","artistId":"UCexampleArtistAAAAAAAAA","pos":0,"score":null},{"row":"top-videos","kind":"video","refId":"vid00000003","artistId":"UCexampleArtistBBBBBBBBB","pos":0,"score":null},{"row":"top-artists","kind":"artist","refId":"UCexampleArtistAAAAAAAAA","artistId":null,"pos":0,"score":null},{"row":"top-artists","kind":"artist","refId":"UCexampleArtistBBBBBBBBB","artistId":null,"pos":1,"score":null}]
zemer          {"playlists":[{"id":"auto-top-50","title":"Top 50","pos":0,"year":null}],"items":[{"playlistId":"auto-top-50","kind":"track","refId":"vid00000003","pos":0},{"playlistId":"auto-top-50","kind":"album","refId":"MPREb_example01","pos":1}]}
blocked        {"global":[],"female":[]}
```

**`/subset/manifest`:**

```json
{
  "v": 1,
  "builtAt": "2026-10-29T00:00:00Z",
  "schema": 1,
  "shards": [
    {"name": "artists",       "hash": "393cd2792e9d7017", "bytes": 114},
    {"name": "tracks-0",      "hash": "b5414aac9f0ed60e", "bytes": 151},
    {"name": "albums-0",      "hash": "aad41f4309004a23", "bytes": 152},
    {"name": "albumtracks-0", "hash": "dd91ba9ed24b81c0", "bytes": 59},
    {"name": "homerank",      "hash": "f9dceaf9cba988c5", "bytes": 167},
    {"name": "zemer",         "hash": "73e3998b3f38516f", "bytes": 156},
    {"name": "blocked",       "hash": "706f5944b1bd0346", "bytes": 42}
  ]
}
```

**Full hex of the served `/subset/blocked` file (42 bytes):**

```
1f 8b 08 00 00 00 00 00 02 ff ab 56 4a cf c9 4f
4a cc 51 b2 8a 8e d5 51 4a 4b cd 4d cc 49 05 b1
6b 01 e7 dd 8b 72 19 00 00 00
```

- `sha256` of these bytes = `706f5944b1bd0346a4744e23381c6cdb…`. The manifest `hash` is the first 16
  hex characters.
- The test vectors in `app/src/test/.../SubsetSyncTest.kt:256-260` let you check your implementation:
  `sha256("")` gives `e3b0c44298fc1c14`, and `sha256("abc")` gives `ba7816bf8f01cfea`.

**Matching content-mirror files:** the `/whitelist` example in §1.2 (both ids), `/whitelist/version` set
to `{"gate":1761696000000}`, and `/blockedContentIds` set to `{"global":[],"female":[]}`.

**What this example produces offline** (with `allowFemale` and `blockVideos` both false):

| Surface | Result |
|---|---|
| `/album?id=MPREb_example01` | Tracks 1 and 2, `trackNumber` 1 and 2 |
| `/artist?id=UCexampleArtistAAAAAAAAA` | Songs [Song One (120000 plays), Song Two]; Albums [First Album] |
| `/artist?id=UCexampleArtistBBBBBBBBB` | Videos [Live Video] |
| `/home-rows` | topAlbums = [First Album], topVideos = [Live Video], topArtists = [A, B], topCommunity = [] |
| `/zemer-playlists` | One card `auto-top-50` with tracks [vid3, vid1, vid2], `rank` 1 for vid3 |
| `/search?q=song` | Songs [Song One, Song Two] |

---

## 3. Which fields drive which feature

### 3.1 The shared content gate

Every offline surface uses the same predicate, `contentGatePasses(femaleInvolved, isKidZone, isVideo, allowFemale, blockVideos, kidZone)`
(`SubsetReadLayer.kt:79-86`), plus `idDropped`. `idDropped` drops `blocked.global` always and
`blocked.female` when `!allowFemale` (`SubsetReadLayer.kt:139-141`).

**Female involvement:**

- A track counts as female-involved when its artist is `isFemale`, **or** its title or artist string
  credits a known female artist, **or** it is in `blocked.female` (`SubsetFemale.kt:113-135`,
  `SubsetReadLayer.kt:104-109`).
- A title credit is text after `feat.`, `ft.` or `featuring`, or `with`/`duet` inside parentheses
  (`SubsetFemale.kt:88-103`).
- Names are matched against female artists' normalized names, and against consonant skeletons in the
  other script (`SubsetFemale.kt:51-77`).

**Request flags:**

- `allowFemale` comes from the user's `allowFemaleSingers` setting.
- `blockVideos` is always sent as `false` (`search/ZemerSearchOptions.kt:21-30`).
- The offline music reads pin `kidZone = false` (`OfflineReadProvider.kt:85-113`), so **`isKidZone` has
  no effect on offline music surfaces.** Kid Zone browsing uses the whitelist's `isKidZone` instead.
- `isChasid` is used only by the radio class bonus (`SubsetRadio.kt:93,217`).

### 3.2 Search (`offlineSearch`, `SubsetCategories.kt:422-440`)

- **Nine category indexes.** Artist docs use `title = name` and an empty `artistName`. Track and album
  docs use the title plus the artist's `name`. Artist-playlist docs use the title plus the owning
  artist's name. Community docs use the title only (`SubsetCategories.kt:28-69,271-399`).
- **Songs and videos** are split by the track `isVideo` flag. **Albums and singles** are split by
  `type == "single"`.
- **Ranking uses text only:**
  - plain and Hebrew-skeleton token match, with prefix (last word only) and typo tolerance
  - IDF weighting and a coverage gate
  - exact / begins-with boosts on title and artist
  - artist affinity on multi-word queries
  - a precision floor at 0.4 of the top score
  - tie-breaks on shorter title, then id (`offline/SubsetSearch.kt:239-320`, `offline/SubsetNormalize.kt:60-85`)
- **`playCount`, `year` and dates do not affect search order.**
- **Synonyms** come from the `synonyms` shard (`SubsetCategories.kt:398`).
- **Result counts:** per category, the top `k`. The client uses `k = 8` for the summary, 100 for chips and
  500 for the community chip (`search/ZemerSearchRepository.kt:500-506`).
- **Track rows carry no thumbnail.** The app uses `https://i.ytimg.com/vi/<id>/hqdefault.jpg`
  (`ZemerResultMapper.kt:59,75`). Album and artist rows use the shard thumbnails.

### 3.3 Artist page (`offlineArtist`, `SubsetReadLayer.kt:209-258`)

- **Not found (404, null)** when the id is blocked, unknown, or fails the gate on its own `isFemale`.
- **Header:** `{id, name, thumbnail}` from `artists`.
- **Songs:** non-video tracks with `artistId == id`, sorted by `playCount` descending with nulls last.
  Female-credited tracks are hidden when `!allowFemale`.
- **Videos:** the same tracks with `isVideo`.
- **Albums and singles:** split by `type`. Sorted by `year` descending with nulls last, then by id.
- **Playlists:** from `playlists`.
- **Only primary-artist tracks appear.** Tracks where the artist is merely featured do not.

### 3.4 Album page (`offlineAlbum`, `SubsetReadLayer.kt:151-194`)

- **Not found** when the album is unknown or blocked, or its artist fails the gate.
- **Tracks:** `albumtracks` in `pos` order, with `trackNumber = pos + 1`, each track filtered by the gate.
- **Header:** `id`, `playlistId`, `title`, artist `name`, `year`, `thumbnail`. The album `thumbnail` is
  also every track's art (`ZemerResultMapper.kt:283-284`).

### 3.5 Home rows (`offlineHomeRows`, `SubsetReadLayer.kt:268-316`)

- **Featured Albums, Videos and Artists:** the `homerank` order for `top-albums`, `top-videos` and
  `top-artists`, gated and id-filtered.
  - `top-videos` keeps only tracks with `isVideo = true` (`:292`).
  - `realVideo` comes from the `realvideos` shard. It filters only the hero carousel
    (`docs/home_rows/README.md:61-64`).
- **Featured Playlists** (`topCommunity`) is computed from `community` (`SubsetReadLayer.kt:318-368`).
  A list qualifies when:
  - `viewCount` is not null
  - its member runtime from `tracks-*` durations is at least 2400 s
  - it contains a member of `auto-top-50`, `auto-trending` or `auto-favorites`, if those curated lists
    exist
  - Qualifying lists are ordered by `viewCount` descending, top 80 considered, at most 32 shown.
- **App side:**
  - The ViewModel applies `rotateByArtist(maxPerArtist = 1, target = 20)` (`docs/home_rows/README.md:87-89`).
    **A row needs at least 20 distinct artists to fill.** Cards need `artistId`, which the read layer
    supplies.
  - The ranked gate also drops Israeli-registry artists and `isKids` artists (`docs/home_rows/README.md:73-77`).
  - A new user's Quick Picks are seeded from the `auto-top-50` curated playlist (`docs/home_rows/README.md:17-18`).
    Publish one.

### 3.6 Zemer (curated) playlists (`SubsetReadLayer.kt:412-541`)

- **List order:** `pos`, then `id`. A playlist with no surviving track is hidden.
- **Detail:**
  - Item rule: direct tracks first, then album expansions in album order; the first occurrence of a track
    wins.
  - Year rule: release year match, where the track's release date is
    `track.uploadDate ?: max(album.uploadDate)`.
  - `fromAlbum` marks tracks reached through an album.
  - For ids starting `auto-`, `rank` is the 1-based position in the raw stored order.
  - Chart-movement badges and `anchorDate` are live-only (`SubsetReadLayer.kt:51-53`).
- **Cover:** always `https://search.zemer.io/zemer-playlists/cover?id=<id>`, a server-generated SVG
  (`SubsetReadLayer.kt:88-93`, `docs/zemer_playlists/README.md:37-39`). See §6.

### 3.7 Radio (offline, `SubsetRadio.kt`, `SubsetReadLayer.kt:861-908`)

- Kinds `song`, `artist`, `album`, `playlist` and `shuffle` work. The Home shuffle button uses
  `shuffle` (`docs/home_rows/README.md:81-83`).
- `genre` returns null, and the call rethrows (`SubsetRadio.kt:134`).
- **Popularity:** `radio-*` `pop`, otherwise `playCount` (`SubsetRadio.kt:96-102`).
- **Era bonus:** from `uploadDate`.

### 3.8 Minimum data for "reasonable" search, artist, album and home

1. A `/whitelist` with every artist, including `displayName` and `thumbnail`.
2. `artists`: every whitelisted artist with a correct female bit and a thumbnail.
3. `tracks-*`: the real YouTube `videoId`, title, `artistId`, `isVideo`, `durationSec`, and `playCount`
   (for artist "top songs" order and radio).
4. `albums-*` and `albumtracks-*`: `type`, `year`, `thumbnail`, `playlistId`.
5. `homerank`: 20 or more distinct-artist entries in each of `top-albums`, `top-videos` (real videos with
   `isVideo = 1`) and `top-artists`.
6. `zemer`: at least `auto-top-50`, for the Quick Picks seed.
7. `blocked`: the same content as `/blockedContentIds`.

Leave `playlists`, `community` and `communitytracks` out until `/playlist` is solved (§6).

---

## 4. Live server versus offline snapshot

### 4.1 Current routing

`serverOrOffline` (`search/ZemerSearchRepository.kt:93-101`):

- It calls `server()` first.
- If that throws an exception for which `isZemerServerUnreachable()` is true, it returns `offline()`, or
  rethrows when the snapshot is null.
  - `isZemerServerUnreachable()` means `IOException` or `UnresolvedAddressException`
    (`ZemerSearchRepository.kt:82-83`).
- A server **404 returns `null` without falling back.** This applies to `/album`, `/artist`,
  `/zemer-playlists?id=` and podcast details (`search/ZemerSearchClient.kt:216-219,246,273,298,477`).
- The search LRU caches only server responses (`ZemerSearchRepository.kt:479-494`).

**Call sites of `serverOrOffline`** (all in `search/ZemerSearchRepository.kt`):

| Line | Function | Endpoint |
|---|---|---|
| 180 | `album` | `/album` |
| 194 | `homeRows` | `/home-rows` |
| 210 | `artist` | `/artist` |
| 226 | `kidZonePodcasts` | `/podcasts?kidZone=1` |
| 233 | `podcast` | |
| 246 | `podcastChannel` | |
| 279 | `podcastsNewEpisodes` | |
| 295 | `podcastGenres` | |
| 313 | `podcastGenre` | |
| 326 | `radio` | |
| 382 | `curatedPlaylists` | |
| 395 | `curatedPlaylist` | |
| 487 | `fetch` | `/search`, used by `summary`, `filtered` and `suggestions` at `:134-145` |

**Direct live-only client calls (no offline path):**

| Line | Function | Endpoint |
|---|---|---|
| 155 | `playlist` | `/playlist` |
| 262 | `podcastChannelEpisodes` | |
| 271 | `podcastHomeRows` | |
| 275 | `videoHomeRows` | |
| 343 | `radioContinuation` | live tokens only |
| 355 | `stations` | |
| 363 | `stationTuneIn` | |
| 436 | `genres` | |
| 453 | `genre` | |
| 465 | `genreFacet` | |

### 4.2 Smallest change to always use the offline layer

**Option A: one function, the smallest change.** Replace the body of `serverOrOffline`
(`ZemerSearchRepository.kt:93-101`):

```kotlin
internal suspend fun <T> serverOrOffline(server: suspend () -> T, offline: suspend () -> T?): T =
    offline() ?: throw java.io.IOException("Offline snapshot unavailable")
```

- **Trade-off:** `OfflineReadProvider` returns `null` both for "no snapshot" and for "not found". So an
  unknown album, artist or curated id now shows the **error / retry state** instead of the not-available
  state.
- The "404 means delete the stale local album" logic (`ZemerSearchRepository.kt:175-177`) will no longer
  fire.
- `search/ZemerSearchRoutingTest` (not extracted, `docs/offline/README.md:130-131`) will need updating.

**Option B: keeps the 404 semantics.**

1. Add `suspend fun hasSnapshot(): Boolean = withContext(Dispatchers.IO) { snapshot() != null }` to
   `OfflineReadProvider`.
2. Give `serverOrOffline` access to it, for example by making it a repository member.
3. Use:
   ```kotlin
   if (!offlineReads.hasSnapshot()) throw IOException(…)
   return offline() ?: throw IOException(…)  // for non-null T
   ```
   For nullable `T` (album, artist, curatedPlaylist), return `offline()` as is.

**Required alongside A or B.** Without these the offline layer returns null for everything:

1. **Snapshot host.** `offline/SubsetSyncClient.kt:57`: replace `ZemerSearchClient.BASE_URL` with your
   host, for example `const val BASE_URL = "https://static.example.org"`.
2. **Sync on by default.**
   - `offline/OfflineSubsetSyncer.kt:99`: change `?: false` to `?: true`.
   - `:161`: change `!= true` to `== false`.
   - The first launch then downloads the snapshot through the existing `maybeSync()` at app start
     (`docs/offline/README.md:106-107`).
   - Until that first sync completes, every music surface shows its error state.
3. **Freshness cap (optional).** Consider raising `SUBSET_MAX_SNAPSHOT_AGE_MS` (14 days,
   `offline/SubsetLiveWhitelist.kt:11`). Every successful manifest check refreshes the timestamp, even
   with no changes (`OfflineSubsetSyncer.kt:130-131`). A device that cannot reach your host for 14 days
   loses all music browsing.
4. **Curated covers.** `offline/SubsetReadLayer.kt:93`:
   - The covers still resolve against `search.zemer.io`.
   - Point them at a static path you publish, for example `"$YOUR_HOST/zemer-playlists/cover/$id.svg"`.
     Serve them as `image/svg+xml`, which Coil's `SvgDecoder` decodes (`docs/zemer_playlists/README.md:37-39`).
5. **Content mirror.** Set the gradle `CONTENT_MIRROR_URL` build-config field to your host. The gradle
   file is not extracted.
   - Optionally remove the Firebase `firebase = { … }` lambdas in `utils/WhitelistFetcher.kt:22-29,71-119,133-150`.
     Otherwise a mirror failure silently reads the original Firestore project.

**The live-only calls listed above stay unchanged** and keep hitting `search.zemer.io`. If that host is
gone they fail. Their screens either hide (home rows; `docs/home_rows/README.md:29-31`) or show errors.

**Kid Zone search:** offline search ignores `options.kidZone` (`OfflineReadProvider.kt:87` versus
`ZemerSearchRepository.kt:489`).

---

## 5. Lyrics

### 5.1 What the app calls

**Chain.** `LyricsStore.ensure` calls `LyricsHelper.getLyrics(mediaMetadata)` (`lyrics/LyricsStore.kt:46-54,75-84`).

- **Offline:** if there is no network, the helper returns not-found immediately
  (`lyrics/LyricsHelper.kt:43-54`).
- **Prefetch:** runs at track start and is skipped when offline (`docs/lyrics/README.md:171-175`).

**Default provider order** (`lyrics/LyricsProviderRegistry.kt:18-28`). Each can be toggled off in
settings, where an absent setting means enabled (`lyrics/LyricsProvider.kt:25`):

| Order | Provider | Request | Trust |
|---|---|---|---|
| 1 | **Zemer** | `GET {ZEMER_LYRICS_BASE_URL}/lyrics/resolve?videoId=<id>` with `Accept: application/json`. Only HTTP 200 counts; anything else returns null (`lyrics/zemer/ZemerLyricsClient.kt:115-120`). The base URL defaults to `https://search.zemer.io` (gradle `-PzemerLyricsBaseUrl`, `docs/lyrics/README.md:19-20`). A successful reply contains source **pointers**, and the app then fetches third-party sites itself (jkaraoke, apple via paxsenix, simpmusic, lrclib `/api/get/<id>`, kugou, shironet and others) (`lyrics/zemer/ZemerLyricsProvider.kt:32-70`). | trusted |
| 2 | **SimpMusic** | `GET https://api-lyrics.simpmusic.org/v1/<videoId>` (`lyrics/simpmusic/SimpMusicLyrics.kt:19,27-31`) | trusted |
| 3 | **LrcLib** | `GET https://lrclib.net/api/search` with `track_name`/`artist_name`/`album_name` or `q`, up to 5 strategies, then an identity gate (title and artist similarity at least 0.75, duration within 3 s) and a sync gate (within 1 s) (`lyrics/lrclib/LrcLib.kt:53-169`) | trusted |
| 4–5 | **YouTube Subtitle / YouTube Music** | InnerTube transcript / lyrics tab (`lyrics/youtube/*.kt`) | low trust |

**Other Zemer lyrics endpoints:**

- `POST /lyrics/extras`: only once the user picks a translation language; the default is OFF
  (`ZemerLyricsClient.kt:128-136`, `docs/lyrics/README.md:87-104`).
- `POST /lyrics/submit` and `POST /lyrics/report`: user feedback (`ZemerLyricsClient.kt:167-183`).
- `GET /lyrics/resolve`: also called for line extras (`lyrics/LyricsStore.kt:52`).

**How the walk proceeds** (`lyrics/LyricsChainWalk.kt:21-30`, `lyrics/SyncedFirstPicker.kt:18-34`):

1. The primary trusted provider (Zemer) runs **alone** first. A synced answer ends the walk there.
2. The remaining trusted providers run concurrently.
3. The low-trust providers run only if no trusted provider answered.
4. Timeouts: 15 s request, 10 s connect, 15 s socket (`lyrics/LyricsHttp.kt:21`).

### 5.2 Does LRCLIB work without `search.zemer.io`?

**Yes.** `LrcLibLyricsProvider` calls `lrclib.net` directly using the player's title, artist, duration and
album (`lyrics/lrclib/LrcLibLyricsProvider.kt:11-17`). It does not depend on the Zemer resolver.
SimpMusic is independent as well.

**What goes wrong if `search.zemer.io` disappears but the app is unchanged:**

- **DNS failure.** `resolve` throws. `getLabeledLyrics` turns that into `Result.failure`
  (`ZemerLyricsProvider.kt:125-129`). The error is not a `LyricsUnavailableException`, so `LyricsHelper`
  calls `reportException` **on every song** (`LyricsHelper.kt:75-80`). The chain still continues to
  SimpMusic and LrcLib.
- **Hanging host.** Every song waits up to 15 s before the other providers are asked, because Zemer runs
  alone first.
- **The `lyricsflags` shard is only a prefetch hint** and never gates a provider
  (`docs/offline/README.md:40-43`).

**Recommended change (any one of these):**

1. Build with `-PzemerLyricsBaseUrl=https://<your static host>` (the field is `ZemerLyricsClient.kt:115`).
   - A static host answers `GET /lyrics/resolve?…` with 404. That becomes `null`, then
     `LyricsUnavailableException`, so the miss is silent and fast (`ZemerLyricsProvider.kt:126`).
   - `POST /lyrics/extras` returning 404 means "no extras" (`ZemerLyricsClient.kt:133`).
   - submit and report return `false` harmlessly.
2. Remove `"Zemer"` from `providerMap` (`LyricsProviderRegistry.kt:23`). Old saved orders tolerate unknown
   names (`LyricsProviderRegistry.kt:36-41`).
3. Default `EnableZemerLyricsKey` to off. This needs `isEnabled` to change, since an absent key currently
   means enabled (`LyricsProvider.kt:25`).

**Serving real Zemer lyrics statically** would need a code change to a path form, for example
`/lyrics/resolve/<videoId>.json`, because most static hosts ignore the query string. The response is
`ZemerLyricsClient.Resolved` (`ZemerLyricsClient.kt:96-106`). The simplest useful static payload:

```json
{"videoId":"vid00000001","verified":true,"sources":[{"type":"manual","plain":"line 1\nline 2\nline 3\nline 4"}]}
```

- This works because the `manual` type serves `syncedLrc` or `plain` inline (`ZemerLyricsProvider.kt:64,79`).
- `lrclib` pointers (`{"type":"lrclib","trackId":123}`) make the app fetch `https://lrclib.net/api/get/123`
  (`ZemerLyricsProvider.kt:62`).

---

## 6. What cannot be served from static files (or needs a code change)

| Need | Why | Source |
|---|---|---|
| `GET /playlist?id=` | Opens **any** YouTube playlist, filtered server-side. It is the open path for community and artist-playlist cards (`zemerPlaylistRoute` uses `?zemer=true`, which means `/playlist`). Live-only, with no offline method. Omit the `community`/`playlists` shards, or the cards will dead-end. | `search/ZemerSearchRepository.kt:154-168`, `search/ZemerRoutes.kt:7-15`, `offline/OfflineReadProvider.kt:40-43` |
| `/zemer-playlists/cover?id=` | A server-generated SVG keyed by **query string**. Pre-rendered SVGs are fine, but the URL needs the code change in §4.2 item 4 (or a host that routes on the query). | `offline/SubsetReadLayer.kt:88-93` |
| `/genres` (catalog, page, facet) | Live-only by design. Per-track genre membership is not in the subset. Home genre chips and pages go away. | `ZemerSearchRepository.kt:418-465`, `docs/offline/README.md:37-39` |
| `/radio` `kind=genre` and live continuations | No genre membership. Live tokens need a server session. The other kinds work offline. | `offline/SubsetRadio.kt:134`, `ZemerSearchRepository.kt:338-345` |
| `/stations`, `/station` | A synchronized, clock-dependent broadcast. | `ZemerSearchRepository.kt:347-365` |
| `/video-home-rows`, `/podcast-home-rows` | Live-only in the code. They are plain GETs, so a static JSON would technically load, but it would ignore the `allowFemale` flag server-side. **Ambiguous:** the client-side ranked gate re-checks female and Israeli artists (`docs/home_rows/README.md:73-77`), but whether that fully covers these responses is not visible in the extracted code. | `ZemerSearchRepository.kt:270-275` |
| Lyrics resolve, extras, submit, report | Per-video dynamic responses and POST writes (§5). | `lyrics/zemer/ZemerLyricsClient.kt:117-183` |
| The ranking inputs themselves (`homerank` order, `playCount`, `radio-*` co-occurrence) | Must be computed by **your** build pipeline. Nothing in the extracted code sends listening telemetry to a backend you control. | (inference) |
| Audio playback and most images | Playback is InnerTube by `videoId`. Thumbnails are `i.ytimg.com`, `yt3` and `lh3` URLs. Your data must use real YouTube ids. | `docs/offline/README.md:5-6`, `ZemerResultMapper.kt:59` |
| Mirror fallback | Any mirror failure falls back to the original Firebase project, which your backend cannot control (§4.2 item 5). | `utils/ZemerContentClient.kt:243-257` |
| Not in extracted files (unknown) | `App.kt` (startup sync), `IsraeliArtistRegistry`, `HomeViewModel`, "Latest Releases" (the flipphoneguy feed, `docs/home_rows/README.md:24`), gradle `BuildConfig` values. | |
