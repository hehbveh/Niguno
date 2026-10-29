"""Pure snapshot building: catalog + crawl data -> the files the app downloads.

No network and no file I/O here, so it is fully unit-tested. Wire formats follow docs/APP_CONTRACT.md
(the app's ZemerContentClient and offline subset decoder).
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field

PARTITION_ROWS = 5000          # rows per tracks-/albums-/albumtracks- shard
HOME_ROW_SIZE = 60             # entries per home row (app rotates to 1 per artist, 20 shown)
TOP_PLAYLIST_SIZE = 50
SCHEMA = 1                     # SUPPORTED_SUBSET_SCHEMA in the app

FLAG_FEMALE, FLAG_CHASID, FLAG_KIDZONE = 1, 2, 4
TRACK_FLAG_VIDEO = 1


# ---------------------------------------------------------------- encoding helpers

def dumps(value) -> bytes:
    """Minified UTF-8 JSON (Hebrew kept as-is), the exact form the shards are built from."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def gzip_bytes(raw: bytes) -> bytes:
    """Deterministic gzip (mtime=0) so an unchanged shard keeps its hash and is not re-downloaded."""
    return gzip.compress(raw, compresslevel=9, mtime=0)


def shard_hash(data: bytes) -> str:
    """First 16 lowercase hex chars of SHA-256 over the served (gzipped) bytes - SubsetHash.kt."""
    return hashlib.sha256(data).hexdigest()[:16]


def partitions(rows: list, prefix: str) -> list[tuple[str, list]]:
    """Prefixed shards must carry a dash suffix (`tracks-0`); a bare `tracks` is ignored by the app."""
    chunks = [rows[i:i + PARTITION_ROWS] for i in range(0, len(rows), PARTITION_ROWS)] or [[]]
    return [(f"{prefix}-{n}", chunk) for n, chunk in enumerate(chunks)]


# ---------------------------------------------------------------- model

@dataclass
class Track:
    video_id: str
    title: str
    artist_id: str
    is_video: bool
    duration_sec: int | None
    play_count: int | None
    upload_date: str | None


@dataclass
class Album:
    id: str
    playlist_id: str | None
    title: str
    artist_id: str
    type: str                 # "album" | "single" | "ep"
    year: int | None
    thumbnail: str | None
    upload_date: str | None
    track_ids: list = field(default_factory=list)


@dataclass
class Snapshot:
    """Everything published, as path -> bytes, plus the manifest summary."""
    files: dict[str, bytes]
    manifest: dict
    stats: dict


class CatalogError(ValueError):
    pass


# ---------------------------------------------------------------- catalog validation

def validate_catalog(artists: list[dict], blocked: dict) -> None:
    if not artists:
        # The app treats an empty /whitelist as an outage and falls back to another source.
        raise CatalogError("catalog/artists.json is empty: add at least one artist")
    seen = set()
    for a in artists:
        aid = (a.get("id") or "").strip()
        if not aid.startswith("UC") or len(aid) != 24:
            raise CatalogError(f"bad channel id {aid!r} (expected UC + 22 chars) for {a.get('name')!r}")
        if aid in seen:
            raise CatalogError(f"duplicate artist id {aid}")
        seen.add(aid)
        if not (a.get("name") or "").strip():
            raise CatalogError(f"artist {aid} has no name")
    for key in ("global", "female"):
        if not isinstance(blocked.get(key, []), list):
            raise CatalogError(f"blocked.json: {key!r} must be a list")


# ---------------------------------------------------------------- normalisation of crawl data

