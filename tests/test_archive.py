"""The archive index: one short row per summarized meeting, a page for each,
and hidden boards left out of the copy readers get. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import archive, pages, visibility
from engine.config import InstanceConfig
from helpers import raw_instance

BASE = "https://pages.example/tracker"


def el(n: int) -> str:
    return f"el_{n:032x}"


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        raw = raw_instance()
        raw["jurisdictions"][4]["hidden"] = True                    # "town" is under review
        raw["seo"] = {"pages_base_url": BASE}
        self.cfg = InstanceConfig(raw, Path("instance.json"))
        self.state = {"processed": {}}

    def meeting(self, mid, source, day, title, **summary):
        summary = {"overview": "The board set the levy. " * 30, "_source": "minutes", "agenda": [],
                   "topics": ["Budget", "Elm Street Bridge"], "issues": ["budget_taxes"],
                   "discussions": [{"item": "Elm Street bridge repair", "body": "Approved."},
                                   {"item": "2027 levy", "body": "Set."}],
                   "votes": [{"item": "2027 levy", "outcome": "Approved", "tally": "5-2"}], **summary}
        (self.dir / f"{mid}_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        self.state["processed"][mid] = {"title": title, "source": source, "meeting_date": day,
                                        "doc_url": "https://elm.example/m", "processed_at": "2026-10-01T00:00:00+00:00",
                                        "summary_file": str(self.dir / f"{mid}.md")}


class IndexTest(ArchiveTestCase):
    def test_one_short_row_per_summarized_meeting_newest_first(self):
        self.meeting(el(1), "village", "20260908", "Village Board Meeting - 2026-09-08")
        self.meeting(el(2), "village", "20261006", "Plan Commission Meeting - 2026-10-06", _source="agenda")
        self.state["processed"][el(3)] = {"title": "No summary on file", "source": "village",
                                          "meeting_date": "20261007", "summary_file": str(self.dir / "gone.md")}
        self.state["processed"]["x_1"] = {"title": "A board that was removed", "source": "nowhere",
                                          "meeting_date": "20261007", "summary_file": str(self.dir / "x.md")}
        (self.dir / "pages" / el(1)).mkdir(parents=True)
        (self.dir / "pages" / el(1) / "index.html").write_text("<p>page</p>", encoding="utf-8")
        rows = archive.build_index(self.cfg, self.state, self.dir / "pages")
        self.assertEqual([r["id"] for r in rows], [el(2), el(1)])
        old = rows[1]
        self.assertEqual((old["source"], old["date"], old["title"]), ("village", "2026-09-08", "Village Board Meeting"))
        self.assertEqual((old["agendaOnly"], old["page"]), (False, True))
        self.assertEqual(old["issues"], ["budget_taxes"])
        self.assertEqual(old["text"], "Elm Street bridge repair · 2027 levy", "item titles once each, for search")
        self.assertLessEqual(len(old["overview"]), archive.OVERVIEW_CHARS)
        self.assertTrue(old["overview"].endswith("."), "cut at a sentence")
        self.assertEqual((rows[0]["agendaOnly"], rows[0]["page"]), (True, False))
        self.assertNotIn("discussions", old)

    def test_command_writes_the_index_for_every_board(self):
        self.meeting(el(1), "village", "20260908", "Village Board Meeting - 2026-09-08")
        self.meeting("pg_7", "town", "20260909", "Town Board - 2026-09-09")
        out = self.dir / "data" / "archive.json"
        with mock.patch.object(archive, "load_instance", return_value=self.cfg), \
             mock.patch.object(archive, "load_state", return_value=self.state), \
             mock.patch("sys.argv", ["engine.archive", "--out", str(out), "--pages-dir", str(self.dir / "pages")]):
            archive.main()
        self.assertEqual({r["source"] for r in json.loads(out.read_text(encoding="utf-8"))}, {"village", "town"})


class PagesAndViewsTest(ArchiveTestCase):
    def test_an_older_meeting_with_no_page_gets_one_and_a_hidden_board_does_not(self):
        self.meeting(el(1), "village", "20260908", "Village Board Meeting - 2026-09-08")
        self.meeting("pg_7", "town", "20260909", "Town Board - 2026-09-09")
        out = self.dir / "site"
        displayed = self.dir / "meetings.json"
        displayed.write_text("[]", encoding="utf-8")                 # both have left the list
        with mock.patch.object(pages, "load_instance", return_value=self.cfg), \
             mock.patch.object(pages, "load_state", return_value=self.state), \
             mock.patch.object(archive, "load_state", return_value=self.state), \
             mock.patch.object(pages, "MEETINGS_JSON", displayed), \
             mock.patch("sys.argv", ["engine.pages", "--out-dir", str(out)]):
            pages.main()
            page = out / "meetings" / el(1) / "index.html"
            self.assertIn("Village Board Meeting", page.read_text(encoding="utf-8"))
            self.assertFalse((out / "meetings" / "pg_7").exists())
            self.assertIn(f"/meetings/{el(1)}/", (out / "sitemap.xml").read_text(encoding="utf-8"))
            # A page that is already there is left alone.
            page.write_text("<p>kept</p>", encoding="utf-8")
            pages.main()
            self.assertEqual(page.read_text(encoding="utf-8"), "<p>kept</p>")

    def test_readers_get_the_index_without_hidden_boards_and_the_preview_gets_all(self):
        index = self.dir / "archive.json"
        index.write_text(json.dumps([{"id": el(1), "source": "village"}, {"id": "pg_7", "source": "town"}]),
                         encoding="utf-8")
        inst = self.dir / "instance.json"
        inst.write_text(json.dumps(_raw_hidden()), encoding="utf-8")
        cfg = InstanceConfig(json.loads(inst.read_text(encoding="utf-8")), inst)
        empty = self.dir / "none.json"
        with mock.patch.object(visibility, "ARCHIVE_INDEX_JSON", index), \
             mock.patch.object(visibility, "MEETINGS_JSON", empty), \
             mock.patch.object(visibility, "UPCOMING_JSON", empty):
            visibility.write_views(cfg, self.dir / "public", self.dir / "public" / "preview")
            public = json.loads((self.dir / "public" / "data" / "archive.json").read_text(encoding="utf-8"))
            preview = json.loads((self.dir / "public" / "preview" / "data" / "archive.json").read_text(encoding="utf-8"))
            self.assertEqual([r["source"] for r in public], ["village"])
            self.assertEqual(len(preview), 2)
            # No index written yet: nothing is staged, and nothing breaks.
            with mock.patch.object(visibility, "ARCHIVE_INDEX_JSON", self.dir / "absent.json"):
                visibility.write_views(cfg, self.dir / "public2")
            self.assertFalse((self.dir / "public2" / "data" / "archive.json").exists())


def _raw_hidden() -> dict:
    raw = raw_instance()
    raw["jurisdictions"][4]["hidden"] = True
    return raw


if __name__ == "__main__":
    unittest.main()
