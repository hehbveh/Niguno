import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build  # noqa: E402
import catalog  # noqa: E402
import crawl  # noqa: E402

A, B, C = "UC" + "a" * 22, "UC" + "b" * 22, "UC" + "c" * 22


class UpstreamMergeTest(unittest.TestCase):
    def test_normalize_accepts_both_id_and_name_spellings_and_skips_junk(self):
        docs = [{"id": A, "name": "Alpha", "displayName": "Alpha", "isFemale": False, "isAmerican": None},
                {"artistId": B, "artistName": "Beta - בטא", "altName": "בטא", "isKidZone": True, "thumbnail": "t"},
                {"id": "not-a-channel", "name": "x"}, {"id": C}, {"id": A, "name": "dup"}, "garbage"]
        artists, skipped = catalog.normalize_upstream(docs)
        self.assertEqual(4, skipped)
        self.assertEqual({"id": A, "name": "Alpha", "displayName": "Alpha", "isFemale": False}, artists[0])
        self.assertEqual({"id": B, "name": "Beta - בטא", "altName": "בטא", "thumbnail": "t", "isKidZone": True},
                         artists[1])

    def test_overrides_change_add_and_exclude(self):
        upstream = [{"id": A, "name": "Alpha", "isFemale": False}, {"id": B, "name": "Beta"}]
        overrides = [{"id": A, "isFemale": True, "note": "fix"}, {"id": B, "exclude": True},
                     {"id": C, "name": "Gamma", "note": "ours"}]
        merged = catalog.merge(upstream, overrides)
        self.assertEqual([{"id": A, "name": "Alpha", "isFemale": True}, {"id": C, "name": "Gamma"}], merged)

    def test_blocked_lists_are_unioned(self):
        self.assertEqual({"global": ["a", "b"], "female": ["c"]},
                         catalog.merge_blocked({"global": ["b", "a"], "female": ["c"]}, {"global": ["a"]}))

    def test_shrink_guard(self):
        prev = [{}] * 100
        self.assertFalse(catalog.shrink_too_large(prev, [{}] * 85, 20))
        self.assertTrue(catalog.shrink_too_large(prev, [{}] * 70, 20))
        self.assertFalse(catalog.shrink_too_large([], [{}], 20))

    def test_load_uses_upstream_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "catalog" / "upstream").mkdir(parents=True)
            (root / "catalog" / "artists.json").write_text(json.dumps([{"id": C, "name": "Gamma"}]))
            (root / "catalog" / "upstream" / "whitelist.json").write_text(json.dumps([{"id": A, "name": "Alpha"}]))
            (root / "catalog" / "upstream" / "blocked.json").write_text(json.dumps({"global": ["x"]}))
            (root / "catalog" / "upstream.json").write_text(json.dumps({"enabled": True}))
            artists, blocked = catalog.load(root)
            self.assertEqual([A, C], [a["id"] for a in artists])
            self.assertEqual(["x"], blocked["global"])
            (root / "catalog" / "upstream.json").write_text(json.dumps({"enabled": False}))
            artists, blocked = catalog.load(root)
            self.assertEqual([C], [a["id"] for a in artists])
            self.assertEqual([], blocked["global"])


class GateTest(unittest.TestCase):
    def test_unchanged_list_keeps_the_live_gate(self):
        self.assertEqual(500, build.choose_gate(b"[1]", b"[1]", 500, now_ms=10_000))

    def test_changed_or_unknown_list_gets_a_newer_gate(self):
        self.assertEqual(10_000, build.choose_gate(b"[2]", b"[1]", 500, now_ms=10_000))
        self.assertEqual(10_000, build.choose_gate(b"[2]", None, None, now_ms=10_000))
        # clock behind the live gate: still strictly newer
        self.assertEqual(20_001, build.choose_gate(b"[2]", b"[1]", 20_000, now_ms=10_000))


class CrawlOrderTest(unittest.TestCase):
    def test_new_first_then_stalest_skipping_fresh_and_capped(self):
        day = 86400
        crawled = {A: 100 * day, B: 50 * day}          # B is staler than A
        order = crawl.crawl_order([A, B, C], crawled, now=101 * day, stale_days=7, max_artists=None)
        self.assertEqual([C, B], order)                # A crawled 1 day ago: fresh, skipped
        self.assertEqual([C], crawl.crawl_order([A, B, C], crawled, now=101 * day, stale_days=7, max_artists=1))
        self.assertEqual([C, B, A], crawl.crawl_order([A, B, C], crawled, now=101 * day, stale_days=0, max_artists=None))


if __name__ == "__main__":
    unittest.main()