def collect(artists: list[dict], crawls: dict[str, dict], blocked_global: set[str]):
    """Merge per-artist crawl files into unique tracks and albums (first artist in catalog order wins)."""
    tracks: dict[str, Track] = {}
    albums: dict[str, Album] = {}
    for a in artists:
        crawl = crawls.get(a["id"])
        if not crawl:
            continue
        for t in crawl.get("tracks", []):
            vid = t.get("videoId")
            if not vid or vid in tracks or vid in blocked_global or not t.get("title"):
                continue
            tracks[vid] = Track(vid, t["title"], a["id"], bool(t.get("isVideo")), _int(t.get("durationSec")),
                                _int(t.get("playCount")), t.get("uploadDate"))
        for al in crawl.get("albums", []):
            aid = al.get("id")
            if not aid or aid in albums or aid in blocked_global or not al.get("title"):
                continue
            year = _int(al.get("year"))
            upload = al.get("uploadDate") or (f"{year}-01-01T00:00:00Z" if year else None)
            album = Album(aid, al.get("playlistId"), al["title"], a["id"], _album_type(al.get("type")), year,
                          al.get("thumbnail"), upload)
            for t in al.get("tracks", []):
                vid = t.get("videoId")
                if not vid or vid in blocked_global or not t.get("title"):
                    continue
                if vid not in tracks:
                    tracks[vid] = Track(vid, t["title"], a["id"], bool(t.get("isVideo")),
                                        _int(t.get("durationSec")), _int(t.get("playCount")), upload)
                elif tracks[vid].upload_date is None:
                    tracks[vid].upload_date = upload
                if vid not in album.track_ids:
                    album.track_ids.append(vid)
            albums[aid] = album
    return tracks, albums


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _album_type(t) -> str:
    t = (t or "album").strip().lower()
    return t if t in ("album", "single", "ep") else "album"


# ---------------------------------------------------------------- ranking (home rows, curated lists)

def _plays(t: Track) -> int:
    return t.play_count or 0


def home_rank(tracks: dict[str, Track], albums: dict[str, Album], artist_ids: list[str]) -> list[dict]:
    rows = []
    album_plays = {a.id: sum(_plays(tracks[v]) for v in a.track_ids if v in tracks) for a in albums.values()}
    top_albums = sorted(albums.values(), key=lambda a: (-album_plays[a.id], a.id))[:HOME_ROW_SIZE]
    rows += [{"row": "top-albums", "kind": "album", "refId": a.id, "artistId": a.artist_id, "pos": i, "score": None}
             for i, a in enumerate(top_albums)]
    videos = sorted((t for t in tracks.values() if t.is_video), key=lambda t: (-_plays(t), t.video_id))[:HOME_ROW_SIZE]
    rows += [{"row": "top-videos", "kind": "video", "refId": t.video_id, "artistId": t.artist_id, "pos": i,
              "score": None} for i, t in enumerate(videos)]
    artist_plays = {aid: 0 for aid in artist_ids}
    for t in tracks.values():
        if t.artist_id in artist_plays:
            artist_plays[t.artist_id] += _plays(t)
    top_artists = sorted(artist_ids, key=lambda aid: (-artist_plays[aid], aid))[:HOME_ROW_SIZE]
    rows += [{"row": "top-artists", "kind": "artist", "refId": aid, "artistId": None, "pos": i, "score": None}
             for i, aid in enumerate(top_artists)]
    return rows


def curated_playlists(tracks: dict[str, Track]) -> dict:
    songs = [t for t in tracks.values() if not t.is_video]
    top = sorted(songs, key=lambda t: (-_plays(t), t.video_id))[:TOP_PLAYLIST_SIZE]
    newest = sorted((t for t in songs if t.upload_date), key=lambda t: (t.upload_date, t.video_id),
                    reverse=True)[:TOP_PLAYLIST_SIZE]
    playlists, items = [], []
    for pos, (pid, title, members) in enumerate([("auto-top-50", "Top 50", top), ("new-releases", "New releases", newest)]):
        if not members:
            continue
        playlists.append({"id": pid, "title": title, "pos": pos, "year": None})
        items += [{"playlistId": pid, "kind": "track", "refId": t.video_id, "pos": i} for i, t in enumerate(members)]
    return {"playlists": playlists, "items": items}


# ---------------------------------------------------------------- the build

