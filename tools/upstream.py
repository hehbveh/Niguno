"""Pull the upstream artist list and blocked ids into catalog/upstream/ (the last good copy).

    python tools/upstream.py

A failed or suspicious pull (unreachable, empty, or shrunk by more than maxShrinkPercent) keeps the previous
copy and exits 0, so the build carries on with known-good data.
"""
from __future__ import annotations

import gzip
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog  # noqa: E402

UPSTREAM_DIR = catalog.CATALOG / "upstream"
TIMEOUT_S = 30


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "player-backend/1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def main() -> int:
    config = json.loads((catalog.CATALOG / "upstream.json").read_text(encoding="utf-8"))
    if not config.get("enabled"):
        print("upstream disabled")
        return 0
    base = config["baseUrl"].rstrip("/")
    previous = json.loads((UPSTREAM_DIR / "whitelist.json").read_text(encoding="utf-8")) \
        if (UPSTREAM_DIR / "whitelist.json").exists() else []

    try:
        docs = fetch_json(f"{base}/whitelist")
        blocked = fetch_json(f"{base}/blockedContentIds")
    except Exception as e:  # noqa: BLE001
        print(f"upstream unreachable ({e}); keeping previous copy ({len(previous)} artists)", file=sys.stderr)
        return 0

    artists, skipped = catalog.normalize_upstream(docs if isinstance(docs, list) else [])
    if not artists:
        print("upstream returned no usable artists; keeping previous copy", file=sys.stderr)
        return 0
    prev_artists, _ = catalog.normalize_upstream(previous)
    if catalog.shrink_too_large(prev_artists, artists, config.get("maxShrinkPercent", 20)):
        print(f"upstream shrank from {len(prev_artists)} to {len(artists)} artists; keeping previous copy "
              "(raise maxShrinkPercent in catalog/upstream.json if this is intended)", file=sys.stderr)
        return 0

    UPSTREAM_DIR.mkdir(exist_ok=True)
    (UPSTREAM_DIR / "whitelist.json").write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
    clean_blocked = {k: [s for s in (blocked or {}).get(k, []) if isinstance(s, str)] for k in ("global", "female")}
    (UPSTREAM_DIR / "blocked.json").write_text(json.dumps(clean_blocked, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"upstream: {len(artists)} artists ({skipped} skipped), "
          f"{len(clean_blocked['global'])} global + {len(clean_blocked['female'])} female blocked ids")
    return 0


if __name__ == "__main__":
    sys.exit(main())
