"""Build the publishable site (public/) from catalog/ and crawl/.

    python tools/build.py [--out public] [--previous-site URL] [--version <epoch-seconds>]

The whitelist version ("gate") must only grow, and should change only when the published artist list
changes (devices skip re-downloading the list while it is unchanged). With --previous-site the build compares
against what is live: same list -> same gate, changed list -> now (ms). Without it, the gate is now.
--version defaults to now (epoch seconds): a new manifest version on every publish.
"""
from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog  # noqa: E402
import snapshot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def previous_whitelist(site: str | None):
    """(whitelist bytes, gate) currently live on the site, or (None, None)."""
    if not site:
        return None, None
    site = site.rstrip("/")
    try:
        with urllib.request.urlopen(f"{site}/whitelist/", timeout=30) as r:
            body = r.read()
        with urllib.request.urlopen(f"{site}/whitelist/version", timeout=30) as r:
            gate = json.loads(r.read().decode("utf-8"))["gate"]
        return body, int(gate)
    except Exception as e:  # noqa: BLE001 - first deploy, or site down: treat as changed
        print(f"previous site not readable ({e}); using a new whitelist version", file=sys.stderr)
        return None, None


def choose_gate(new_list: bytes, previous_list: bytes | None, previous_gate: int | None, now_ms: int) -> int:
    if previous_list is not None and previous_gate and previous_list == new_list:
        return previous_gate
    return max(now_ms, (previous_gate or 0) + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "public"))
    ap.add_argument("--previous-site", default=None, help="URL of the live site, to keep the whitelist version stable")
    ap.add_argument("--version", type=int, default=None)
    args = ap.parse_args()

    artists, blocked = catalog.load(ROOT)
    crawls = {}
    for f in sorted((ROOT / "crawl").glob("UC*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        crawls[data["id"]] = data

    snap = snapshot.build(
        artists, blocked, crawls,
        gate=0,  # decided below, once the list is known
        version=args.version or int(time.time()),
        built_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    prev_list, prev_gate = previous_whitelist(args.previous_site)
    gate = choose_gate(snap.files["whitelist/index.html"], prev_list, prev_gate, int(time.time() * 1000))
    snap.files["whitelist/version"] = snapshot.dumps({"gate": gate})

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    for rel, data in snap.files.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    print(json.dumps(snap.stats), f"-> {out} ({len(snap.manifest['shards'])} shards, v={snap.manifest['v']})")
    if snap.stats["crawledArtists"] < snap.stats["artists"]:
        print("warning: some artists have no crawl data yet (run tools/crawl.py)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