def build(artists: list[dict], blocked: dict, crawls: dict[str, dict], *, gate: int, version: int,
          built_at: str) -> Snapshot:
    """
    :param gate: whitelist version (Long); must only ever increase - e.g. the last commit time of the catalog.
    :param version: manifest `v` (32-bit Int); must increase on every publish - e.g. epoch seconds.
    """
    validate_catalog(artists, blocked)
    if not (0 < version < 2**31):
        raise CatalogError("manifest version must fit a 32-bit Int (use epoch seconds, not ms)")
    blocked_global = {s.strip() for s in blocked.get("global", []) if s and s.strip()}
    blocked_female = {s.strip() for s in blocked.get("female", []) if s and s.strip()}
    blocked_doc = {"global": sorted(blocked_global), "female": sorted(blocked_female - blocked_global)}

    tracks, albums = collect(artists, crawls, blocked_global)
    artist_ids = [a["id"] for a in artists]

    def thumb(a):
        return a.get("thumbnail") or (crawls.get(a["id"]) or {}).get("thumbnail")

    whitelist = []
    for a in artists:
        doc = {"id": a["id"], "name": a["name"].strip(),
               "displayName": (a.get("displayName") or a["name"]).strip(),
               "isFemale": bool(a.get("isFemale")), "isChasid": bool(a.get("isChasid")),
               "isKids": bool(a.get("isKids")), "isKidZone": bool(a.get("isKidZone"))}
        # Optional profile flags (nullable in the app: absent means unknown, so only publish real values).
        for f in ("isGenZ", "isAmerican", "isFamous", "isDJ", "isGroup"):
            if isinstance(a.get(f), bool):
                doc[f] = a[f]
        if a.get("altName"):
            doc["altName"] = a["altName"].strip()
        if thumb(a):
            doc["thumbnail"] = thumb(a)
        whitelist.append(doc)

    artist_rows = [[a["id"], (a.get("displayName") or a["name"]).strip(), thumb(a),
                    (FLAG_FEMALE if a.get("isFemale") else 0) | (FLAG_CHASID if a.get("isChasid") else 0)
                    | (FLAG_KIDZONE if a.get("isKidZone") else 0)] for a in artists]
    track_rows = [[t.video_id, t.title, t.artist_id, TRACK_FLAG_VIDEO if t.is_video else 0, t.duration_sec,
                   t.play_count, t.upload_date] for t in tracks.values()]
    album_rows = [[a.id, a.playlist_id, a.title, a.artist_id, a.type, a.year, a.thumbnail, a.upload_date]
                  for a in albums.values()]
    albumtrack_rows = [[a.id, vid, pos] for a in albums.values() for pos, vid in enumerate(a.track_ids)
                       if vid in tracks]

    shards: list[tuple[str, object]] = [("artists", artist_rows)]
    shards += partitions(track_rows, "tracks")
    shards += partitions(album_rows, "albums")
    shards += partitions(albumtrack_rows, "albumtracks")
    shards += [("homerank", home_rank(tracks, albums, artist_ids)),
               ("zemer", curated_playlists(tracks)),
               ("blocked", blocked_doc)]

    files: dict[str, bytes] = {}
    manifest_shards = []
    for name, value in shards:
        data = gzip_bytes(dumps(value))
        files[f"subset/{name}"] = data
        manifest_shards.append({"name": name, "hash": shard_hash(data), "bytes": len(data)})
    manifest = {"v": version, "builtAt": built_at, "schema": SCHEMA, "shards": manifest_shards}
    files["subset/manifest"] = dumps(manifest)

    # Content mirror. `/whitelist` and `/whitelist/version` cannot both be files on a static host, so the
    # list is published as whitelist/index.html (hosts serve it for `/whitelist/`; the app follows redirects).
    files["whitelist/index.html"] = dumps(whitelist)
    files["whitelist/version"] = dumps({"gate": gate})
    files["blockedContentIds"] = dumps(blocked_doc)
    files["israeliArtists"] = dumps([])
    files[".nojekyll"] = b""

    stats = {"artists": len(artists), "crawledArtists": sum(1 for a in artists if a["id"] in crawls),
             "tracks": len(tracks), "albums": len(albums), "videos": sum(t.is_video for t in tracks.values())}
    return Snapshot(files, manifest, stats)
