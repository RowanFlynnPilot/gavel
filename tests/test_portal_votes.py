"""Portal vote records for recorded meetings: finding the portal event behind
a recording and saving each member's vote beside the summary. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from engine import pages, portal_votes
from engine.adapters import civicclerk
from engine.config import InstanceConfig
from engine.errors import UpstreamUnavailableError
from helpers import make_config, raw_instance

TODAY = date(2026, 11, 20)
VOTED = {"event_id": 3660, "agenda_id": 9, "items": [
    {"number": "1", "name": "Lead service lines", "desc": "", "docs": [], "children": [],
     "votes": [{"motion": "Adopt", "passed": False, "initiator": "A", "seconder": "B",
                "yes": ["A", "B"], "no": ["C", "D", "E"], "abstain": []}]}]}
NO_VOTES = {"event_id": 3660, "agenda_id": 9, "items": [
    {"number": "1", "name": "Lead service lines", "desc": "", "docs": [], "children": [], "votes": []}]}


def _cfg():
    raw = raw_instance()
    city = raw["jurisdictions"][3]
    city.update(name="City of Wausau",
                video={"adapter": "youtube", "channel_url": "https://youtube.example/@city"})
    return InstanceConfig(raw, Path("instance.json"))


def _event(eid, name, day="2026-11-10"):
    return {"id": str(eid), "name": name, "date": day,
            "url": f"https://oak.portal.example/event/{eid}/files"}


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.jur = _cfg().jurisdiction("city")
        self.events = [_event(3614, "Board of Public Works Meeting"),
                       _event(3655, "Joint Finance Committee and the Human Resources Committee Meeting"),
                       _event(3660, "Common Council Meeting"),
                       _event(3661, "Public Health & Safety Committee Meeting")]

    def match(self, title, events=None):
        hit = portal_votes.match_event(title, self.events if events is None else events, self.jur)
        return hit and hit["id"]

    def test_a_recording_is_matched_by_the_words_that_name_its_body(self):
        self.assertEqual(self.match("Wausau City Council Meeting"), "3660")
        self.assertEqual(self.match("Wausau Board of Public Works Meeting"), "3614")
        self.assertEqual(self.match("Wausau Joint Finance and Human Resources Meeting"), "3655")
        # A committee of the council, titled with "City Council" in front.
        self.assertEqual(self.match("Wausau City Council Public Health & Safety Committee Meeting"), "3661")

    def test_no_match_is_better_than_a_guess(self):
        self.assertIsNone(self.match("Wausau Waterworks Works Meeting"))          # the title's own typo
        self.assertIsNone(self.match("Wausau Plan Commission Meeting"))           # nothing that day
        self.assertIsNone(self.match("Wausau Meeting"))                           # names no body
        two = self.events + [_event(3662, "Special Common Council Meeting")]
        self.assertIsNone(self.match("Wausau City Council Meeting", two), "two council events that day")
        # Finance alone is not the joint meeting.
        self.assertIsNone(self.match("Wausau Finance Committee Meeting"))


class AttachTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = _cfg()
        self.state = {"processed": {}}
        self.adapter = mock.Mock()
        self.adapter.events_on.return_value = [_event(3660, "Common Council Meeting")]
        self.adapter.fetch_meeting_data.return_value = VOTED
        for target, value in ((mock.patch.object(portal_votes, "get_adapter", return_value=self.adapter), None),
                              (mock.patch.object(portal_votes, "save_state"), None)):
            target.start()
            self.addCleanup(target.stop)

    def recording(self, vid="vid00000001", day="20261110", title="Wausau City Council Meeting", **info):
        self.state["processed"][vid] = {"title": title, "source": "city", "meeting_date": day,
                                        "summary_file": str(self.dir / f"{vid}.md"), **info}
        return self.dir / f"{vid}_votes.json"

    def attach(self, today=TODAY):
        return portal_votes.attach(self.cfg, "city", self.state, today=today)

    def test_votes_recorded_in_the_portal_are_saved_beside_the_summary(self):
        sidecar = self.recording()
        self.assertEqual(self.attach(), ["vid00000001"])
        self.adapter.events_on.assert_called_once_with(self.cfg.jurisdiction("city").agendas, date(2026, 11, 10))
        self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8"))["items"][0]["votes"][0]["no"],
                         ["C", "D", "E"])
        info = self.state["processed"]["vid00000001"]
        self.assertEqual(info["doc_url"], "https://oak.portal.example/event/3660/files")
        # Done: the next run has nothing to look up.
        self.adapter.reset_mock()
        self.assertEqual(self.attach(today=date(2026, 11, 21)), [])
        self.adapter.events_on.assert_not_called()

    def test_a_recording_that_carries_its_portal_link_uses_it(self):
        self.recording(doc_url="https://oak.portal.example/event/77/overview")
        self.assertEqual(self.attach(), ["vid00000001"])
        self.adapter.events_on.assert_not_called()
        self.adapter.fetch_meeting_data.assert_called_once_with(
            self.cfg.jurisdiction("city").agendas, "https://oak.portal.example/event/77/overview")

    def test_no_votes_yet_is_looked_up_again_tomorrow_not_this_afternoon(self):
        sidecar = self.recording()
        self.adapter.fetch_meeting_data.return_value = NO_VOTES
        self.assertEqual(self.attach(), [])
        self.assertFalse(sidecar.exists(), "an agenda with no votes is not saved over the transcript's votes")
        self.attach()
        self.assertEqual(self.adapter.fetch_meeting_data.call_count, 1)
        self.adapter.fetch_meeting_data.return_value = VOTED
        self.assertEqual(self.attach(today=date(2026, 11, 21)), ["vid00000001"])

    def test_an_outage_is_not_an_answer(self):
        self.recording()
        self.adapter.events_on.side_effect = UpstreamUnavailableError("no answer")
        with self.assertLogs("engine.portal_votes", level="WARNING"):
            self.assertEqual(self.attach(), [])
        self.assertNotIn("portal_votes_checked", self.state["processed"]["vid00000001"])

    def test_what_is_left_alone(self):
        self.recording("vid00000002", day="20261001")                               # past the window
        self.recording("cc_44")                                                     # made from the portal itself
        self.recording("vid00000003", title="Wausau Plan Commission Meeting")       # no such event that day
        done = self.recording("vid00000004")
        done.write_text(json.dumps(VOTED), encoding="utf-8")                        # already has them
        self.assertEqual(self.attach(), [])
        self.adapter.fetch_meeting_data.assert_not_called()
        self.assertEqual(self.state["processed"]["vid00000003"]["portal_votes_checked"], TODAY.isoformat())

    def test_only_boards_with_a_channel_and_a_vote_keeping_portal_are_checked(self):
        plain = make_config()                    # its civicclerk city has no video channel
        self.state["processed"]["vid00000001"] = {"title": "Common Council", "source": "city",
                                                  "meeting_date": "20261110",
                                                  "summary_file": str(self.dir / "x.md")}
        self.assertEqual(portal_votes.attach(plain, "city", self.state, today=TODAY), [])
        self.assertEqual(portal_votes.attach(plain, "county", self.state, today=TODAY), [])
        self.adapter.events_on.assert_not_called()


class CivicClerkTest(unittest.TestCase):
    CFG = {"api_base": "https://oak.api.example/v1", "portal": "https://oak.portal.example/"}

    def test_every_page_of_events_is_read(self):
        pages_ = {"first": {"value": [{"id": 1}, {"id": 2}], "@odata.nextLink": "second"},
                  "second": {"value": [{"id": 3}]}}
        seen = []

        def fetch(url):
            seen.append(url)
            return pages_[url]
        self.assertEqual([e["id"] for e in civicclerk._all_events("first", fetch)], [1, 2, 3])
        self.assertEqual(seen, ["first", "second"])

    def test_events_on_lists_one_day_without_cancelled_meetings(self):
        page = {"value": [
            {"id": 3660, "eventName": "Common Council Meeting", "eventDate": "2026-11-10T18:30:00Z"},
            {"id": 3657, "eventName": "CANCELED - Finance Committee", "eventDate": "2026-11-10T08:00:00Z"},
            {"id": 3644, "eventName": "Possible Quorum of the Common Council", "eventDate": "2026-11-10T11:00:00Z"}]}
        with mock.patch.object(civicclerk.net, "get_json", return_value=page) as get:
            events = civicclerk.events_on(self.CFG, date(2026, 11, 10))
        self.assertEqual(events, [{"id": "3660", "name": "Common Council Meeting", "date": "2026-11-10",
                                   "url": "https://oak.portal.example/event/3660/files"}])
        self.assertIn("eventDate%20ge%202026-11-10T00", get.call_args.args[0])
        self.assertIn("eventDate%20le%202026-11-10T23", get.call_args.args[0])


class PageTest(unittest.TestCase):
    def test_archive_page_lists_each_members_vote_from_the_portal(self):
        m = {"id": "vid00000001", "source": "city", "title": "City Council", "date": "November 10, 2026",
             "overview": "x", "isAgendaOnly": False, "votes": [], "civicItems": VOTED["items"]}
        page = pages.render_page(_cfg(), m, "https://pages.example/tracker", False)
        self.assertIn("Votes by member, from the official record", page)
        self.assertIn("Lead service lines</strong> — Adopt: Failed 2-3", page)
        self.assertIn("Yes: A, B · No: C, D, E", page)
        plain = pages.render_page(_cfg(), {**m, "civicItems": NO_VOTES["items"]},
                                  "https://pages.example/tracker", False)
        self.assertNotIn("Votes by member", plain)


if __name__ == "__main__":
    unittest.main()
