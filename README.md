# Player backend (static)

Everything the music app downloads, published as static files on Cloudflare Pages:

| Published path | What it is |
|---|---|
| `whitelist/` (+ `whitelist/version`) | Approved artists and their flags |
| `blockedContentIds` | Songs/videos hidden for everyone (`global`) or when female singers are off (`female`) |
| `israeliArtists` | Always `[]` (kept for app compatibility) |
| `subset/manifest`, `subset/<shard>` | The catalogue snapshot: artists, songs, videos, albums, home rows, curated playlists |

The app syncs these and answers search, artist, album, home and playlist screens from the snapshot on the
device. There is no live server. The exact wire format the app expects is in
[`docs/APP_CONTRACT.md`](docs/APP_CONTRACT.md).

## How it works

1. **The artist list** = Zemer's public list (pulled by `tools/upstream.py`, configured in
   `catalog/upstream.json`, used with the Zemer team's OK) **plus our own overrides** in
   `catalog/artists.json`:
   - an id that isn't upstream is **added**;
   - an id that is upstream gets the listed fields **changed** (e.g. `"isFemale": true`);
   - `"exclude": true` **removes** that artist.
   Blocked video ids = Zemer's blocked list + `catalog/blocked.json`. Set `"enabled": false` in
   `catalog/upstream.json` to run on our own list only.
2. **`tools/crawl.py`** reads each approved artist's own channel on YouTube Music (songs, videos, albums,
   singles) into `crawl/<id>.json`. Only those channels are read; nothing else enters the catalogue.
3. **`tools/build.py`** turns catalog + crawl data into `public/` (deterministic: unchanged data gives
   byte-identical shards, so devices only download what changed).
4. **GitHub Actions** (`.github/workflows/publish.yml`) runs tests, pulls the list and crawls, and commits the
   results; **Cloudflare Pages** rebuilds the site from each commit. The crawl runs
   - on every push that changes the catalog or tools,
   - nightly, to pick up new singers and approved artists' new releases,
   - manually ("Run workflow", optionally a full re-crawl).

Zemer's new singers arrive automatically with the nightly run; adding or removing one of our own = edit
`catalog/artists.json` and push. Players pick changes up on their next sync.

Safety: if the upstream list can't be fetched, comes back empty, or shrinks by more than
`maxShrinkPercent` (20%), the last good copy is kept (the app deletes artists that vanish from the list).
Large catalogs are crawled gradually: new artists first, then anything older than a week, 300 per run.

## One-time setup (Cloudflare Pages)

GitHub Actions (`.github/workflows/publish.yml`) pulls Zemer's list and crawls YouTube Music, then commits the
results (`catalog/upstream/`, `crawl/`) to `main`. Cloudflare Pages is connected to this repo and rebuilds the
site on every commit. Its build only runs `tools/build.py` (standard library, a few seconds); the site files
(`public/`) are generated, never committed.

1. GitHub: the workflow must live at `.github/workflows/publish.yml`. Actions → Crawl catalog → Run workflow
   (each run crawls up to 300 not-yet-crawled artists; run it a few times, or wait a few nights).
2. Cloudflare → Workers & Pages → Create → Pages → connect this repo:
   - Framework preset: None
   - Build command: `python3 tools/build.py --out public --previous-site https://niguno.pages.dev`
   - Build output directory: `public`
3. The site is `https://niguno.pages.dev/`; check `/whitelist/` and `/subset/manifest`.

## Local use

```
python3 -m unittest discover -s tests          # tests (no network)
pip install ytmusicapi==1.12.3
python3 tools/upstream.py                      # pull Zemer's artist list into catalog/upstream/
python3 tools/crawl.py --only UC…              # crawl one artist (or --stale-days 7 --max-artists 50)
python3 tools/build.py --out public            # build the site
```

## Rules the app imposes (don't break these)

- `whitelist` must never be empty (the app treats that as an outage).
- The whitelist version only grows, and changes only when the published list changes (`build.py` compares
  with the live site).
- Shard hashes are over the exact bytes served; shards are served without `Content-Encoding`.
- An artist removed from the whitelist is removed from players, with their songs.
