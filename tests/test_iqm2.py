"""IQM2 adapter against saved pages from the City of Mosinee's portal (tests/fixtures/iqm2/, fetched Nov. 20, 2026):
the calendar list with 14 of 2026's 116 rows, an empty calendar range, three meeting pages (agenda and minutes
posted; nothing posted yet; cancelled) and the text of the Sept. 14 Common Council minutes PDF as pdfplumber reads
it. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from engine.adapters import iqm2, rule_schedule
from engine.config import InstanceConfig
from engine.errors import AdapterStructureError, UpstreamUnavailableError

from helpers import raw_instance

FIXTURES = Path(__file__).parent / "fixtures" / "iqm2"
BASE = "https://mosineecitywi.iqm2.com"
CFG = {"adapter": "iqm2", "base": BASE, "portal": f"{BASE}/Citizens/Default.aspx"}
NOV_20 = date(2026, 11, 20)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _meeting_url(meeting_id: int) -> str:
    return f"{BASE}/Citizens/Detail_Meeting.aspx?ID={meeting_id}"


class _Response:
    def __init__(self, text="", status_code=200):
        self.text, self.status_code = text, status_code


class _Portal:
    """Stands in for requests.get: the calendar and the meeting pages, counting what was asked for."""

    def __init__(self, calendar: str | None = None):
        self.calendar = _fixture("calendar.html") if calendar is None else calendar
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        if "/Citizens/Calendar.aspx?" in url:
            return _Response(self.calendar)
        page = FIXTURES / f"meeting_{url.rsplit('=', 1)[1]}.html"
        return _Response(page.read_text(encoding="utf-8")) if page.exists() else _Response(status_code=404)


def _on(day: date):
    class _Day(date):
        @classmethod
        def today(cls):
            return day
    return _Day


class Iqm2TestCase(unittest.TestCase):
    def setUp(self):
        iqm2._CALENDARS.clear()
        self.addCleanup(iqm2._CALENDARS.clear)
        self.portal = _Portal()
        self.pdf = mock.Mock(return_value=_fixture("minutes_1660.txt"))
        for p in (mock.patch.object(requests, "get", self.portal),
                  mock.patch.object(iqm2, "fetch_pdf_text", self.pdf)):
            p.start()
            self.addCleanup(p.stop)
        self.today(NOV_20)

    def today(self, day: date):
        for module in (iqm2, rule_schedule):
            p = mock.patch.object(module, "date", _on(day))
            p.start()
            self.addCleanup(p.stop)


class ListingTest(Iqm2TestCase):
    def test_past_meetings_with_agendas_newest_first(self):
        rows = iqm2.list_recent(CFG, date(2026, 3, 1))
        self.assertEqual([r["id"] for r in rows],
                         ["3519", "3507", "3511", "3513", "3496", "3500", "3503", "3504", "3493", "3483"])
        self.assertEqual(rows[0], {"id": "3519", "name": "Personnel & Finance Committee - Special Meeting",
                                   "date": "2026-11-18", "url": _meeting_url(3519)})
        self.assertEqual(rows[1]["name"], "Common Council - Regular Meeting")
        self.assertNotIn("3435", [r["id"] for r in rows], "the cancelled March 16 Fire District meeting is left out")
        self.assertNotIn("3526", [r["id"] for r in rows], "Nov. 23 hasn't happened yet")

    def test_since_bounds_the_list(self):
        self.assertEqual([r["id"] for r in iqm2.list_recent(CFG, date(2026, 11, 1))], ["3519", "3507", "3511"])

    def test_meeting_with_no_agenda_yet_is_left_out(self):
        self.today(date(2026, 12, 14))
        self.assertEqual(iqm2.list_recent(CFG, date(2026, 12, 1)), [],
                         "Dec. 14's meetings are on the calendar with no agenda posted")
        self.assertEqual([m["name"] for m in iqm2.fetch_upcoming(CFG, "mosinee", 7)],
                         ["Public Safety Committee - Regular Committee Meeting", "Common Council - Regular Meeting"])

    def test_bodies_allow_list(self):
        cfg = {**CFG, "bodies": ["Common Council", "Plan Commission"]}
        rows = iqm2.list_recent(cfg, date(2026, 10, 10))
        self.assertEqual([r["id"] for r in rows], ["3507", "3496"])
        self.assertEqual([m["name"] for m in iqm2.fetch_upcoming(cfg, "mosinee", 45)],
                         ["Common Council - Budget Meeting", "Common Council - Regular Meeting"])

    def test_ids_are_the_portals_own_and_stable(self):
        first = iqm2.parse_calendar(self.portal.calendar, BASE)
        again = iqm2.parse_calendar(self.portal.calendar, BASE)
        self.assertEqual([m["id"] for m in first], [m["id"] for m in again])
        self.assertEqual(len(first), 14)
        self.assertEqual(len({m["id"] for m in first}), 14, "no two meetings share an id")
        for m in first:
            self.assertTrue(m["id"].isdigit())
            self.assertEqual(m["url"], _meeting_url(int(m["id"])), "the doc_url carries the same id")
            self.assertEqual(iqm2._meeting_id(m["url"]), m["id"])

    def test_calendar_row_details(self):
        by_id = {m["id"]: m for m in iqm2.parse_calendar(self.portal.calendar, BASE)}
        self.assertTrue(by_id["3435"]["cancelled"])
        self.assertEqual(by_id["3483"]["minutes_url"], f"{BASE}/Citizens/FileOpen.aspx?Type=12&ID=1660&Inline=True")
        self.assertIsNone(by_id["3507"]["minutes_url"])
        self.assertTrue(by_id["3504"]["agenda"], "an agenda behind the FileView viewer is still a posted agenda")
        self.assertEqual((by_id["3503"]["date"], by_id["3503"]["time"]), ("2026-10-07", ""), "no time set")
        self.assertEqual((by_id["3493"]["body"], by_id["3493"]["time"]), ("Common Council", "6:20 PM"))
        self.assertFalse(by_id["3520"]["agenda"])

    def test_empty_range_is_no_meetings(self):
        self.portal.calendar = _fixture("calendar_empty.html")
        self.assertEqual(iqm2.list_recent(CFG, date(2026, 11, 1)), [])
        self.assertEqual(iqm2.fetch_upcoming(CFG, "mosinee", 45), [])

    def test_structure_drift_raises(self):
        self.portal.calendar = "<html><body><h1>Meeting Calendar</h1><p>We've redesigned!</p></body></html>"
        with self.assertRaises(AdapterStructureError):
            iqm2.list_recent(CFG, date(2026, 11, 1))
        with self.assertRaises(AdapterStructureError):
            iqm2.fetch_upcoming(CFG, "mosinee", 45)
        self.portal.calendar = _fixture("calendar.html").replace("Board:&#09;", "Committee:&#09;")
        with self.assertRaises(AdapterStructureError, msg="rows are there but can't be read"):
            iqm2.list_recent(CFG, date(2026, 11, 1))

    def test_outage_raises(self):
        for answer in (requests.ConnectionError("connection refused"), _Response(status_code=503)):
            down = mock.Mock(side_effect=answer if isinstance(answer, Exception) else None, return_value=answer)
            with mock.patch.object(requests, "get", down), mock.patch("engine.net.time.sleep"):
                with self.assertRaises(UpstreamUnavailableError):
                    iqm2.list_recent(CFG, date(2026, 11, 1))
                with self.assertRaises(UpstreamUnavailableError):
                    iqm2.fetch_upcoming(CFG, "mosinee", 45)
            self.assertEqual(down.call_count, 6, "three attempts each")
        self.assertEqual(iqm2._CALENDARS, {}, "a failed read is not kept")


class UpcomingTest(Iqm2TestCase):
    def test_posted_future_meetings_by_date_then_time(self):
        rows = iqm2.fetch_upcoming(CFG, "mosinee", 45)
        self.assertEqual(rows, [
            {"date": "2026-11-23", "time": "6:00 PM", "name": "Common Council - Budget Meeting",
             "url": _meeting_url(3526), "source": "mosinee"},
            {"date": "2026-12-14", "time": "7:00 PM", "name": "Public Safety Committee - Regular Committee Meeting",
             "url": _meeting_url(3524), "source": "mosinee"},
            {"date": "2026-12-14", "time": "7:30 PM", "name": "Common Council - Regular Meeting",
             "url": _meeting_url(3520), "source": "mosinee"},
        ])

    def test_rule_fills_only_the_months_the_portal_has_not_scheduled(self):
        cfg = {**CFG, "fallback_rules": [{"name": "Common Council - Regular Meeting", "weekday": 0, "nth": 2,
                                          "time": "7:30 PM"}]}
        rows = iqm2.fetch_upcoming(cfg, "mosinee", 60)
        council = [(r["date"], r["url"]) for r in rows if r["name"] == "Common Council - Regular Meeting"]
        self.assertEqual(council, [("2026-12-14", _meeting_url(3520)),              # posted: not projected twice
                                   ("2027-01-11", f"{BASE}/Citizens/Default.aspx")])  # not on the portal yet

    def test_cancelled_meeting_is_not_projected_back(self):
        row = self.portal.calendar.index("Detail_Meeting.aspx?ID=3520")
        self.portal.calendar = (self.portal.calendar[:row]
                                + self.portal.calendar[row:].replace("Status:&#09;Scheduled", "Status:&#09;Cancelled", 1))
        cfg = {**CFG, "fallback_rules": [{"name": "Common Council - Regular Meeting", "weekday": 0, "nth": 2,
                                          "time": "7:30 PM"}]}
        rows = iqm2.fetch_upcoming(cfg, "mosinee", 45)
        self.assertEqual([r["name"] for r in rows if r["date"] == "2026-12-14"],
                         ["Public Safety Committee - Regular Committee Meeting"])


class RequestsTest(Iqm2TestCase):
    def test_one_calendar_request_serves_a_run(self):
        cfg = {**CFG, "minutes_upgrade_window_days": 120, "days_ahead": 60}
        iqm2.list_recent(cfg, date(2026, 11, 6))                         # CI: --days 14
        self.assertIsNone(iqm2.fetch_minutes_text(cfg, _meeting_url(3507)), "listed with no minutes: no request")
        self.assertIsNone(iqm2.fetch_minutes_text(cfg, _meeting_url(3496)))
        self.assertIsNone(iqm2.fetch_agenda_text(cfg, _meeting_url(3520)), "listed with no agenda: no request")
        iqm2.fetch_upcoming(cfg, "mosinee", 60)
        self.assertEqual(self.portal.urls, [f"{BASE}/Citizens/Calendar.aspx?From=7/23/2026&To=1/19/2027"],
                         "from the minutes window back (120 days) to days_ahead forward (60)")
        self.pdf.assert_not_called()

    def test_wider_request_replaces_the_kept_range(self):
        iqm2.fetch_upcoming(CFG, "mosinee", 30)
        iqm2.list_recent(CFG, date(2026, 11, 6))
        self.assertEqual(self.portal.urls, [f"{BASE}/Citizens/Calendar.aspx?From=11/20/2026&To=12/20/2026",
                                            f"{BASE}/Citizens/Calendar.aspx?From=9/6/2026&To=1/19/2027"])


class AgendaTextTest(Iqm2TestCase):
    def test_agenda_is_the_meeting_pages_outline(self):
        text = iqm2.fetch_agenda_text(CFG, _meeting_url(3483))
        self.assertEqual(self.portal.urls, [_meeting_url(3483)])
        self.assertEqual(text.splitlines()[:6], ["Common Council", "Regular Meeting", "9/14/2026 7:30 PM",
                                                 "City Hall Council Chambers", "225 Main Street Mosinee, WI 54455", ""])
        self.assertIn("\n10. Public Works Committee:\n  A. Discussion & possible action: 2026 Leaf Collection Contract.\n", text)
        self.assertIn("  A. Discussion and possible action: Ordinance No. 2026-01 ~ An Ordinance repealing", text)
        self.assertTrue(text.endswith("18. Adjourn."))
        self.assertNotIn("Staff Report", text, "attachment rows are left out")
        self.assertNotIn("<", text)
        self.assertNotIn("&amp;", text)

    def test_nothing_to_read(self):
        self.assertIsNone(iqm2.fetch_agenda_text(CFG, _meeting_url(3520)), "no agenda posted yet")
        self.assertIsNone(iqm2.fetch_agenda_text(CFG, _meeting_url(3435)), "cancelled")
        self.assertIsNone(iqm2.fetch_agenda_text(CFG, _meeting_url(9999)), "HTTP 404")
        asked = len(self.portal.urls)
        self.assertIsNone(iqm2.fetch_agenda_text(CFG, f"{BASE}/Citizens/Default.aspx"), "a rule-projected meeting's link")
        self.assertIsNone(iqm2.fetch_agenda_text(CFG, "https://example.com/agenda.pdf"))
        self.assertEqual(len(self.portal.urls), asked, "not a meeting page: no request")

    def test_page_fetch_failure_is_no_text(self):
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("reset")):
            self.assertIsNone(iqm2.fetch_agenda_text(CFG, _meeting_url(3483)))
            self.assertIsNone(iqm2.fetch_minutes_text(CFG, _meeting_url(3483)))


class MinutesTextTest(Iqm2TestCase):
    MINUTES_PDF = f"{BASE}/Citizens/FileOpen.aspx?Type=12&ID=1660&Inline=True"

    def test_minutes_listed_on_the_calendar(self):
        iqm2.list_recent(CFG, date(2026, 9, 1))
        text = iqm2.fetch_minutes_text(CFG, _meeting_url(3483))
        self.pdf.assert_called_once_with(self.MINUTES_PDF, timeout=60)
        self.assertEqual(len(self.portal.urls), 1, "the calendar already had the link: no meeting-page request")
        self.assertIn("RESULT: APPROVED [UNANIMOUS]\nMOVER: Robert Stephens, Ward 3\nSECONDER: Mark Gewiss, Ward 1\n"
                      "AYES: Viau, Priest, Gewiss, Pasha, Stephens, Mielke", text)
        self.assertIn("below the health advisory level.\nRESULT: INFORMATIONAL ITEM", text,
                      "the page footer that fell between an item and its result is gone")
        self.assertNotRegex(text, r"(?m)^Page \d+$")

    def test_minutes_found_on_the_meeting_page_in_a_fresh_process(self):
        text = iqm2.fetch_minutes_text(CFG, _meeting_url(3483))
        self.assertEqual(self.portal.urls, [_meeting_url(3483)])
        self.pdf.assert_called_once_with(self.MINUTES_PDF, timeout=60)
        self.assertIn("MOVER: Mark Viau, Ward 5", text)

    def test_no_minutes_yet(self):
        self.assertIsNone(iqm2.fetch_minutes_text(CFG, _meeting_url(3520)))
        self.assertIsNone(iqm2.fetch_minutes_text(CFG, "https://example.com/minutes.pdf"))
        self.pdf.assert_not_called()

    def test_minutes_that_are_not_a_readable_pdf(self):
        self.pdf.return_value = None
        self.assertIsNone(iqm2.fetch_minutes_text(CFG, _meeting_url(3483)))

    def test_word_minutes_behind_the_viewer_are_not_read(self):
        page = _fixture("meeting_3483.html").replace("FileOpen.aspx?Type=12&amp;ID=1660&amp;Inline=True",
                                                     "FileView.aspx?Type=12&amp;ID=1660")
        with mock.patch.object(requests, "get", return_value=_Response(page)):
            self.assertIsNone(iqm2.fetch_minutes_text(CFG, _meeting_url(3483)))
        self.pdf.assert_not_called()


class MosineeConfigTest(unittest.TestCase):
    def test_jurisdiction_file_passes_validation(self):
        root = Path(__file__).parent.parent
        mosinee = json.loads((root / "docs" / "new-boards" / "mosinee.json").read_text(encoding="utf-8"))
        raw = raw_instance()
        raw["jurisdictions"].append(mosinee)
        jur = InstanceConfig(raw, Path("instance.json")).jurisdiction("mosinee")
        self.assertEqual(jur.agendas_adapter, "iqm2")
        self.assertIsNone(jur.video)
        posted = {m["body"] for m in iqm2.parse_calendar(_fixture("calendar.html"), BASE)}
        self.assertIn("Common Council", set(jur.agendas["bodies"]) & posted, "bodies use the portal's own names")


if __name__ == "__main__":
    unittest.main()
