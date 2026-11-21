"""Revize adapter against saved pages from two villages (tests/fixtures/revize/, trimmed to a few rows each):
Rib Mountain's Village Board and Plan Commission pages (meeting type in the row, files in dated folders) and
Rothschild's Village Board and Water Utility Commission pages (date only, loose file names), as served on
Nov. 20, 2026. The two .txt fixtures are the text of a real agenda and the start of real minutes. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from engine.adapters import revize
from engine.config import InstanceConfig
from engine.errors import AdapterStructureError, UpstreamUnavailableError
from helpers import raw_instance

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "revize"
RIB_PAGES = "https://www.ribmountainwi.gov/government/meetings/"
RIB_FILES = "https://www.ribmountainwi.gov/Document_Center/Agenda%20&%20Minutes/"
ROTH_PAGES = "https://www.rothschildwi.com/government/village_agendas_recordings___minute_meetings/"
RIB = {"adapter": "revize", "portal": RIB_PAGES + "index.php", "pages": [
    {"body": "Village Board", "url": RIB_PAGES + "town_board.php"},
    {"body": "Plan Commission", "url": RIB_PAGES + "plan_commission.php"}]}
ROTH = {"adapter": "revize", "portal": ROTH_PAGES + "index.php", "pages": [
    {"body": "Village Board", "url": ROTH_PAGES + "index.php"},
    {"body": "Water Utility Commission", "url": ROTH_PAGES + "water_utility_commission.php"}]}
PAGES = {
    RIB_PAGES + "town_board.php": "rib_mountain_village_board.html",
    RIB_PAGES + "plan_commission.php": "rib_mountain_plan_commission.html",
    ROTH_PAGES + "index.php": "rothschild_village_board.html",
    ROTH_PAGES + "water_utility_commission.php": "rothschild_water_utility_commission.html",
}
REGULAR_NOV_17 = RIB_FILES + "Town%20Board/2026/Agenda/01%20261117%20VB%20Agenda.pdf#rz967"
RESCHEDULED_NOV_5 = RIB_FILES + "Town%20Board/2026/Agenda/01%20261105%20VB%20Agenda.pdf#rz962"
MINUTES_NOV_5 = RIB_FILES + "Town%20Board/2026/Minutes/261105%20VB%20Minutes.pdf"
SINCE = date(2026, 9, 6)


class _Response:
    def __init__(self, text="", status_code=200, content=b""):
        self.text, self.status_code, self.content = text, status_code, content


def _page(url, **kwargs):
    return _Response(text=(FIXTURES / PAGES[url]).read_text(encoding="utf-8"))


def _on(year, month, day):
    """Patches that make `date.today()` that day for the adapter and the rule projection."""
    class Day(date):
        @classmethod
        def today(cls):
            return cls(year, month, day)
    return mock.patch.multiple("engine.adapters.revize", date=Day), \
        mock.patch("engine.adapters.rule_schedule.date", Day)


class RevizeCase(unittest.TestCase):
    today = (2026, 11, 20)

    def setUp(self):
        revize._pages.clear()
        self.addCleanup(revize._pages.clear)
        for p in _on(*self.today):
            p.start()
            self.addCleanup(p.stop)
        get = mock.patch("engine.net.requests.get", side_effect=_page)
        self.get = get.start()
        self.addCleanup(get.stop)
        pdf = mock.patch("engine.adapters.revize.fetch_pdf_text", return_value=None)
        self.pdf = pdf.start()
        self.addCleanup(pdf.stop)


class ParsePageTest(unittest.TestCase):
    def test_rows_are_meetings_and_year_headings_are_not(self):
        page = (FIXTURES / "rib_mountain_village_board.html").read_text(encoding="utf-8")
        self.assertIn('name="agendas_minutes_rz876"', page, "the 2026 heading has a record number of its own")
        self.assertIn('name="agendas_minutes_rz778"', page, "so does the 2025 heading, which follows a meeting row")
        rows = revize.parse_page(page, RIB_PAGES + "town_board.php", "Village Board")
        self.assertEqual([(r["record"], r["date"]) for r in rows], [
            ("970", "2026-12-01"), ("966", "2026-11-17"), ("967", "2026-11-17"), ("962", "2026-11-05"),
            ("959", "2026-10-20"), ("960", "2026-10-20"), ("886", "2025-12-29")])
        self.assertEqual(rows[3], {
            "record": "962", "date": "2026-11-05", "name": "Village Board Re-scheduled Regular Meeting", "held": True,
            "agenda": RESCHEDULED_NOV_5.split("#")[0], "minutes": MINUTES_NOV_5, "url": RESCHEDULED_NOV_5})
        self.assertEqual(rows[6]["name"], "Special Village Board", "the row already names the body")

    def test_names(self):
        for title, name in (("Regular Meeting", "Plan Commission Regular Meeting"),
                            ("", "Plan Commission"),
                            ("Regular Meeting *Amended Agenda*", "Plan Commission Regular Meeting"),
                            ("Regular Meeting - REVISED", "Plan Commission Regular Meeting"),
                            ("Regular Meeting - Amended Agenda & Packet", "Plan Commission Regular Meeting"),
                            ("Special Joint Town Board & Plan Commission", "Special Joint Town Board & Plan Commission"),
                            ("Special Budget #3 @ Public Works Garage", "Plan Commission Special Budget #3 @ Public Works Garage")):
            self.assertEqual(revize._name("Plan Commission", title), name)


class ListRecentTest(RevizeCase):
    def test_rib_mountain_lists_past_meetings_with_agendas_newest_first(self):
        rows = revize.list_recent(RIB, SINCE)
        self.assertEqual([(r["date"], r["name"]) for r in rows], [
            ("2026-11-17", "Village Board Regular Meeting"),
            ("2026-11-17", "Village Board Special Budget Hearing"),
            ("2026-11-11", "Plan Commission Regular Meeting"),
            ("2026-11-05", "Village Board Re-scheduled Regular Meeting"),
            ("2026-10-20", "Village Board Regular Meeting"),
            ("2026-10-20", "Village Board Special Budget Workshop #4"),
            ("2026-09-09", "Plan Commission Regular Meeting"),
        ])
        self.assertEqual(rows[0], {"id": "936967", "name": "Village Board Regular Meeting", "date": "2026-11-17",
                                   "url": REGULAR_NOV_17})
        names = [r["name"] for r in rows]
        self.assertFalse(any("CANCEL" in n.upper() for n in names), "Oct. 14 Plan Commission was cancelled")

    def test_a_meeting_with_no_agenda_posted_is_not_listed(self):
        for p in _on(2026, 12, 2):      # the day after the Dec. 1 meeting, whose row has no files yet
            p.start()
            self.addCleanup(p.stop)
        rows = revize.list_recent(RIB, SINCE)
        self.assertNotIn("2026-12-01", [r["date"] for r in rows])
        self.assertEqual(rows[0]["date"], "2026-11-17")

    def test_since_is_the_cutoff(self):
        self.assertEqual({r["date"] for r in revize.list_recent(RIB, date(2026, 11, 6))}, {"2026-11-17", "2026-11-11"})

    def test_rothschild_takes_the_date_from_the_row_not_the_file_name(self):
        for p in _on(2026, 11, 24):
            p.start()
            self.addCleanup(p.stop)
        rows = revize.list_recent(ROTH, SINCE)
        self.assertEqual([(r["date"], r["name"]) for r in rows], [
            ("2026-11-23", "Village Board"),            # joint meeting: on both pages with one agenda file, listed once
            ("2026-11-09", "Village Board"),
            ("2026-10-26", "Village Board"),
            ("2026-10-26", "Water Utility Commission"),
            ("2026-10-12", "Village Board"),
        ])
        self.assertEqual(rows[1]["url"], "https://www.rothschildwi.com/11-09-2026.pdf#rz999")
        self.assertEqual(rows[4]["url"], "https://www.rothschildwi.com/10-12-2026%20vb.pdf#rz991")
        self.assertTrue(rows[0]["url"].endswith("Joint%20Village%20Board%20&%20Water%20Utility%20Agenda.pdf#rz1009"))

    def test_quorum_notice_is_not_a_meeting(self):
        rows = revize.list_recent(ROTH, date(2025, 12, 1))
        self.assertNotIn("2025-12-18", [r["date"] for r in rows], "'Quorum No Action' row")

    def test_bodies_allow_list(self):
        rows = revize.list_recent({**RIB, "bodies": ["Plan Commission"]}, SINCE)
        self.assertEqual({r["name"] for r in rows}, {"Plan Commission Regular Meeting"})
        self.assertEqual(self.get.call_count, 1, "the other body's page isn't fetched")

    def test_one_request_per_page_per_run(self):
        revize.list_recent(RIB, SINCE)
        revize.fetch_upcoming(RIB, "rib_mountain", 45)
        revize.fetch_minutes_text(RIB, RESCHEDULED_NOV_5)
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.get.call_args.kwargs["headers"]["User-Agent"], "Mozilla/5.0")


class MeetingIdTest(RevizeCase):
    def test_ids_are_digits_unique_and_the_same_on_every_run(self):
        first = revize.list_recent(RIB, SINCE)
        revize._pages.clear()
        again = revize.list_recent(RIB, SINCE)
        ids = [r["id"] for r in first]
        self.assertEqual(ids, [r["id"] for r in again])
        self.assertTrue(all(i.isdigit() for i in ids))
        self.assertEqual(len(set(ids)), len(ids), "two Village Board meetings on one day get different ids")

    def test_id_survives_an_edited_date_and_a_replaced_agenda_file(self):
        before = {r["id"] for r in revize.list_recent(RIB, SINCE)}
        page = (FIXTURES / "rib_mountain_village_board.html").read_text(encoding="utf-8")
        edited = page.replace("01 261105 VB Agenda.pdf", "01 261105 VB Agenda Amended.pdf").replace("11/05/26", "11/04/26")
        self.assertNotEqual(page, edited)
        revize._pages.clear()
        self.get.side_effect = lambda url, **kw: _Response(text=edited) if url.endswith("town_board.php") else _page(url)
        after = revize.list_recent(RIB, SINCE)
        self.assertEqual({r["id"] for r in after}, before)
        self.assertIn(("936962", "2026-11-04"), [(r["id"], r["date"]) for r in after])

    def test_two_villages_cannot_share_an_id(self):
        # Record numbers are per site (both villages have a row 967), so the id starts with a number for the site.
        self.assertNotEqual(revize._site_number(RIB), revize._site_number(ROTH))
        rib = {r["id"] for r in revize.list_recent(RIB, date(2025, 1, 1))}
        roth = {r["id"] for r in revize.list_recent(ROTH, date(2025, 1, 1))}
        self.assertFalse(rib & roth)
        self.assertEqual(revize._site_number({"portal": "https://ribmountainwi.gov/x"}), revize._site_number(RIB),
                         "www. or a different page on the site doesn't change it")


class AgendaTextTest(RevizeCase):
    def test_agenda_text_is_read_from_the_agenda_file(self):
        self.pdf.return_value = (FIXTURES / "rib_mountain_agenda_2026-11-17.txt").read_text(encoding="utf-8")
        text = revize.fetch_agenda_text(RIB, REGULAR_NOV_17)
        self.pdf.assert_called_once_with(REGULAR_NOV_17.split("#")[0])
        self.get.assert_not_called()        # no page fetch: the doc_url names the file
        self.assertIn("NOVEMBER 17, 2026, MEETING AT 6:00 P.M.", text)
        self.assertIn("6. Discussion and Action on Resolution # 26-12 Adoption of the Village Tax Levy", text)

    def test_symbol_font_bullets_are_dropped(self):
        self.pdf.return_value = "Comments From the Public\n A resident spoke about the levy.\n" + "x" * 100
        self.assertNotIn("", revize.fetch_agenda_text(RIB, REGULAR_NOV_17))

    def test_scanned_agenda_has_no_text(self):
        self.pdf.return_value = None        # what engine.pdftext returns for a scan with no text layer
        self.assertIsNone(revize.fetch_agenda_text(RIB, REGULAR_NOV_17))

    def test_a_row_with_no_agenda_links_its_page_and_has_no_text(self):
        upcoming = revize.fetch_upcoming(RIB, "rib_mountain", 45)
        dec_1 = next(u for u in upcoming if u["date"] == "2026-12-01")
        self.assertEqual(dec_1["url"], RIB_PAGES + "town_board.php#agendas_minutes_rz970")
        self.assertIsNone(revize.fetch_agenda_text(RIB, dec_1["url"]))
        self.assertIsNone(revize.fetch_agenda_text(RIB, RIB_PAGES + "town_board.php"))
        self.pdf.assert_not_called()

    def test_docx_is_read_and_legacy_doc_is_not(self):
        body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in (
            "VILLAGE BOARD MEETING MINUTES", "Call to Order:",
            "Motioned by Stroik/Luedtke to Approve the Minutes as Presented. Questioned and carried 7:0.",
            "Motioned by Kennedy/Schremp to Adjourn. Questioned and carried 7:0."))
        xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               f"<w:body>{body}</w:body></w:document>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        with mock.patch("engine.adapters.revize.requests.get", return_value=_Response(content=buf.getvalue())) as get:
            text = revize.fetch_agenda_text(ROTH, "https://www.rothschildwi.com/4-28-2025.docx#rz836")
            self.assertIsNone(revize.fetch_agenda_text(ROTH, "https://www.rothschildwi.com/old%20minutes.doc#rz12"))
        get.assert_called_once()
        self.assertEqual(text.splitlines()[1], "Call to Order:")
        self.assertIn("Motioned by Kennedy/Schremp to Adjourn. Questioned and carried 7:0.", text)


class MinutesTextTest(RevizeCase):
    def test_minutes_are_found_through_the_meetings_row(self):
        self.pdf.return_value = (FIXTURES / "rothschild_minutes_2026-10-26.txt").read_text(encoding="utf-8")
        doc_url = "https://www.rothschildwi.com/10-26-2026%20Village%20Board%20Agenda.pdf#rz995"
        text = revize.fetch_minutes_text(ROTH, doc_url)
        self.pdf.assert_called_once_with("https://www.rothschildwi.com/10-26-2026%20Village%20Board%20Minutes.pdf")
        self.assertIn("Motioned by Stroik/Luedtke to Approve", text)
        self.assertIn("Questioned and carried 7:0.", text)

    def test_same_agenda_file_two_bodies_each_gets_its_own_minutes(self):
        # Oct. 26: the board's row and the utility commission's row are different records with different minutes.
        self.pdf.return_value = "minutes " * 40
        revize.fetch_minutes_text(ROTH, "https://www.rothschildwi.com/10-26-2026%20Water%20Utility%20Agenda.pdf#rz994")
        self.pdf.assert_called_once_with("https://www.rothschildwi.com/10-26-2026%20Water%20Utility%20Minutes.pdf")

    def test_none_until_minutes_are_posted(self):
        self.assertIsNone(revize.fetch_minutes_text(RIB, REGULAR_NOV_17))
        self.pdf.assert_not_called()

    def test_row_is_found_by_record_number_after_the_agenda_file_is_replaced(self):
        self.pdf.return_value = "minutes " * 40
        stored = RIB_FILES + "Town%20Board/2026/Agenda/an%20earlier%20file%20name.pdf#rz962"
        self.assertTrue(revize.fetch_minutes_text(RIB, stored))
        self.pdf.assert_called_once_with(MINUTES_NOV_5)

    def test_scanned_minutes_and_unknown_urls_give_none(self):
        self.assertIsNone(revize.fetch_minutes_text(RIB, RESCHEDULED_NOV_5), "a scan: no text layer")
        self.pdf.assert_called_once_with(MINUTES_NOV_5)
        self.assertIsNone(revize.fetch_minutes_text(RIB, RIB_FILES + "x.pdf#rz99999"), "no such row")
        self.assertIsNone(revize.fetch_minutes_text(RIB, RIB_PAGES + "town_board.php#agendas_minutes_rz970"))
        self.assertIsNone(revize.fetch_minutes_text(RIB, "https://example.com/agenda.pdf"))


class UpcomingTest(RevizeCase):
    RULES = [
        {"name": "Village Board Regular Meeting", "weekday": 1, "nth": 1, "time": "6:00 PM"},
        {"name": "Village Board Regular Meeting", "weekday": 1, "nth": 3, "time": "6:00 PM",
         "url": RIB_PAGES + "town_board.php"},
        {"name": "Plan Commission Regular Meeting", "weekday": 2, "nth": 2, "time": "5:00 PM"},
    ]

    def test_listed_meetings_merge_with_the_rules(self):
        rows = revize.fetch_upcoming({**RIB, "fallback_rules": self.RULES}, "rib_mountain", 45)
        self.assertEqual(rows, [
            # listed with no agenda yet: the row's anchor, and the time of the rule that covers it
            {"date": "2026-12-01", "time": "6:00 PM", "name": "Village Board Regular Meeting",
             "url": RIB_PAGES + "town_board.php#agendas_minutes_rz970", "source": "rib_mountain"},
            {"date": "2026-12-09", "time": "5:00 PM", "name": "Plan Commission Regular Meeting",
             "url": RIB_PAGES + "index.php", "source": "rib_mountain"},
            {"date": "2026-12-15", "time": "6:00 PM", "name": "Village Board Regular Meeting",
             "url": RIB_PAGES + "town_board.php", "source": "rib_mountain"},
        ])

    def test_without_rules_only_listed_meetings(self):
        rows = revize.fetch_upcoming(RIB, "rib_mountain", 45)
        self.assertEqual([(r["date"], r["time"], r["name"]) for r in rows],
                         [("2026-12-01", "", "Village Board Regular Meeting")])
        self.assertEqual(set(rows[0]), {"date", "time", "name", "url", "source"})

    def test_cancelled_meeting_is_neither_listed_nor_projected(self):
        for p in _on(2026, 10, 12):
            p.start()
            self.addCleanup(p.stop)
        rows = revize.fetch_upcoming({**RIB, "fallback_rules": self.RULES}, "rib_mountain", 10)
        self.assertEqual([(r["date"], r["time"], r["name"]) for r in rows], [
            # Oct. 14, the second Wednesday: the Plan Commission row says CANCELLED
            ("2026-10-20", "", "Village Board Special Budget Workshop #4"),
            ("2026-10-20", "6:00 PM", "Village Board Regular Meeting"),
        ])
        self.assertTrue(rows[1]["url"].endswith("01%20261020%20VB%20Agenda.pdf#rz960"), "posted agenda is the link")

    def test_rothschild_joint_meeting_once_with_the_rule_time(self):
        rules = [{"name": "Village Board", "weekday": 0, "nth": n, "time": "6:30 PM"} for n in (2, 4)]
        rows = revize.fetch_upcoming({**ROTH, "fallback_rules": rules}, "rothschild", 30)
        self.assertEqual([(r["date"], r["time"], r["name"]) for r in rows], [
            ("2026-11-23", "6:30 PM", "Village Board"), ("2026-12-14", "6:30 PM", "Village Board")])
        self.assertTrue(rows[0]["url"].endswith("Agenda.pdf#rz1009"))
        self.assertEqual(rows[1]["url"], ROTH_PAGES + "index.php")


class FailureTest(RevizeCase):
    def test_page_without_meeting_rows_is_structure_drift(self):
        drifted = "<html><body><h1>Village Board of Trustees</h1><ul><li>11/17/26 Regular Meeting</li></ul></body></html>"
        self.get.side_effect = lambda url, **kw: _Response(text=drifted)
        with self.assertRaises(AdapterStructureError):
            revize.list_recent(RIB, SINCE)
        with self.assertRaises(AdapterStructureError):
            revize.fetch_upcoming(RIB, "rib_mountain", 45)
        with self.assertRaises(AdapterStructureError):
            revize.fetch_minutes_text(RIB, RESCHEDULED_NOV_5)

    def test_outage_raises_and_is_not_remembered(self):
        self.get.side_effect = requests.ConnectionError("connection refused")
        with mock.patch("engine.net.time.sleep"):
            with self.assertRaises(UpstreamUnavailableError):
                revize.list_recent(RIB, SINCE)
            with self.assertRaises(UpstreamUnavailableError):
                revize.fetch_upcoming(RIB, "rib_mountain", 45)
        self.assertEqual(self.get.call_count, 6, "three attempts each time")
        self.get.side_effect = _page
        self.assertEqual(len(revize.list_recent(RIB, SINCE)), 7, "the next run reads the page again")

    def test_http_error_is_an_outage(self):
        self.get.side_effect = lambda url, **kw: _Response(status_code=503)
        with mock.patch("engine.net.time.sleep"), self.assertRaises(UpstreamUnavailableError):
            revize.list_recent(ROTH, SINCE)


class NewBoardsTest(unittest.TestCase):
    """docs/new-boards/*.json for the two Revize villages load as jurisdictions."""

    def test_jurisdiction_files_are_valid_and_cannot_collide(self):
        boards = [json.loads((ROOT / "docs" / "new-boards" / f"{key}.json").read_text(encoding="utf-8"))
                  for key in ("rib_mountain", "rothschild")]
        raw = raw_instance()
        raw["jurisdictions"] += boards
        cfg = InstanceConfig(raw, Path("instance.json"))
        for board in boards:
            jur = cfg.jurisdiction(board["key"])
            self.assertEqual(jur.agendas_adapter, "revize")
            self.assertIsNone(jur.video)
            bodies = [p["body"] for p in jur.agendas["pages"]]
            self.assertEqual(len(set(bodies)), len(bodies))
            self.assertEqual(bodies[0], "Village Board")
        self.assertEqual(len({revize._site_number(b["agendas"]) for b in boards}), 2)


if __name__ == "__main__":
    unittest.main()
