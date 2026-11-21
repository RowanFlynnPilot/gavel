"""Static pages (tracker permalink, correction link, archive footers) and the
calendar feed's UID suffix. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from engine import ics, pages
from helpers import make_config

BASE = "https://pages.example/tracker"
MEETING = {"id": "-SFidjEgoHg", "source": "county", "title": "County Board", "date": "October 20, 2026",
           "committee": "County Board", "overview": "The board met.", "isAgendaOnly": False}


def _footer(page: str) -> str:
    return re.search(r"<footer>.*</footer>", page).group(0)


class FooterTest(unittest.TestCase):
    def test_permalink_uses_the_hash_the_frontend_reads(self):
        footer = _footer(pages.render_page(make_config(), MEETING, BASE, noindex=False))
        self.assertIn(f'<a href="{BASE}/#m/-SFidjEgoHg">Permalink in the tracker</a>', footer)
        self.assertNotIn("Request a correction", footer, "no corrections address, no link")

    def test_correction_link_when_the_instance_has_an_address(self):
        cfg = make_config(corrections_email="editor@ledger.example")
        footer = _footer(pages.render_page(cfg, MEETING, BASE, noindex=False))
        href = re.search(r'<a href="(mailto:[^"]+)">Request a correction</a>', footer).group(1)
        self.assertTrue(href.startswith("mailto:editor@ledger.example?subject="))
        decoded = unquote(href.replace("&amp;", "&"))
        self.assertIn("subject=Correction request: County Board (October 20, 2026)", decoded)
        self.assertIn(f"Page: {BASE}/meetings/-SFidjEgoHg/", decoded)


class ArchiveFooterTest(unittest.TestCase):
    OLD = ('<p>body</p><footer>Published by <a href="https://ledger.example">Riverton Ledger</a> · '
           '<a href="{base}/#{mid}">Permalink in the tracker</a></footer>')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _page(self, mid: str, html: str) -> Path:
        (self.dir / mid).mkdir()
        path = self.dir / mid / "index.html"
        path.write_text(html, encoding="utf-8")
        return path

    def test_old_footers_get_the_current_permalink_and_the_correction_link(self):
        cfg = make_config(corrections_email="editor@ledger.example")
        old = self._page("-oldVideo01", self.OLD.format(base=BASE, mid="-oldVideo01"))
        current = self._page("bb_77", pages.render_page(cfg, {**MEETING, "id": "bb_77"}, BASE, noindex=False))
        current_before = current.read_text(encoding="utf-8")

        self.assertEqual(pages.refresh_archive_footers(cfg, self.dir, BASE), 1)
        text = old.read_text(encoding="utf-8")
        self.assertIn(f'<a href="{BASE}/#m/-oldVideo01">Permalink in the tracker</a> · <a href="mailto:', text)
        self.assertIn("Correction%20request%3A%20meeting%20-oldVideo01", text)
        self.assertTrue(text.startswith("<p>body</p><footer>Published by"), "only the footer links change")
        self.assertEqual(current.read_text(encoding="utf-8"), current_before)
        self.assertEqual(pages.refresh_archive_footers(cfg, self.dir, BASE), 0, "idempotent")

    def test_without_an_address_only_the_permalink_form_changes(self):
        old = self._page("abc", self.OLD.format(base=BASE, mid="abc"))
        self.assertEqual(pages.refresh_archive_footers(make_config(), self.dir, BASE), 1)
        text = old.read_text(encoding="utf-8")
        self.assertTrue(text.endswith(f'<a href="{BASE}/#m/abc">Permalink in the tracker</a></footer>'))
        self.assertEqual(pages.refresh_archive_footers(make_config(), self.dir, BASE), 0)


class ArchiveUpdatesTest(unittest.TestCase):
    """A meeting rebuilt this run but too old for the displayed list still
    gets its archive page rewritten."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pages_dir = self.root / "meetings"
        self.pages_dir.mkdir()
        self.queue = self.root / "archive_updates.json"

    def test_queued_meetings_get_their_page_rewritten_and_the_queue_is_cleared(self):
        from engine import publish
        old = {**MEETING, "id": "old_meeting", "overview": "The ordinance failed 5-6.", "badge": "new"}
        shown = {**MEETING, "id": "shown", "overview": "Displayed; rendered from meetings.json."}
        (self.pages_dir / "old_meeting").mkdir()
        (self.pages_dir / "old_meeting" / "index.html").write_text(
            "<p>The ordinance passed.</p>", encoding="utf-8")
        publish.queue_archive_updates(self.queue, [old])
        publish.queue_archive_updates(self.queue, [shown, {**old, "overview": "The ordinance failed, 5-6."}])
        queued = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual([m["id"] for m in queued], ["old_meeting", "shown"], "merged by ID")
        self.assertIsNone(queued[0]["badge"])

        n = pages.render_archive_updates(make_config(), self.pages_dir, BASE, False, self.queue, {"shown"})
        self.assertEqual(n, 1)
        page = (self.pages_dir / "old_meeting" / "index.html").read_text(encoding="utf-8")
        self.assertIn("The ordinance failed, 5-6.", page)
        self.assertNotIn("The ordinance passed.", page)
        self.assertFalse((self.pages_dir / "shown").exists(), "displayed meetings are rendered elsewhere")
        self.assertFalse(self.queue.exists())

    def test_no_queue_or_a_broken_one_writes_nothing(self):
        cfg = make_config()
        self.assertEqual(pages.render_archive_updates(cfg, self.pages_dir, BASE, False, self.queue, set()), 0)
        self.queue.write_text("{not json", encoding="utf-8")
        self.assertEqual(pages.render_archive_updates(cfg, self.pages_dir, BASE, False, self.queue, set()), 0)
        self.assertFalse(self.queue.exists())
        self.queue.write_text(json.dumps([{"id": "../escape", "title": "x"}, "junk"]), encoding="utf-8")
        self.assertEqual(pages.render_archive_updates(cfg, self.pages_dir, BASE, False, self.queue, set()), 0)
        self.assertEqual(list(self.pages_dir.iterdir()), [])


class IcsUidTest(unittest.TestCase):
    def _uids(self, cfg) -> list[str]:
        day = (date.today() + timedelta(days=3)).isoformat()
        upcoming = {"county": [{"date": day, "time": "7:00 PM", "name": "County Board Meeting", "url": "", "source": "county"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upcoming.json"
            path.write_text(json.dumps(upcoming), encoding="utf-8")
            with mock.patch.object(ics, "UPCOMING_JSON", path):
                events = ics.build_events(cfg, 60)
        self.day = day
        return [e["uid"] for e in events]

    def test_default_suffix(self):
        (uid,) = self._uids(make_config())
        self.assertTrue(uid.endswith("@gavel"))

    def test_configured_suffix_reproduces_an_existing_feeds_uids(self):
        (uid,) = self._uids(make_config(ics_uid_domain="marathon-meetings"))
        # The reference feed's formula: sha1("<source>|<date>|<name lowercased>")[:20] + "@marathon-meetings".
        expected = hashlib.sha1(f"county|{self.day}|county board meeting".encode()).hexdigest()[:20]
        self.assertEqual(uid, expected + "@marathon-meetings")
        rendered = ics.render(make_config(ics_uid_domain="marathon-meetings"),
                              [{"uid": uid, "date": date.today(), "start": None, "summary": "x", "url": ""}])
        self.assertIn(f"UID:{uid}\r\n", rendered)


if __name__ == "__main__":
    unittest.main()
