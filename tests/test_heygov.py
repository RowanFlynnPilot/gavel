"""HeyGov adapter against saved responses for the Town of Weston (tests/fixtures/heygov/, fetched Nov. 20, 2026
and trimmed): the twelve newest meetings, the meeting categories, three meetings as HeyGov's public meeting page
loads them, and the Plan Commission's Sept. 15, 2026 minutes (.docx). No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from engine.adapters import heygov
from engine.errors import AdapterStructureError, UpstreamUnavailableError

FIXTURES = Path(__file__).parent / "fixtures" / "heygov"
CFG = {"adapter": "heygov", "site": "townofwestonwi.com", "portal": "https://townofwestonwi.com/agendas-minutes"}
BOARD_RULE = {"name": "Town Board Meeting", "weekday": 1, "nth": 3, "time": "6:30 PM"}


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


MEETINGS = _load("meetings.json")
CATEGORIES = _load("categories.json")
OCT_BOARD = _load("meeting_2026-10-20_town_board.json")          # PDF agenda, .doc minutes
NOV_BOARD = _load("meeting_2026-11-17_town_board.json")          # .doc agenda, no minutes yet
SEP_PLAN = _load("meeting_2026-09-15_plan_commission.json")      # PDF agenda, .docx minutes
DOCX = (FIXTURES / "plan_commission_minutes_2026-09-15.docx").read_bytes()


def url_of(pid) -> str:
    return f"https://app.heygov.com/townofwestonwi.com/meetings/{pid}"


class _Response:
    def __init__(self, data=None, status_code=200, content=b""):
        self._data, self.status_code, self.content = data, status_code, content

    def json(self):
        if self._data is None:
            raise ValueError("not JSON")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _HeyGov:
    """Stands in for requests.get: the meeting list, the categories, single meetings and files."""

    def __init__(self, meetings=None, categories=None):
        self.meetings = MEETINGS if meetings is None else meetings
        self.categories = CATEGORIES if categories is None else categories
        self.single = {m["pid"]: m for m in (OCT_BOARD, NOV_BOARD, SEP_PLAN)}
        self.calls = []

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        if url == "https://api.heygov.com/townofwestonwi.com/meetings":
            size, page = params["limit"], params["page"]
            return _Response(self.meetings[(page - 1) * size:page * size] if isinstance(self.meetings, list) else self.meetings)
        if url == "https://api.heygov.com/townofwestonwi.com/meetings/categories":
            return _Response(self.categories)
        if url.startswith("https://api.heygov.com/townofwestonwi.com/meetings/"):
            meeting = self.single.get(url.rsplit("/", 1)[1])
            return _Response(meeting) if meeting else _Response(status_code=404)
        if url.startswith("https://files.heygov.com/") and url.endswith(".docx"):
            return _Response(content=DOCX)
        raise AssertionError(f"unexpected request: {url}")


def _run(day: date, site: _HeyGov, fn, *args):
    class _Today(date):
        @classmethod
        def today(cls):
            return day
    with mock.patch("engine.adapters.heygov.date", _Today), mock.patch("engine.adapters.rule_schedule.date", _Today), \
            mock.patch("requests.get", side_effect=site):
        return fn(*args)


class ListRecentTest(unittest.TestCase):
    def test_past_meetings_with_an_agenda_newest_first(self):
        site = _HeyGov()
        rows = _run(date(2026, 11, 20), site, heygov.list_recent, CFG, date(2026, 8, 1))
        self.assertEqual([(r["date"], r["name"]) for r in rows], [
            ("2026-11-17", "Public Hearing Budget/Special mtg of Electors/Town Board Meeting"),
            ("2026-10-20", "Town Board Meeting"),
            ("2026-09-15", "Town Board Meeting"),
            ("2026-09-15", "Plan Commission/Public Hearing"),
            ("2026-08-18", "Plan Commission: Public Hearing"),      # titled just "Public Hearing", filed under Plan Commission
            ("2026-08-18", "Town Board Meeting"),
        ])
        self.assertEqual(rows[1], {"id": "2166288277504026717154357220291954172", "name": "Town Board Meeting",
                                   "date": "2026-10-20", "url": url_of("me_01m4v446q075cqmgf26xhnhhfw")})
        self.assertEqual([u.rsplit("/", 1)[1] for u, _ in site.calls], ["meetings", "categories"], "two requests")
        self.assertEqual(site.calls[0][1], {"api": 1, "order": "desc", "limit": 25, "page": 1})

    def test_reads_further_pages_until_it_passes_the_start_date(self):
        site = _HeyGov()
        with mock.patch("engine.adapters.heygov._PAGE_SIZE", 4):
            rows = _run(date(2026, 11, 20), site, heygov.list_recent, CFG, date(2026, 7, 1))
        self.assertEqual([r["date"] for r in rows], ["2026-11-17", "2026-10-20", "2026-09-15", "2026-09-15",
                                                     "2026-08-18", "2026-08-18", "2026-07-21"])
        self.assertEqual([p["page"] for u, p in site.calls if u.endswith("/meetings")], [1, 2], "page 2 ends before July 1")

    def test_cancelled_meetings_and_meetings_with_no_agenda_yet_are_left_out(self):
        meetings = copy.deepcopy(MEETINGS)
        meetings[1]["status"] = "canceled"              # what the town's own page script treats as cancelled
        meetings[2].update(agenda_status="missing", agenda_file_url=None)
        rows = _run(date(2026, 11, 20), _HeyGov(meetings=meetings), heygov.list_recent, CFG, date(2026, 9, 1))
        self.assertEqual([(r["date"], r["name"]) for r in rows], [
            ("2026-11-17", "Public Hearing Budget/Special mtg of Electors/Town Board Meeting"),
            ("2026-09-15", meetings[3]["title"])])

    def test_bodies_keeps_only_the_named_categories(self):
        rows = _run(date(2026, 11, 20), _HeyGov(), heygov.list_recent, {**CFG, "bodies": ["Town Board"]}, date(2026, 8, 1))
        self.assertEqual([r["date"] for r in rows], ["2026-11-17", "2026-10-20", "2026-09-15", "2026-08-18"])
        self.assertTrue(all("Plan Commission" not in r["name"] for r in rows))

    def test_ids_are_digits_from_the_meeting_s_own_id_and_do_not_change(self):
        first = _run(date(2026, 11, 20), _HeyGov(), heygov.list_recent, CFG, date(2026, 1, 1))
        renamed = copy.deepcopy(MEETINGS)
        for m in renamed:
            m["title"] += " (rescheduled)"
            m["starts_at_local"] = m["starts_at_local"].replace("T00:00", "T18:30")
        later = _run(date(2026, 11, 30), _HeyGov(meetings=list(reversed(renamed))), heygov.list_recent, CFG, date(2026, 1, 1))
        self.assertTrue(all(r["id"].isdigit() for r in first))
        self.assertEqual(len({r["id"] for r in first}), len(first), "two bodies meeting the same day get different ids")
        self.assertEqual({r["url"]: r["id"] for r in first}, {r["url"]: r["id"] for r in later},
                         "a new title, time or list order changes no id")
        self.assertEqual(heygov.numeric_id("me_01m4v446q075cqmgf26xhnhhfw"), "2166288277504026717154357220291954172")
        self.assertEqual(heygov.numeric_id("ME_01M4V446Q075CQMGF26XHNHHFW"), "2166288277504026717154357220291954172")
        for bad in ("137799", "me_123", "me_01m4v446q075cqmgf26xhnhhfu", None):          # "u" is not in a ULID
            with self.assertRaises(AdapterStructureError):
                heygov.numeric_id(bad)


class UpcomingTest(unittest.TestCase):
    def test_posted_meetings_then_the_rule_for_months_not_posted(self):
        rows = _run(date(2026, 11, 13), _HeyGov(), heygov.fetch_upcoming, {**CFG, "fallback_rules": [BOARD_RULE]}, "town_weston", 45)
        self.assertEqual(rows, [
            # Posted: HeyGov has no start time for it (midnight), and the rule's Nov. 17 projection is covered by it.
            {"date": "2026-11-17", "time": "", "name": "Public Hearing Budget/Special mtg of Electors/Town Board Meeting",
             "url": url_of("me_01m78f4t8j1qe1t0pe443n49tt"), "source": "town_weston"},
            {"date": "2026-12-15", "time": "6:30 PM", "name": "Town Board Meeting",
             "url": "https://townofwestonwi.com/agendas-minutes", "source": "town_weston"},
        ])

    def test_a_start_time_is_used_when_heygov_has_one(self):
        meetings = copy.deepcopy(MEETINGS)
        meetings[0]["starts_at_local"] = "2026-11-17T18:30:00.000Z"
        rows = _run(date(2026, 11, 13), _HeyGov(meetings=meetings), heygov.fetch_upcoming, CFG, "town_weston", 45)
        self.assertEqual([(r["date"], r["time"]) for r in rows], [("2026-11-17", "6:30 PM")])

    def test_a_cancelled_meeting_is_not_listed_and_not_projected_back(self):
        meetings = copy.deepcopy(MEETINGS)
        meetings[0]["status"] = "canceled"
        rows = _run(date(2026, 11, 13), _HeyGov(meetings=meetings), heygov.fetch_upcoming,
                    {**CFG, "fallback_rules": [BOARD_RULE]}, "town_weston", 45)
        self.assertEqual([r["date"] for r in rows], ["2026-12-15"])

    def test_nothing_posted_is_rules_only_and_one_request(self):
        site = _HeyGov()
        rows = _run(date(2026, 11, 20), site, heygov.fetch_upcoming, {**CFG, "fallback_rules": [BOARD_RULE]}, "town_weston", 45)
        self.assertEqual([(r["date"], r["name"]) for r in rows], [("2026-12-15", "Town Board Meeting")])
        self.assertEqual(len(site.calls), 1, "the categories aren't asked for when there is no meeting to name")


class DocumentTest(unittest.TestCase):
    def _fetch(self, fn, pid, pdf_text="TOWN OF WESTON BOARD AGENDA " * 10):
        site = _HeyGov()
        with mock.patch("requests.get", side_effect=site), \
                mock.patch("engine.adapters.townweb.fetch_pdf_text", return_value=pdf_text) as pdf:
            return fn(CFG, url_of(pid)), pdf, site

    def test_a_pdf_agenda_is_read_from_the_file(self):
        text, pdf, site = self._fetch(heygov.fetch_agenda_text, OCT_BOARD["pid"])
        self.assertEqual(text, "TOWN OF WESTON BOARD AGENDA " * 10)
        pdf.assert_called_once_with("https://files.heygov.com/townofwestonwi.com/meetings/me_01m4v446q075cqmgf26xhnhhfw/"
                                    "oct-20-2026-town-board-agenda.pdf")
        self.assertEqual(site.calls, [(f"https://api.heygov.com/townofwestonwi.com/meetings/{OCT_BOARD['pid']}",
                                       {"expand": "agenda_items"})])

    def test_a_doc_agenda_falls_to_the_items_heygov_parsed_from_it(self):
        self.assertTrue(NOV_BOARD["agenda_file_path"].endswith(".doc"))
        text, pdf, _ = self._fetch(heygov.fetch_agenda_text, NOV_BOARD["pid"])
        pdf.assert_not_called()
        lines = text.splitlines()
        self.assertEqual(lines[0], "1. Call To Order")
        self.assertIn("6. Unfinished Business", lines)
        self.assertIn("  6.4 Budget Work/Adopt 2027 General Account Budget", lines, "sub-items sit under their parent, in order")
        self.assertEqual(lines[-1], "11. Adjourn")
        self.assertEqual(len(lines), len(NOV_BOARD["agenda_items"]))

    def test_no_public_agenda_is_nothing_to_read(self):
        hidden = {**OCT_BOARD, "agenda_status": "draft"}
        site = _HeyGov()
        site.single[OCT_BOARD["pid"]] = hidden
        with mock.patch("requests.get", side_effect=site), mock.patch("engine.adapters.townweb.fetch_pdf_text") as pdf:
            self.assertIsNone(heygov.fetch_agenda_text(CFG, url_of(OCT_BOARD["pid"])))
        pdf.assert_not_called()

    def test_docx_minutes_are_read_with_their_motions_and_vote_tables(self):
        text, pdf, site = self._fetch(heygov.fetch_minutes_text, SEP_PLAN["pid"])
        pdf.assert_not_called()
        self.assertTrue(site.calls[-1][0].endswith("/pc-town-of-weston-minutes-09152026.docx"))
        self.assertTrue(text.startswith("Town of Weston, Wisconsin\nOFFICIAL PROCEEDINGS OF THE TOWN PLAN COMMISSION MEETING"))
        self.assertIn("Motion by Hull, second by Conrad: to recommend approval to the Town Board", text)
        self.assertIn("Yes Vote: 5\tNo Votes: 0\tAbstain: 0\tNot Voting: 0\tResult: PASS", text, "a table row is one line")
        self.assertIn("Dave Phelps\tYES", text)
        self.assertNotIn("<w:", text)

    def test_doc_minutes_cannot_be_read(self):
        self.assertTrue(OCT_BOARD["meeting_minutes_file_path"].endswith(".doc"))
        text, pdf, site = self._fetch(heygov.fetch_minutes_text, OCT_BOARD["pid"])
        self.assertIsNone(text)
        pdf.assert_not_called()
        self.assertEqual(len(site.calls), 1, "the .doc itself is not downloaded")

    def test_no_minutes_until_they_are_public(self):
        self.assertEqual(NOV_BOARD["minutes_status"], "missing")
        text, pdf, site = self._fetch(heygov.fetch_minutes_text, NOV_BOARD["pid"])
        self.assertIsNone(text)
        self.assertEqual(len(site.calls), 1)

    def test_a_portal_link_or_a_removed_meeting_has_nothing_to_read(self):
        site = _HeyGov()
        with mock.patch("requests.get", side_effect=site):
            self.assertIsNone(heygov.fetch_agenda_text(CFG, CFG["portal"]))
            self.assertEqual(site.calls, [], "a rule-projected meeting's link costs no request")
            self.assertIsNone(heygov.fetch_agenda_text(CFG, url_of("me_01zzzzzzzzzzzzzzzzzzzzzzzz")))
            self.assertIsNone(heygov.fetch_minutes_text(CFG, url_of("me_01zzzzzzzzzzzzzzzzzzzzzzzz")))

    def test_heygov_not_answering_raises_rather_than_reading_as_no_minutes(self):
        with mock.patch("requests.get", return_value=_Response(status_code=503)):
            with self.assertRaises(requests.HTTPError):
                heygov.fetch_minutes_text(CFG, url_of(OCT_BOARD["pid"]))


class FailureTest(unittest.TestCase):
    def test_structure_drift_raises(self):
        no_local_date = copy.deepcopy(MEETINGS)
        del no_local_date[0]["starts_at_local"]
        unknown_category = [{"id": 9999, "name": "Something Else"}]
        for site in (_HeyGov(meetings={"error": "gone"}), _HeyGov(meetings=[]), _HeyGov(meetings=no_local_date),
                     _HeyGov(categories={"error": "gone"}), _HeyGov(categories=unknown_category)):
            with self.assertRaises(AdapterStructureError):
                _run(date(2026, 11, 20), site, heygov.list_recent, CFG, date(2026, 9, 1))
        with self.assertRaises(AdapterStructureError):
            _run(date(2026, 11, 13), _HeyGov(meetings=[]), heygov.fetch_upcoming, CFG, "town_weston", 45)

    def test_an_outage_raises(self):
        def down(*args, **kwargs):
            raise requests.Timeout("read timed out")
        for answer in ({"side_effect": down}, {"return_value": _Response(status_code=403)}, {"return_value": _Response(None)}):
            with mock.patch("requests.get", **answer), mock.patch("engine.net.time.sleep"):
                with self.assertRaises(UpstreamUnavailableError):
                    heygov.list_recent(CFG, date(2026, 9, 1))
                with self.assertRaises(UpstreamUnavailableError):
                    heygov.fetch_upcoming(CFG, "town_weston", 45)


if __name__ == "__main__":
    unittest.main()
