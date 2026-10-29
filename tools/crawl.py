"""Crawl each catalog artist's releases from YouTube Music into crawl/<channelId>.json.

    python tools/crawl.py [--only UC...] [--stale-days 7] [--max-artists 300]

Output per artist (consumed by tools/build.py):
    {"id", "name", "thumbnail", "crawledAt",
     "tracks": [{"videoId","title","isVideo","durationSec","playCount","uploadDate"}],
     "albums": [{"id","playlistId","title","type","year","thumbnail","tracks":[track...]}]}

Only the artist's own channel is read (their songs, videos, albums and singles); nothing is searched.
A failing artist keeps its previous crawl file, so one bad channel never empties the catalogue.
Artists never crawled go first, then the stalest; --max-artists caps one run so a large catalog is refreshed
gradually (e.g. nightly) instead of hammering YouTube Music in one go.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRAWL_DIR = ROOT / "crawl"

ATV = "MUSIC_VIDEO_TYPE_ATV"   # audio-only "art track"; everything else (OMV, UGC) counts as a video
SLEEP_S = 1.0                  # be polite between artists


# ---------------------------------------------------------------- pure parsing (unit-tested)

_COUNT = re.compile(r"^\s*([\d.,]+)\s*([KMB])?", re.IGNORECASE)


def parse_count(text) -> int | None:
    """'1.2M plays' -> 1200000, '3,693 views' -> 3693, '19K' -> 19000, None/garbage -> None."""
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = _COUNT.match(str(text))
    if not m:
        return None
    number, suffix = m.group(1), (m.group(2) or "").upper()
    try:
        if suffix:
            value = float(number.replace(",", "")) * {"K": 1e3, "M": 1e6, "B": 1e9}[suffix]
        else:
            value = float(number.replace(",", ""))
    except ValueError:
        return None
    return int(round(value))


def best_thumbnail(thumbnails) -> str | None:
    if not thumbnails:
        return None
    return max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")


def track_from_item(item: dict, upload_date: str | None = None) -> dict | None:
    vid = item.get("videoId")
    if not vid or not item.get("title") or item.get("isAvailable") is False:
        return None
    return {
        "videoId": vid,
        "title": item["title"],
        "isVideo": (item.get("videoType") or ATV) != ATV,
        "durationSec": item.get("duration_seconds"),
        "playCount": parse_count(item.get("views")),
        "uploadDate": upload_date,
    }


def album_from_response(browse_id: str, album: dict) -> dict:
    year = album.get("year")
    year = int(year) if isinstance(year, str) and year.isdigit() else (year if isinstance(year, int) else None)
    upload = f"{year}-01-01T00:00:00Z" if year else None
    tracks = [t for t in (track_from_item(i, upload) for i in album.get("tracks") or []) if t]
    return {
        "id": browse_id,
        "playlistId": album.get("audioPlaylistId"),
        "title": album.get("title"),
        "type": (album.get("type") or "album").lower(),
        "year": year,
        "thumbnail": best_thumbnail(album.get("thumbnails")),
        "tracks": tracks,
    }


# ---------------------------------------------------------------- network (ytmusicapi)

def crawl_artist(yt, channel_id: str) -> dict:
    artist = yt.get_artist(channel_id)
    tracks: dict[str, dict] = {}

    for key in ("songs", "videos"):
        section = artist.get(key) or {}
        items = section.get("results") or []
        if section.get("browseId"):
            try:
                items = yt.get_playlist(section["browseId"], limit=None).get("tracks") or items
            except Exception as e:  # noqa: BLE001 - keep the top results if the full list fails
                print(f"  {key}: full list failed ({e}); using top results", file=sys.stderr)
        for item in items:
            t = track_from_item(item)
            if t and t["videoId"] not in tracks:
                tracks[t["videoId"]] = t

    albums = []
    for key in ("albums", "singles"):
        section = artist.get(key) or {}
        refs = section.get("results") or []
        if section.get("browseId") and section.get("params"):
            try:
                refs = yt.get_artist_albums(section["browseId"], section["params"], limit=None) or refs
            except Exception as e:  # noqa: BLE001
                print(f"  {key}: full list failed ({e}); using top results", file=sys.stderr)
        for ref in refs:
            bid = ref.get("browseId")
            if not bid:
                continue
            try:
                albums.append(album_from_response(bid, yt.get_album(bid)))
            except Exception as e:  # noqa: BLE001
                print(f"  album {bid} failed: {e}", file=sys.stderr)

    return {
        "id": channel_id,
        "name": artist.get("name"),
        "thumbnail": best_thumbnail(artist.get("thumbnails")),
        "crawledAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tracks": list(tracks.values()),
        "albums": albums,
    }


def crawled_time(path: Path) -> float:
    """When an artist was last crawled: the file's own crawledAt (survives CI cache restores), else mtime."""
    try:
        stamp = json.loads(path.read_text(encoding="utf-8")).get("crawledAt")
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError, OSError, AttributeError):
        return path.stat().st_mtime


def crawl_order(artist_ids: list[str], crawled_at: dict[str, float], now: float, stale_days: float,
                max_artists: int | None) -> list[str]:
    """Never-crawled first (catalog order), then stalest first; skip fresh ones; cap the run."""
    todo = [cid for cid in artist_ids if now - crawled_at.get(cid, 0) >= stale_days * 86400]
    todo.sort(key=lambda cid: (cid in crawled_at, crawled_at.get(cid, 0)))
    return todo[:max_artists] if max_artists else todo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", help="crawl just these channel ids")
    ap.add_argument("--stale-days", type=float, default=0, help="skip artists crawled more recently than this")
    ap.add_argument("--max-artists", type=int, default=None, help="crawl at most this many artists in one run")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import catalog  # noqa: E402
    from ytmusicapi import YTMusic  # imported here so the pure helpers test without it

    yt = YTMusic()
    CRAWL_DIR.mkdir(exist_ok=True)
    artists, _ = catalog.load()
    names = {a["id"]: a.get("name") for a in artists}
    crawled_at = {f.stem: crawled_time(f) for f in CRAWL_DIR.glob("UC*.json")}
    ids = args.only or crawl_order(list(names), crawled_at, time.time(), args.stale_days, args.max_artists)
    print(f"{len(artists)} artists in catalog, crawling {len(ids)}")
    failures = 0
    for cid in ids:
        print(f"crawling {names.get(cid)} ({cid})")
        try:
            data = crawl_artist(yt, cid)
            (CRAWL_DIR / f"{cid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {len(data['tracks'])} tracks, {len(data['albums'])} albums")
        except Exception as e:  # noqa: BLE001 - keep the previous file for this artist
            failures += 1
            print(f"  FAILED: {e}", file=sys.stderr)
        time.sleep(SLEEP_S)
    print(f"done, {failures} failures")
    return 1 if ids and failures == len(ids) else 0


if __name__ == "__main__":
    sys.exit(main())
