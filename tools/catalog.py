"""The effective artist catalog = upstream list (optional) + our own overrides.

catalog/upstream.json        {"enabled": true, "baseUrl": "https://content.zemer.io", "maxShrinkPercent": 20}
catalog/upstream/            last good copy of the upstream list (written by tools/upstream.py)
catalog/artists.json         our additions and changes, applied on top. Per entry:
                               - an id not upstream        -> added
                               - an id that is upstream    -> listed fields replace the upstream values
                               - "exclude": true           -> that artist is removed
catalog/blocked.json         our blocked ids, merged with the upstream blocked ids
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog"

# Fields carried from a whitelist doc into the published one (the app's ContentWhitelistDoc).
TEXT_FIELDS = ("name", "displayName", "altName", "thumbnail")
FLAG_FIELDS = ("isFemale", "isChasid", "isKids", "isKidZone", "isGenZ", "isAmerican", "isFamous", "isDJ", "isGroup")
OVERRIDE_ONLY = ("exclude", "note")


def valid_channel_id(cid) -> bool:
    return isinstance(cid, str) and cid.startswith("UC") and len(cid) == 24


def normalize_upstream(docs: list) -> tuple[list[dict], int]:
    """Upstream /whitelist docs -> our artist dicts. Returns (artists, skipped_count)."""
    out, seen, skipped = [], set(), 0
    for d in docs or []:
        if not isinstance(d, dict):
            skipped += 1
            continue
        cid = (d.get("id") or d.get("artistId") or "").strip()
        name = (d.get("name") or d.get("artistName") or "").strip()
        if not valid_channel_id(cid) or not name or cid in seen:
            skipped += 1
            continue
        seen.add(cid)
        a = {"id": cid, "name": name}
        for f in TEXT_FIELDS[1:]:
            if isinstance(d.get(f), str) and d[f].strip():
                a[f] = d[f].strip()
        for f in FLAG_FIELDS:
            if isinstance(d.get(f), bool):
                a[f] = d[f]
        out.append(a)
    return out, skipped


def merge(upstream: list[dict], overrides: list[dict]) -> list[dict]:
    """Apply our overrides on top of the (normalized) upstream artists. Order: upstream first, then additions."""
    by_id = {a["id"]: dict(a) for a in upstream}
    order = [a["id"] for a in upstream]
    excluded = set()
    for o in overrides:
        cid = o.get("id")
        if o.get("exclude"):
            excluded.add(cid)
            continue
        if cid in by_id:
            by_id[cid].update({k: v for k, v in o.items() if k not in OVERRIDE_ONLY})
        else:
            by_id[cid] = {k: v for k, v in o.items() if k not in OVERRIDE_ONLY}
            order.append(cid)
    return [by_id[cid] for cid in order if cid not in excluded]


def merge_blocked(upstream: dict, ours: dict) -> dict:
    return {key: sorted({*(upstream or {}).get(key, []), *(ours or {}).get(key, [])}) for key in ("global", "female")}


def shrink_too_large(previous: list, current: list, max_shrink_percent: float) -> bool:
    """Guard: the app deletes artists that vanish from the list, so never publish a suddenly-tiny upstream."""
    if not previous:
        return False
    return len(current) < len(previous) * (1 - max_shrink_percent / 100.0)


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load(root: Path = ROOT) -> tuple[list[dict], dict]:
    """The effective (artists, blocked) used by crawl.py and build.py."""
    cat = root / "catalog"
    config = _read(cat / "upstream.json", {"enabled": False})
    overrides = _read(cat / "artists.json", [])
    ours_blocked = _read(cat / "blocked.json", {})
    if not config.get("enabled"):
        return [o for o in overrides if not o.get("exclude")], merge_blocked({}, ours_blocked)
    upstream, _ = normalize_upstream(_read(cat / "upstream" / "whitelist.json", []))
    upstream_blocked = _read(cat / "upstream" / "blocked.json", {})
    return merge(upstream, overrides), merge_blocked(upstream_blocked, ours_blocked)
