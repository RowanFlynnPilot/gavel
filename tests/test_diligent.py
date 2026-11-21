"""Diligent Community adapter against saved responses from the Mosinee School District portal
(tests/fixtures/diligent/, fetched Nov. 20, 2026): the meetings listing for Sept. 6 - Nov. 20, the documents of
three meetings (a committee with HTML minutes, the annual meeting with none, the regular board meeting with
PDF minutes; that one's agenda HTML is blanked to keep it small), and the first seven pages of text of the
regular meeting's 77-page minutes PDF, with a resident's name and address and a student's name taken out.
No network.

The portal had no cancelled, unpublished or external-calendar meeting and no adopted-plus-published,
closed-session or release-delayed document to save, so those rows are built here from real ones.

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

from engine.adapters import diligent
from engine.config import InstanceConfig
from engine.errors import AdapterStructureError, UpstreamUnavailableError
from helpers import FakeResponse, raw_instance

FIXTURES = Path(__file__).parent / "fixtures" / "diligent"
BASE = "https://mosineeschools.community.highbond.com"
CFG = {"adapter": "diligent", "base": BASE, "portal": f"{BASE}/Portal/MeetingTypeList.aspx"}
PAGE = f"{BASE}/Portal/MeetingInformation.aspx?Id="
SINCE = date(2026, 9, 6)


def _json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _day(year: int, month: int, day: int):
    """A date class whose today() is the given day, for the modules that ask."""
    class _Day(date):
        @classmethod
        def today(cls):
            return cls(year, month, day)
    return _Day


def _on(year: int, month: int, day: int):
    fake = _day(year, month, day)
    return mock.patch.multiple("engine.adapters.diligent", date=fake), \
        mock.patch("engine.adapters.rule_schedule.date", fake)


def _serving(data):
    return mock.patch("engine.net.requests.get", return_value=FakeResponse(data=data))


class ListRecentTest(unittest.TestCase):
    def _list(self, rows, cfg=CFG):
        a, b = _on(2026, 11, 20)
        with a, b, _serving(rows) as get:
            return diligent.list_recent(cfg, SINCE), get

    def test_lists_the_published_meetings_newest_first(self):
        rows, get = self._list(_json("meetings.json"))
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0],
                         f"{BASE}/Services/MeetingsService.svc/meetings?from=2026-09-05&to=2026-11-21&loadall=true",
                         "a day wider on each side than the days wanted")
        self.assertEqual(len(rows), 13)
        self.assertEqual(rows[0], {"id": "494", "name": "Regular School Board Meeting", "date": "2026-11-17",
                                   "url": PAGE + "494"})
        self.assertEqual([r["date"] for r in rows], sorted((r["date"] for r in rows), reverse=True))
        self.assertEqual({r["name"] for r in rows},
                         {"Regular School Board Meeting", "Annual Meeting", "Personnel Committee Meeting",
                          "Finance & Building Committee Meeting", "Instructional Programs Committee Meeting"})
        annual = next(r for r in rows if r["id"] == "482")
        self.assertEqual(annual["name"], "Annual Meeting", "the clerk's own name for a meeting, not its type")

    def test_cancelled_unpublished_and_outside_meetings_are_left_out(self):
        listing = _json("meetings.json")
        real = listing[0]
        cancelled = {**real, "Id": 9001, "Name": "Regular School Board Meeting - CANCELLED - Nov 10 2026",
                     "MeetingDate": "2026-11-10"}
        no_agenda_yet = {**real, "Id": 9002, "MeetingDate": "2026-11-12", "Published": False}
        other_calendar = {**real, "Id": 9003, "MeetingDate": "2026-11-13", "ExternalCalendar": True}
        before_window = {**real, "Id": 9004, "MeetingDate": "2026-08-18"}
        rows, _ = self._list(listing + [cancelled, no_agenda_yet, other_calendar, before_window])
        self.assertEqual(len(rows), 13)
        self.assertFalse({"9001", "9002", "9003", "9004"} & {r["id"] for r in rows})

    def test_bodies_keeps_only_the_named_meeting_types(self):
        rows, _ = self._list(_json("meetings.json"), {**CFG, "bodies": ["Regular School Board Meeting"]})
        self.assertEqual([r["id"] for r in rows], ["494", "490", "486", "482"],
                         "the annual meeting is a meeting of the same body")

    def test_ids_are_the_portals_own_and_stable(self):
        listing = _json("meetings.json")
        first, _ = self._list(listing)
        later = [{**listing[0], "Id": 501, "MeetingDate": "2026-11-19", "Name": "Special School Board Meeting - Nov 19 2026"}]
        second, _ = self._list(later + list(reversed(listing)))
        self.assertEqual({r["id"]: (r["date"], r["url"]) for r in first},
                         {r["id"]: (r["date"], r["url"]) for r in second if r["id"] != "501"})
        for r in first:
            self.assertTrue(r["id"].isdigit())
            self.assertEqual(diligent._meeting_id(r["url"]), int(r["id"]), "the doc_url alone identifies the meeting")
        self.assertEqual(len({r["id"] for r in first}), 13, "four bodies meeting the same evening don't collide")

    def test_an_empty_listing_is_no_meetings(self):
        rows, _ = self._list([])
        self.assertEqual(rows, [])

    def test_structure_drift_raises(self):
        for answer in ({"Message": "Endpoint not found."},
                       [{"MeetingId": 494, "Title": "Regular School Board Meeting", "Date": "2026-11-17"}]):
            a, b = _on(2026, 11, 20)
            with a, b, _serving(answer):
                with self.assertRaises(AdapterStructureError, msg=str(answer)):
                    diligent.list_recent(CFG, SINCE)
                with self.assertRaises(AdapterStructureError, msg=str(answer)):
                    diligent.fetch_upcoming(CFG, "mosinee_schools", 45)

    def test_an_outage_raises(self):
        for failure, tries in (({"side_effect": requests.ConnectionError("no route")}, 3),
                               ({"return_value": FakeResponse(status_code=503)}, 3),
                               ({"return_value": FakeResponse(text="<html>Maintenance</html>")}, 1)):     # not JSON
            with mock.patch("engine.net.requests.get", **failure) as get, mock.patch("engine.net.time.sleep"):
                with self.assertRaises(UpstreamUnavailableError):
                    diligent.list_recent(CFG, SINCE)
                self.assertEqual(get.call_count, tries)
                with self.assertRaises(UpstreamUnavailableError):
                    diligent.fetch_upcoming(CFG, "mosinee_schools", 45)


class AgendaTest(unittest.TestCase):
    def test_agenda_text_is_the_agenda_one_item_per_line(self):
        with _serving(_json("documents_487.json")) as get:
            text = diligent.fetch_agenda_text(CFG, PAGE + "487")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], f"{BASE}/Services/MeetingsService.svc/meetings/487/meetingDocuments")
        lines = text.splitlines()
        self.assertEqual(lines[0], "Finance and Building Committee Agenda")
        self.assertIn("Tuesday, October 27, 2026 at 5:30 p.m.", lines)
        self.assertIn("6. New Business", lines, "an item's number is on its line")
        self.assertIn("c. Discussion and Possible Recommendation: Draeger Trucking Contract", lines)
        self.assertIn("8. Adjournment", lines)
        self.assertNotIn("<", text)
        self.assertNotIn("font-family", text)

    def test_no_agenda_document_is_no_text(self):
        for data in ({"Documents": []}, _json("documents_494.json")):     # nothing posted; an agenda with no HTML
            with _serving(data):
                self.assertIsNone(diligent.fetch_agenda_text(CFG, PAGE + "494"))

    def test_an_agenda_held_for_later_release_is_not_read(self):
        data = _json("documents_487.json")
        data["Documents"][0]["PublicReleaseDelayDateString"] = "Oct 26 2026 4:00 PM"
        with _serving(data):
            self.assertIsNone(diligent.fetch_agenda_text(CFG, PAGE + "487"))

    def test_a_url_that_is_not_a_meeting_page_makes_no_request(self):
        with mock.patch("engine.net.requests.get") as get:
            for url in (CFG["portal"], f"{BASE}/portal/members.aspx?id=16", "https://example.com/agenda.pdf", ""):
                self.assertIsNone(diligent.fetch_agenda_text(CFG, url), url)
                self.assertIsNone(diligent.fetch_minutes_text(CFG, url), url)
        get.assert_not_called()

    def test_a_failed_documents_request_is_no_text_after_one_try(self):
        with mock.patch("engine.net.requests.get", side_effect=requests.ConnectionError("down")) as get:
            self.assertIsNone(diligent.fetch_agenda_text(CFG, PAGE + "487"))
            self.assertIsNone(diligent.fetch_minutes_text(CFG, PAGE + "487"))
        self.assertEqual(get.call_count, 2)


class MinutesTest(unittest.TestCase):
    def _pdf(self, text):
        return mock.patch("engine.adapters.diligent.fetch_pdf_text", return_value=text)

    def test_html_minutes_carry_the_motions_and_votes(self):
        with _serving(_json("documents_487.json")) as get, self._pdf(None) as pdf:
            text = diligent.fetch_minutes_text(CFG, PAGE + "487")
        get.assert_called_once()
        pdf.assert_not_called()
        self.assertTrue(text.startswith("Finance and Building Committee Minutes\nTuesday, October 27, 2026 at 5:30 PM"))
        self.assertIn("Motion by Gorman, seconded by Nest, to approve the agenda, as posted. "
                      "Voice vote revealed 2 yes. 0 no. Motion carried unanimously.", text)
        self.assertIn("c. Discussion and Possible Recommendation: Draeger Trucking Contract", text.splitlines())
        self.assertNotIn("<", text)

    def test_no_minutes_yet(self):
        with _serving(_json("documents_482.json")), self._pdf(None) as pdf:
            self.assertIsNone(diligent.fetch_minutes_text(CFG, PAGE + "482"))
        pdf.assert_not_called()

    def test_pdf_minutes_stop_before_the_attachments(self):
        packet = (FIXTURES / "minutes_pdf_text_494.txt").read_text(encoding="utf-8")
        with _serving(_json("documents_494.json")), self._pdf(packet) as pdf:
            text = diligent.fetch_minutes_text(CFG, PAGE + "494")
        pdf.assert_called_once_with(f"{BASE}/document/14346", timeout=60)
        self.assertTrue(text.startswith("Draft\nRegular Meeting Minutes\nTuesday, November 17, 2026 at 6:00 PM"))
        self.assertIn("Motion to approve Tech Usage Guide", text)
        self.assertIn("Moved by: Calandra Zebro; seconded by: David Bauer\nCarried 9-0", text)
        self.assertIn("18. Adjournment\nDraft\nMotion to adjourn at 8:53pm\nMoved by: Donna Kaiser; seconded by: Diane Gorman",
                      text, "the adjournment runs onto the next page")
        self.assertNotIn("October 27, 2026 at 6:00 PM", text, "the attached minutes of the meeting before are left out")
        self.assertNotIn("Carried 8-0 1 abstention", text, "and so are their votes")
        self.assertNotIn("Page 5 of 77", text)

    def test_pdf_minutes_end_on_the_adjournment_page_when_it_has_the_motion(self):
        packet = ("Minutes\n1. Call to Order\n" + "Discussion of the budget.\n" * 8 + "Page 1 of 9\n"
                  "7. Adjournment\nMotion to adjourn at 5:36 p.m.\nMotion carried.\nPage 2 of 9\n"
                  "Minutes of the meeting before\n4. Approval of Agenda\nCarried 2-1\n7. Adjournment\nPage 3 of 9\n")
        text = diligent.minutes_proper(packet)
        self.assertTrue(text.endswith("7. Adjournment\nMotion to adjourn at 5:36 p.m.\nMotion carried."))
        self.assertNotIn("Carried 2-1", text)

    def test_a_pdf_with_no_adjournment_item_is_not_read(self):
        packet = (FIXTURES / "minutes_pdf_text_494.txt").read_text(encoding="utf-8").replace("18. Adjournment\n", "")
        self.assertIsNone(diligent.minutes_proper(packet))
        self.assertIsNone(diligent.minutes_proper("16. Adjourn to Closed Session\nPage 1 of 4\nOther business\n"),
                          "going into closed session is not the end of the meeting")
        with _serving(_json("documents_494.json")), self._pdf(packet):
            self.assertIsNone(diligent.fetch_minutes_text(CFG, PAGE + "494"))

    def test_adopted_minutes_come_before_published_ones(self):
        data = _json("documents_494.json")
        published = next(d for d in data["Documents"] if d["DocumentType"] == 10)
        data["Documents"].append({**published, "Id": 14999, "DocumentType": 53})
        packet = (FIXTURES / "minutes_pdf_text_494.txt").read_text(encoding="utf-8")
        with _serving(data), self._pdf(packet) as pdf:
            self.assertTrue(diligent.fetch_minutes_text(CFG, PAGE + "494"))
        pdf.assert_called_once_with(f"{BASE}/document/14999", timeout=60)

    def test_adopted_minutes_only_waits_for_adoption(self):
        cfg = {**CFG, "adopted_minutes_only": True}
        for fixture, meeting in (("documents_494.json", "494"), ("documents_487.json", "487")):
            with _serving(_json(fixture)), self._pdf("x") as pdf:
                self.assertIsNone(diligent.fetch_minutes_text(cfg, PAGE + meeting))
            pdf.assert_not_called()

    def test_closed_session_and_delayed_minutes_are_not_read(self):
        data = _json("documents_487.json")
        closed = copy.deepcopy(data)
        for d in closed["Documents"]:
            if d["Type"] == 2:
                d["DocumentType"] = {9: 11, 10: 12}[d["DocumentType"]]     # publishedMinutesClosedHtml / ...ClosedPdf
        delayed = copy.deepcopy(data)
        for d in delayed["Documents"]:
            if d["Type"] == 2:
                d["PublicReleaseDelayDateString"] = "Dec 1 2026 8:00 AM"
        for answer in (closed, delayed):
            with _serving(answer), self._pdf("x") as pdf:
                self.assertIsNone(diligent.fetch_minutes_text(CFG, PAGE + "487"))
            pdf.assert_not_called()


class UpcomingTest(unittest.TestCase):
    RULES = [{"name": "Regular School Board Meeting", "weekday": 1, "nth": 3, "time": "6:00 PM"}]

    def _upcoming(self, rows, cfg, days_ahead=45):
        a, b = _on(2026, 11, 10)
        with a, b, _serving(rows) as get:
            return diligent.fetch_upcoming(cfg, "mosinee_schools", days_ahead), get

    def test_posted_meetings_in_date_and_time_order(self):
        rows, get = self._upcoming(_json("meetings.json"), CFG)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0],
                         f"{BASE}/Services/MeetingsService.svc/meetings?from=2026-11-09&to=2026-12-26&loadall=true")
        self.assertEqual([(r["date"], r["time"], r["name"]) for r in rows], [
            ("2026-11-17", "5:15 PM", "Instructional Programs Committee Meeting"),
            ("2026-11-17", "5:30 PM", "Finance & Building Committee Meeting"),
            ("2026-11-17", "5:30 PM", "Personnel Committee Meeting"),
            ("2026-11-17", "6:00 PM", "Regular School Board Meeting")])
        self.assertEqual(rows[3], {"date": "2026-11-17", "time": "6:00 PM", "name": "Regular School Board Meeting",
                                   "url": PAGE + "494", "source": "mosinee_schools"})

    def test_rules_fill_in_what_is_not_posted_yet(self):
        rows, _ = self._upcoming(_json("meetings.json"), {**CFG, "fallback_rules": self.RULES})
        regular = [r for r in rows if r["name"] == "Regular School Board Meeting"]
        self.assertEqual([(r["date"], r["url"]) for r in regular],
                         [("2026-11-17", PAGE + "494"), ("2026-12-15", CFG["portal"])],
                         "November's is the posted meeting, once; December's is projected")
        self.assertEqual(len(rows), 5)

    def test_nothing_posted_is_the_projection_alone(self):
        rows, _ = self._upcoming([], {**CFG, "fallback_rules": self.RULES})
        self.assertEqual(rows, [
            {"date": "2026-11-17", "time": "6:00 PM", "name": "Regular School Board Meeting", "url": CFG["portal"],
             "source": "mosinee_schools"},
            {"date": "2026-12-15", "time": "6:00 PM", "name": "Regular School Board Meeting", "url": CFG["portal"],
             "source": "mosinee_schools"}])
        rows, _ = self._upcoming([], CFG)
        self.assertEqual(rows, [])

    def test_cancelled_meetings_are_left_out_and_unpublished_ones_kept(self):
        listing = _json("meetings.json")
        cancelled = {**listing[0], "Id": 9001, "MeetingDate": "2026-12-01", "Name": "Special School Board Meeting - Cancelled"}
        unpublished = {**listing[0], "Id": 9002, "MeetingDate": "2026-12-15", "Published": False}
        rows, _ = self._upcoming(listing + [cancelled, unpublished], CFG)
        self.assertNotIn("2026-12-01", [r["date"] for r in rows])
        self.assertIn(("2026-12-15", PAGE + "9002"), [(r["date"], r["url"]) for r in rows],
                      "a scheduled meeting is upcoming before its agenda is out")


class JurisdictionFileTest(unittest.TestCase):
    def test_the_mosinee_jurisdiction_loads(self):
        root = Path(__file__).parent.parent
        raw = raw_instance()
        raw["jurisdictions"].append(json.loads((root / "docs" / "new-boards" / "mosinee_schools.json").read_text(encoding="utf-8")))
        jur = InstanceConfig(raw, Path("instance.json")).jurisdiction("mosinee_schools")
        self.assertEqual(jur.agendas_adapter, "diligent")
        self.assertIsNone(jur.video)
        self.assertEqual(len(jur.officials), 9)


if __name__ == "__main__":
    unittest.main()
