import gzip
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import crawl  # noqa: E402
import snapshot  # noqa: E402

A = "UCexampleArtistAAAAAAAAA"
B = "UCexampleArtistBBBBBBBBB"


def decode(data: bytes):
    return json.loads(gzip.decompress(data).decode("utf-8"))


class EncodingMatchesTheAppContract(unittest.TestCase):
    """Byte-exact vectors from docs/APP_CONTRACT.md §2.8 and the app's SubsetSyncTest."""

    def test_hash_vectors_from_the_app_tests(self):
        self.assertEqual("e3b0c44298fc1c14", snapshot.shard_hash(b""))
        self.assertEqual("ba7816bf8f01cfea", snapshot.shard_hash(b"abc"))

    def test_blocked_shard_bytes_match_the_contract_example(self):
        data = snapshot.gzip_bytes(snapshot.dumps({"global": [], "female": []}))
        expected = bytes.fromhex(
            "1f8b08000000000002ffab564acfc94f4acc51b28a8ed5514a4bcd4dcc4905b16b01e7dd8b7219000000")
        self.assertEqual(expected, data)
        self.assertEqual("706f5944b1bd0346", snapshot.shard_hash(data))

    def test_example_shard_hashes_match_the_contract(self):
        shards = {
            "artists": [[A, "Avraham Example", "https://yt3.ggpht.com/exampleA=s800", 0],
                        [B, "Example Choir", "https://yt3.ggpht.com/exampleB=s800", 2]],
            "albumtracks-0": [["MPREb_example01", "vid00000001", 0], ["MPREb_example01", "vid00000002", 1]],
        }
        self.assertEqual("393cd2792e9d7017", snapshot.shard_hash(snapshot.gzip_bytes(snapshot.dumps(shards["artists"]))))
        self.assertEqual("dd91ba9ed24b81c0",
                         snapshot.shard_hash(snapshot.gzip_bytes(snapshot.dumps(shards["albumtracks-0"]))))


def fixture():
    artists = [
        {"id": A, "name": "Avraham Example", "altName": "אברהם", "isChasid": True},
        {"id": B, "name": "Example Choir", "displayName": "The Example Choir", "isFemale": True, "isKidZone": True},
    ]
    crawls = {
        A: {"id": A, "thumbnail": "https://yt3/a", "tracks": [
            {"videoId": "v1", "title": "Song One", "isVideo": False, "durationSec": 215, "playCount": 120000},
            {"videoId": "v3", "title": "Live Video", "isVideo": True, "durationSec": 300, "playCount": 900},
            {"videoId": "bad", "title": "Blocked", "isVideo": False, "durationSec": 1, "playCount": 1},
        ], "albums": [{"id": "MPREb_1", "playlistId": "OLAK_1", "title": "First Album", "type": "Album",
                       "year": 2024, "thumbnail": "https://lh3/al", "tracks": [
                           {"videoId": "v1", "title": "Song One", "isVideo": False},
                           {"videoId": "v2", "title": "Song Two", "isVideo": False, "durationSec": 190,
                            "playCount": 80000}]}]},
        B: {"id": B, "thumbnail": None, "tracks": [
            {"videoId": "v1", "title": "Duplicate of A's song", "isVideo": False},
            {"videoId": "v4", "title": "Choir Song", "isVideo": False, "playCount": None}], "albums": []},
    }
    return artists, {"global": ["bad"], "female": ["v4", "bad"]}, crawls


