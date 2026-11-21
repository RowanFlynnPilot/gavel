"""Hidden jurisdictions: tracked, but absent from everything readers get,
and present in the editor's preview copy. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from engine import digest, ics, pages, roundup, visibility
from engine.config import InstanceConfig
from engine.errors import ConfigError
from helpers import raw_instance

SOON = (date.today() + timedelta(days=2)).isoformat()
TODAY_LONG = f"{date.today().strftime('%B')} {date.today().day}, {date.today().year}"
BASE = "https://pages.example/tracker"


def _meeting(mid, source):
    return {"id": mid, "source": source, "title": f"Meeting {mid}", "date": TODAY_LONG,
            "committee": "Board", "overview": "x", "isAgendaOnly": False}


class HiddenTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        raw = raw_instance()
        raw["jurisdictions"][3]["hidden"] = True                     # "city" is under review
        self.inst = self.root / "instance.json"
        self.inst.write_text(json.dumps(raw), encoding="utf-8")
        self.cfg = InstanceConfig(raw, self.inst)
        self.meetings = self.root / "meetings.json"
        self.upcoming = self.root / "upcoming.json"
        self.meetings.write_text(json.dumps([_meeting("a1", "county"), _meeting("cc_9", "city")]),
                                 encoding="utf-8")
        self.upcoming.write_text(json.dumps({
            "county": [{"date": SOON, "time": "7:00 PM", "name": "County Board", "url": "u", "source": "county"}],
            "city": [{"date": SOON, "time": "6:00 PM", "name": "Common Council", "url": "u", "source": "city"}],
        }), encoding="utf-8")

    def patch_paths(self, module):
        for name, value in (("MEETINGS_JSON", self.meetings), ("UPCOMING_JSON", self.upcoming)):
            if hasattr(module, name):
                p = mock.patch.object(module, name, value)
                p.start()
                self.addCleanup(p.stop)


class ViewsTest(HiddenTestCase):
    def test_public_copies_leave_the_hidden_board_out_and_the_preview_keeps_it(self):
        self.patch_paths(visibility)
        out, preview = self.root / "public", self.root / "public" / "preview"
        stats = visibility.write_views(self.cfg, out, preview)
        self.assertEqual(stats["hidden"], ["city"])

        def load(p):
            return json.loads(p.read_text(encoding="utf-8"))
        self.assertNotIn("city", [j["key"] for j in load(out / "instance.json")["jurisdictions"]])
        self.assertEqual([m["id"] for m in load(out / "data" / "meetings.json")], ["a1"])
        self.assertEqual(list(load(out / "data" / "upcoming.json")), ["county"])
        self.assertIn("city", [j["key"] for j in load(preview / "instance.json")["jurisdictions"]])
        self.assertTrue(load(preview / "instance.json")["preview"])
        self.assertEqual(len(load(preview / "data" / "meetings.json")), 2)
        self.assertIn("city", load(preview / "data" / "upcoming.json"))

    def test_nothing_hidden_means_no_preview_and_a_stale_one_is_removed(self):
        self.patch_paths(visibility)
        raw = raw_instance()
        self.inst.write_text(json.dumps(raw), encoding="utf-8")
        cfg = InstanceConfig(raw, self.inst)
        out, preview = self.root / "public", self.root / "public" / "preview"
        (preview / "data").mkdir(parents=True)
        (preview / "instance.json").write_text("{}", encoding="utf-8")
        stats = visibility.write_views(cfg, out, preview)
        self.assertFalse(stats["preview"])
        self.assertFalse(preview.exists())
        self.assertEqual(len(json.loads((out / "data" / "meetings.json").read_text(encoding="utf-8"))), 2)

    def test_preview_page_is_the_built_app_marked_noindex(self):
        dist = self.root / "dist"
        (dist / "preview").mkdir(parents=True)
        (dist / "index.html").write_text("<html><head><title>T</title></head><body></body></html>",
                                         encoding="utf-8")
        self.assertTrue(visibility.write_preview_index(dist))
        page = (dist / "preview" / "index.html").read_text(encoding="utf-8")
        self.assertIn('name="robots" content="noindex, nofollow"', page)
        self.assertIn("<title>T</title>", page)
        self.assertNotIn("noindex", (dist / "index.html").read_text(encoding="utf-8"))
        # Nothing hidden: no preview folder, nothing written.
        other = self.root / "dist2"
        other.mkdir()
        (other / "index.html").write_text("<html><head></head></html>", encoding="utf-8")
        self.assertFalse(visibility.write_preview_index(other))

    def test_hidden_must_be_a_boolean(self):
        raw = raw_instance()
        raw["jurisdictions"][0]["hidden"] = "yes"
        with self.assertRaises(ConfigError):
            InstanceConfig(raw, Path("instance.json"))


class PublicOutputsTest(HiddenTestCase):
    def test_calendar_feed_and_roundup_leave_the_hidden_board_out(self):
        self.patch_paths(ics)
        self.patch_paths(roundup)
        events = json.dumps(ics.build_events(self.cfg, 30), default=str)
        self.assertIn("County Board", events)
        self.assertNotIn("Common Council", events)
        recent, week_ahead = roundup.collect(self.cfg, 7)
        self.assertEqual([m["id"] for _, m in recent], ["a1"])
        self.assertEqual([src for _, src, _ in week_ahead], ["county"])

    def test_static_pages_are_not_written_for_a_hidden_board_and_old_ones_are_removed(self):
        self.patch_paths(pages)
        cfg = self.cfg
        cfg.seo = {"pages_base_url": BASE}
        out = self.root / "site"
        old = out / "meetings" / "cc_9"
        old.mkdir(parents=True)
        (old / "index.html").write_text("<p>published before the board was hidden</p>", encoding="utf-8")
        state = {"processed": {"cc_9": {"source": "city"}, "a1": {"source": "county"}}}
        with mock.patch.object(pages, "load_instance", return_value=cfg), \
             mock.patch.object(pages, "load_state", return_value=state), \
             mock.patch("sys.argv", ["engine.pages", "--out-dir", str(out)]):
            pages.main()
        self.assertTrue((out / "meetings" / "a1" / "index.html").exists())
        self.assertFalse(old.exists())
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("/meetings/a1/", sitemap)
        self.assertNotIn("cc_9", sitemap)

    def test_digest_has_no_entry_for_a_hidden_board(self):
        repo = Path(__file__).resolve().parent.parent
        raw = raw_instance()
        raw["theme"] = json.loads((repo / "instance.json").read_text(encoding="utf-8"))["theme"]
        raw["jurisdictions"][3]["hidden"] = True
        theme = digest.Theme(InstanceConfig(raw, repo / "instance.json"))
        self.assertIn("county", theme.sources)
        self.assertNotIn("city", theme.sources)


if __name__ == "__main__":
    unittest.main()
