"""Town Web adapter against saved responses from the Village of Maine's site (tests/fixtures/townweb/, fetched
Nov. 20, 2026 and trimmed): the meeting repository for Sept. 8 - Nov. 18, 2026, the site calendar for the same
weeks, and the Oct. 22 board meeting's own record. No network.

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

from engine.adapters import townweb
from engine.errors import AdapterStructureError, UpstreamUnavailableError

FIXTURES = Path(__file__).parent / "fixtures" / "townweb"
CFG = {"adapter": "townweb", "base": "https://villageofmaine.org",
       "portal": "https://villageofmaine.org/agendas-minutes/"}
REPOSITORY = json.loads((FIXTURES / "repository.json").read_text(encoding="utf-8"))
EVENTS = json.loads((FIXTURES / "events.json").read_text(encoding="utf-8"))
RECORD_1890 = json.loads((FIXTURES / "record_1890.json").read_text(encoding="utf-8"))
BOARD_RULE = {"name": "Board of Trustees Meeting", "weekday": 0, "nth": 2, "time": "6:00 PM"}


def url_of(record_id) -> str:
    return f"https://villageofmaine.org/?post_type=twd_repository&p={record_id}"


class _Response:
    def __init__(self, data=None, status_code=200, headers=None):
        self._data, self.status_code, self.headers = data, status_code, headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("not JSON")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Site:
    """Stands in for requests.get: the repository list, the calendar, and single records."""

    def __init__(self, repository=None, events=None, records=None):
        self.repository = REPOSITORY if repository is None else repository
        self.events = EVENTS if events is None else events
        self.records = {1890: RECORD_1890} if records is None else records
        self.calls = []

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        if url.endswith("/wp-json/wp/v2/twd_repository"):
            return _Response(self.repository, headers={"X-WP-TotalPages": "1"})
        if url.endswith("/wp-json/tribe/events/v1/events"):
            return _Response(self.events)
        record = self.records.get(int(url.rsplit("/", 1)[1]))
        return _Response(record) if record else _Response({"code": "rest_post_invalid_id"}, status_code=404)


def _on(day: date, site: _Site):
    """Patches for "today is `day`" and "the site answers with `site`"."""
    class _Today(date):
        @classmethod
        def today(cls):
            return day
    return (mock.patch("engine.adapters.townweb.date", _Today), mock.patch("engine.adapters.rule_schedule.date", _Today),
            mock.patch("requests.get", side_effect=site))


def _run(day: date, site: _Site, fn, *args):
    a, b, c = _on(day, site)
    with a, b, c:
        return fn(*args)


class ListRecentTest(unittest.TestCase):
    def test_past_meetings_with_an_agenda_newest_first(self):
        site = _Site()
        rows = _run(date(2026, 11, 17), site, townweb.list_recent, CFG, date(2026, 9, 6))
        self.assertEqual([(r["id"], r["date"], r["name"]) for r in rows], [
            ("1909", "2026-11-16", "Board of Trustees Meeting"),
            ("1899", "2026-11-09", "Board of Trustees Meeting"),
            ("1894", "2026-10-26", "Finance Meeting"),
            ("1890", "2026-10-22", "Board of Trustees Meeting"),
            ("1866", "2026-09-28", "Board of Trustees Meeting"),
            ("1868", "2026-09-22", "Municipal Projects & Public Property"),
            ("1858", "2026-09-21", "Plan Commission"),
            ("1855", "2026-09-14", "Board of Trustees Meeting"),
            ("1852", "2026-09-08", "Municipal Projects & Public Property"),
        ])
        # 1913 is Nov. 18 (tomorrow); 1881 (Oct. 12) and 1878 (Oct. 5) were cancelled.
        self.assertEqual(rows[0]["url"], url_of(1909))
        self.assertEqual(set(rows[0]), {"id", "name", "date", "url"})

    def test_asks_the_site_for_the_window_in_two_requests(self):
        site = _Site()
        _run(date(2026, 11, 17), site, townweb.list_recent, CFG, date(2026, 9, 6))
        self.assertEqual([u for u, _ in site.calls], ["https://villageofmaine.org/wp-json/wp/v2/twd_repository",
                                                      "https://villageofmaine.org/wp-json/tribe/events/v1/events"])
        params = site.calls[0][1]
        self.assertEqual((params["filter[meta_query][0][value]"], params["filter[meta_query][0][compare]"]), ("2026-09-06", ">="))
        self.assertEqual((params["filter[meta_query][1][value]"], params["filter[meta_query][1][compare]"]), ("2026-11-17", "<="))
        self.assertEqual(params["_embed"], 1, "without _embed the agenda is a bare attachment id")
        self.assertEqual((site.calls[1][1]["start_date"], site.calls[1][1]["end_date"]), ("2026-09-06", "2026-11-17"))

    def test_a_meeting_cancelled_on_the_calendar_is_left_out_though_its_agenda_is_still_posted(self):
        finance = next(r for r in REPOSITORY if r["id"] == 1878)
        self.assertTrue(finance["agenda"], "the repository record itself doesn't say cancelled")
        self.assertNotIn("cancel", finance["title"]["rendered"].lower())
        rows = _run(date(2026, 11, 17), _Site(), townweb.list_recent, CFG, date(2026, 9, 6))
        self.assertNotIn("1878", [r["id"] for r in rows])

    def test_a_cancelled_agenda_file_is_enough_when_the_calendar_has_no_event(self):
        rows = _run(date(2026, 11, 17), _Site(events={"events": [], "total": 0, "total_pages": 0}),
                    townweb.list_recent, CFG, date(2026, 9, 6))
        self.assertNotIn("1878", [r["id"] for r in rows], '"...Finance Committee Agenda_cancelled"')
        self.assertIn("1890", [r["id"] for r in rows], "a meeting with no calendar event is still listed")

    def test_a_meeting_with_no_agenda_yet_waits(self):
        repository = copy.deepcopy(REPOSITORY)
        next(r for r in repository if r["id"] == 1909)["agenda"] = ""     # how the API shows "none" (see 1881)
        rows = _run(date(2026, 11, 17), _Site(repository=repository), townweb.list_recent, CFG, date(2026, 9, 6))
        self.assertNotIn("1909", [r["id"] for r in rows])
        self.assertEqual(len(rows), 8)

    def test_bodies_keeps_only_the_named_categories(self):
        cfg = {**CFG, "bodies": ["Board of Trustees", "Plan Commission"]}
        rows = _run(date(2026, 11, 17), _Site(), townweb.list_recent, cfg, date(2026, 9, 6))
        self.assertEqual([r["id"] for r in rows], ["1909", "1899", "1890", "1866", "1858", "1855"])

    def test_ids_are_the_record_ids_and_do_not_change(self):
        first = _run(date(2026, 11, 17), _Site(), townweb.list_recent, CFG, date(2026, 9, 6))
        later = _run(date(2026, 11, 30), _Site(), townweb.list_recent, CFG, date(2026, 10, 1))
        self.assertTrue(all(r["id"].isdigit() for r in first))
        self.assertEqual(len({r["id"] for r in first}), len(first))
        by_id = {r["id"]: r for r in first}
        for r in later:
            if r["id"] in by_id:
                self.assertEqual(r, by_id[r["id"]], "same meeting, same id and doc_url on a later run")
        self.assertEqual(townweb._record_id(first[0]["url"]), first[0]["id"])

    def test_the_date_filter_is_checked_here_too(self):
        # The fixture holds Sept. 8 - Nov. 18 whatever is asked for, as if the site ignored the filter.
        rows = _run(date(2026, 11, 17), _Site(), townweb.list_recent, CFG, date(2026, 10, 20))
        self.assertEqual([r["id"] for r in rows], ["1909", "1899", "1894", "1890"])


class NameTest(unittest.TestCase):
    def test_the_body_goes_in_front_only_when_the_title_does_not_name_it(self):
        self.assertEqual(townweb.meeting_name("Board of Trustees Meeting", "Board of Trustees"), "Board of Trustees Meeting")
        self.assertEqual(townweb.meeting_name("Finance and Personnel Committee Meeting", "Finance"),
                         "Finance and Personnel Committee Meeting")
        self.assertEqual(townweb.meeting_name("Municipal Projects & Public Property", "Municipal Projects and Public Property"),
                         "Municipal Projects & Public Property", '"&" and "and" are the same word')
        self.assertEqual(townweb.meeting_name("Public Hearing", "Plan Commission"), "Plan Commission: Public Hearing")
        self.assertEqual(townweb.meeting_name("", "Plan Commission"), "Plan Commission")
        self.assertEqual(townweb.meeting_name("Open Book", ""), "Open Book")


class UpcomingTest(unittest.TestCase):
    def test_posted_meetings_get_their_time_from_the_calendar(self):
        rows = _run(date(2026, 11, 5), _Site(), townweb.fetch_upcoming, CFG, "maine", 45)
        self.assertEqual(rows, [
            {"date": "2026-11-09", "time": "5:30 PM", "name": "Board of Trustees Meeting", "url": url_of(1899), "source": "maine"},
            {"date": "2026-11-16", "time": "6:00 PM", "name": "Board of Trustees Meeting", "url": url_of(1909), "source": "maine"},
            {"date": "2026-11-18", "time": "5:30 PM", "name": "Municipal Projects & Public Property", "url": url_of(1913), "source": "maine"},
        ])

    def test_fallback_rules_fill_the_months_not_posted_yet(self):
        cfg = {**CFG, "fallback_rules": [BOARD_RULE]}
        rows = _run(date(2026, 11, 5), _Site(), townweb.fetch_upcoming, cfg, "maine", 45)
        self.assertEqual([(r["date"], r["time"], r["name"]) for r in rows], [
            ("2026-11-09", "5:30 PM", "Board of Trustees Meeting"),       # posted, with its real time: not the rule's 6:00
            ("2026-11-16", "6:00 PM", "Board of Trustees Meeting"),
            ("2026-11-18", "5:30 PM", "Municipal Projects & Public Property"),
            ("2026-12-14", "6:00 PM", "Board of Trustees Meeting"),       # projected
        ])
        self.assertEqual(rows[-1]["url"], CFG["portal"])
        self.assertEqual(rows[-1]["source"], "maine")

    def test_a_cancelled_meeting_is_not_listed_and_not_projected_back(self):
        cfg = {**CFG, "fallback_rules": [BOARD_RULE]}
        rows = _run(date(2026, 10, 1), _Site(), townweb.fetch_upcoming, cfg, "maine", 45)
        self.assertEqual([(r["date"], r["name"]) for r in rows], [
            ("2026-10-22", "Board of Trustees Meeting"),
            ("2026-10-26", "Finance Meeting"),
            ("2026-11-09", "Board of Trustees Meeting"),
        ])  # not Oct. 5 (Finance, cancelled) and not Oct. 12 (the board's second Monday, cancelled)

    def test_nothing_posted_is_rules_only_and_one_request(self):
        site = _Site(repository=[])
        rows = _run(date(2026, 11, 20), site, townweb.fetch_upcoming, {**CFG, "fallback_rules": [BOARD_RULE]}, "maine", 45)
        self.assertEqual([(r["date"], r["name"]) for r in rows], [("2026-12-14", "Board of Trustees Meeting")])
        self.assertEqual(len(site.calls), 1, "no calendar request when the repository has no meetings in the window")


class DocumentTest(unittest.TestCase):
    def _fetch(self, fn, doc_url, site=None, pdf_text="Village of Maine " * 20):
        site = site or _Site()
        with mock.patch("requests.get", side_effect=site), \
                mock.patch("engine.adapters.townweb.fetch_pdf_text", return_value=pdf_text) as pdf:
            return fn(CFG, doc_url), pdf, site

    def test_agenda_text_is_read_from_the_record_s_agenda_file(self):
        text, pdf, site = self._fetch(townweb.fetch_agenda_text, url_of(1890))
        self.assertEqual(text, "Village of Maine " * 20)
        pdf.assert_called_once_with("https://villageofmaine.org/wp-content/uploads/2026/10/2026-10-22-Village-Board-Agenda.pdf")
        self.assertEqual(site.calls, [("https://villageofmaine.org/wp-json/wp/v2/twd_repository/1890", {"_embed": 1})])

    def test_minutes_text_once_minutes_are_posted(self):
        text, pdf, _ = self._fetch(townweb.fetch_minutes_text, url_of(1890), pdf_text="Motion by Reynolds/Seidler ... Carried 4/0 " * 5)
        self.assertIn("Carried 4/0", text)
        pdf.assert_called_once_with("https://villageofmaine.org/wp-content/uploads/2026/11/2026-10-22-Village-Board-minutes.pdf")

    def test_no_minutes_until_they_are_posted(self):
        waiting = {**RECORD_1890, "id": 1899, "meeting_minutes": ""}
        text, pdf, _ = self._fetch(townweb.fetch_minutes_text, url_of(1899), site=_Site(records={1899: waiting}))
        self.assertIsNone(text)
        pdf.assert_not_called()

    def test_a_portal_link_or_a_removed_record_has_nothing_to_read(self):
        text, pdf, site = self._fetch(townweb.fetch_agenda_text, CFG["portal"])
        self.assertIsNone(text)
        self.assertEqual(site.calls, [], "a rule-projected meeting's link costs no request")
        for fn in (townweb.fetch_agenda_text, townweb.fetch_minutes_text):
            text, pdf, _ = self._fetch(fn, url_of(4040))
            self.assertIsNone(text)
            pdf.assert_not_called()

    def test_a_site_that_does_not_answer_raises_rather_than_reading_as_no_minutes(self):
        with mock.patch("requests.get", return_value=_Response(status_code=503)):
            with self.assertRaises(requests.HTTPError):
                townweb.fetch_minutes_text(CFG, url_of(1890))

    def test_a_legacy_doc_cannot_be_read(self):
        with mock.patch("requests.get") as get:
            self.assertIsNone(townweb.document_text("https://villageofmaine.org/wp-content/uploads/2026/11/minutes.doc"))
        get.assert_not_called()


class FailureTest(unittest.TestCase):
    def test_structure_drift_raises(self):
        not_a_list = _Site(repository={"code": "rest_no_route", "message": "No route was found"})
        no_dates = _Site(repository=[{"id": 1, "title": {"rendered": "Board"}}])
        bare_ids = copy.deepcopy(REPOSITORY)
        bare_ids[0]["agenda"] = "1915"          # what the API returns without the plugin's _embed expansion
        calendar_gone = _Site(events={"code": "rest_no_route"})
        for site in (not_a_list, no_dates, _Site(repository=bare_ids), calendar_gone):
            with self.assertRaises(AdapterStructureError):
                _run(date(2026, 11, 20), site, townweb.list_recent, CFG, date(2026, 9, 6))
        with self.assertRaises(AdapterStructureError):
            _run(date(2026, 11, 5), not_a_list, townweb.fetch_upcoming, CFG, "maine", 45)

    def test_an_outage_raises(self):
        def down(*args, **kwargs):
            raise requests.ConnectionError("connection refused")
        blocked = _Response(status_code=403)            # what a Cloudflare block looks like to the adapter
        for answer in ({"side_effect": down}, {"return_value": blocked}, {"return_value": _Response(None)}):
            with mock.patch("requests.get", **answer), mock.patch("engine.net.time.sleep"):
                with self.assertRaises(UpstreamUnavailableError):
                    townweb.list_recent(CFG, date(2026, 9, 6))
                with self.assertRaises(UpstreamUnavailableError):
                    townweb.fetch_upcoming(CFG, "maine", 45)


if __name__ == "__main__":
    unittest.main()