class BuildTest(unittest.TestCase):
    def setUp(self):
        artists, blocked, crawls = fixture()
        self.snap = snapshot.build(artists, blocked, crawls, gate=1761696000000, version=1761696000,
                                   built_at="2026-10-29T00:00:00Z")
        self.files = self.snap.files

    def shard(self, name):
        return decode(self.files[f"subset/{name}"])

    def test_manifest_lists_every_shard_with_its_hash(self):
        m = json.loads(self.files["subset/manifest"])
        self.assertEqual(1, m["schema"])
        self.assertEqual(1761696000, m["v"])
        for s in m["shards"]:
            data = self.files[f"subset/{s['name']}"]
            self.assertEqual(snapshot.shard_hash(data), s["hash"])
            self.assertEqual(len(data), s["bytes"])
            self.assertRegex(s["name"], r"^[a-z0-9-]+$")
        names = [s["name"] for s in m["shards"]]
        self.assertIn("tracks-0", names)
        self.assertNotIn("tracks", names)

    def test_row_shapes_match_the_decoder(self):
        self.assertTrue(all(len(r) == 4 for r in self.shard("artists")))
        self.assertTrue(all(len(r) == 7 for r in self.shard("tracks-0")))
        self.assertTrue(all(len(r) == 8 for r in self.shard("albums-0")))
        self.assertTrue(all(len(r) == 3 for r in self.shard("albumtracks-0")))

    def test_artist_flags_and_display_name(self):
        rows = {r[0]: r for r in self.shard("artists")}
        self.assertEqual(snapshot.FLAG_CHASID, rows[A][3])
        self.assertEqual(snapshot.FLAG_FEMALE | snapshot.FLAG_KIDZONE, rows[B][3])
        self.assertEqual("The Example Choir", rows[B][1])
        self.assertEqual("https://yt3/a", rows[A][2])

    def test_tracks_are_unique_blocked_ones_removed_and_album_tracks_added(self):
        tracks = {r[0]: r for r in self.shard("tracks-0")}
        self.assertEqual({"v1", "v2", "v3", "v4"}, set(tracks))
        self.assertEqual(A, tracks["v1"][2])            # first artist in catalog order owns a shared videoId
        self.assertEqual(1, tracks["v3"][3])            # video flag
        self.assertEqual("2024-01-01T00:00:00Z", tracks["v2"][6])  # album year becomes the release date
        self.assertEqual([["MPREb_1", "v1", 0], ["MPREb_1", "v2", 1]], self.shard("albumtracks-0"))
        self.assertEqual("album", self.shard("albums-0")[0][4])

    def test_blocked_lists_published_in_both_places(self):
        self.assertEqual({"global": ["bad"], "female": ["v4"]}, self.shard("blocked"))
        self.assertEqual({"global": ["bad"], "female": ["v4"]}, json.loads(self.files["blockedContentIds"]))

    def test_home_rows_and_curated_playlists(self):
        rank = self.shard("homerank")
        self.assertEqual("MPREb_1", [r for r in rank if r["row"] == "top-albums"][0]["refId"])
        self.assertEqual(["v3"], [r["refId"] for r in rank if r["row"] == "top-videos"])
        self.assertEqual([A, B], [r["refId"] for r in rank if r["row"] == "top-artists"])
        z = self.shard("zemer")
        self.assertEqual("auto-top-50", z["playlists"][0]["id"])
        top = [i["refId"] for i in z["items"] if i["playlistId"] == "auto-top-50"]
        self.assertEqual(["v1", "v2", "v4"], top)        # songs only, by plays; no videos

    def test_content_mirror_files(self):
        wl = json.loads(self.files["whitelist/index.html"])
        self.assertEqual([A, B], [d["id"] for d in wl])
        self.assertTrue(all(d["displayName"] for d in wl))  # needed for the app's sync fast path
        self.assertEqual("אברהם", wl[0]["altName"])
        self.assertEqual({"gate": 1761696000000}, json.loads(self.files["whitelist/version"]))
        self.assertEqual([], json.loads(self.files["israeliArtists"]))

    def test_rebuild_is_byte_identical(self):
        artists, blocked, crawls = fixture()
        again = snapshot.build(artists, blocked, crawls, gate=1761696000000, version=1761696000,
                               built_at="2026-10-29T00:00:00Z")
        self.assertEqual(self.files, again.files)

    def test_bad_catalogs_are_rejected(self):
        with self.assertRaises(snapshot.CatalogError):
            snapshot.build([], {}, {}, gate=1, version=1, built_at="x")
        with self.assertRaises(snapshot.CatalogError):
            snapshot.build([{"id": "nope", "name": "x"}], {}, {}, gate=1, version=1, built_at="x")
        with self.assertRaises(snapshot.CatalogError):
            snapshot.build([{"id": A, "name": "x"}, {"id": A, "name": "y"}], {}, {}, gate=1, version=1, built_at="x")
        with self.assertRaises(snapshot.CatalogError):
            snapshot.build([{"id": A, "name": "x"}], {}, {}, gate=1, version=1761696000000, built_at="x")

    def test_large_catalogs_are_partitioned(self):
        many = {A: {"id": A, "tracks": [{"videoId": f"v{i}", "title": "t"} for i in range(snapshot.PARTITION_ROWS + 1)],
                    "albums": []}}
        snap = snapshot.build([{"id": A, "name": "x"}], {}, many, gate=1, version=1, built_at="x")
        self.assertIn("subset/tracks-1", snap.files)


class CrawlParsingTest(unittest.TestCase):
    def test_parse_count(self):
        self.assertEqual(1200000, crawl.parse_count("1.2M plays"))
        self.assertEqual(3693, crawl.parse_count("3,693 views"))
        self.assertEqual(19000, crawl.parse_count("19K"))
        self.assertIsNone(crawl.parse_count(None))
        self.assertIsNone(crawl.parse_count("no plays"))

    def test_track_kinds(self):
        song = crawl.track_from_item({"videoId": "a", "title": "x", "videoType": "MUSIC_VIDEO_TYPE_ATV",
                                      "duration_seconds": 10, "views": "5K"})
        self.assertEqual({"videoId": "a", "title": "x", "isVideo": False, "durationSec": 10, "playCount": 5000,
                          "uploadDate": None}, song)
        self.assertTrue(crawl.track_from_item({"videoId": "b", "title": "y", "videoType": "MUSIC_VIDEO_TYPE_OMV"})["isVideo"])
        self.assertIsNone(crawl.track_from_item({"videoId": "c", "title": "z", "isAvailable": False}))

    def test_album_response(self):
        al = crawl.album_from_response("MPREb_x", {"title": "T", "type": "Single", "year": "2023",
                                                   "audioPlaylistId": "OLAK", "thumbnails": [
                                                       {"url": "s", "width": 60, "height": 60},
                                                       {"url": "big", "width": 544, "height": 544}],
                                                   "tracks": [{"videoId": "v", "title": "t"}]})
        self.assertEqual(("single", 2023, "big", "OLAK"), (al["type"], al["year"], al["thumbnail"], al["playlistId"]))
        self.assertEqual("2023-01-01T00:00:00Z", al["tracks"][0]["uploadDate"])


if __name__ == "__main__":
    unittest.main()
