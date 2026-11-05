"""Portal-first jurisdictions: a CivicClerk, Legistar or PrimeGov town with no video channel, whose meetings are
created from the portal's listing (list_recent) and published as cc_/lg_/pg_ entries. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from engine import publish
from engine.adapters import civicclerk, legistar, primegov
from engine.config import InstanceConfig


class _Response:
    def __init__(self, status_code=200, data=None):
        self.status_code, self._data = status_code, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(self.status_code)


TODAY = date.today().isoformat()
SINCE = date(2026, 10, 1)


class ListRecentTest(unittest.TestCase):
    def test_civicclerk_lists_past_events_with_agendas(self):
        events = {"value": [
            {"id": 44, "eventName": "City Council", "eventDate": "2026-10-20T19:00:00Z", "hasAgenda": True},
            {"id": 45, "eventName": "Planning Commission", "eventDate": "2026-10-22T18:30:00Z", "hasAgenda": False},
            {"id": 46, "eventName": "City Council - CANCELLED", "eventDate": "2026-10-27T19:00:00Z", "hasAgenda": True},
        ]}
        cfg = {"api_base": "https://louisvilleco.api.civicclerk.com/v1", "portal": "https://louisvilleco.portal.civicclerk.com/"}
        with mock.patch("engine.adapters.civicclerk.requests.get", return_value=_Response(data=events)) as get:
            rows = civicclerk.list_recent(cfg, SINCE)
        self.assertIn(f"eventDate%20ge%20{SINCE}", get.call_args.args[0])
        self.assertIn(f"eventDate%20le%20{TODAY}", get.call_args.args[0])
        self.assertEqual(rows, [{"id": "44", "name": "City Council", "date": "2026-10-20",
                                 "url": "https://louisvilleco.portal.civicclerk.com/event/44/files"}])

    def test_legistar_lists_past_meetings_with_agendas(self):
        events = [
            {"EventId": 8108, "EventBodyName": "City Council ", "EventDate": "2026-10-27T00:00:00", "EventAgendaFile": "https://x/a.pdf",
             "EventInSiteURL": "https://sanjose.legistar.com/MeetingDetail.aspx?LEGID=8108"},
            {"EventId": 8109, "EventBodyName": "Rules Committee", "EventDate": "2026-10-28T00:00:00", "EventAgendaFile": None,
             "EventInSiteURL": "https://sanjose.legistar.com/MeetingDetail.aspx?LEGID=8109"},
            {"EventId": 8110, "EventBodyName": "City Council", "EventDate": "2026-10-29T00:00:00", "EventAgendaFile": "https://x/b.pdf",
             "EventInSiteURL": "https://sanjose.legistar.com/MeetingDetail.aspx?LEGID=8110", "EventComment": "CANCELLED"},
        ]
        with mock.patch("engine.adapters.legistar.requests.get", return_value=_Response(data=events)):
            rows = legistar.list_recent({"client": "sanjose"}, SINCE)
        self.assertEqual(rows, [{"id": "8108", "name": "City Council", "date": "2026-10-27",
                                 "url": "https://sanjose.legistar.com/MeetingDetail.aspx?LEGID=8108"}])

    def test_legistar_failed_request_raises(self):
        with mock.patch("engine.adapters.legistar.requests.get", return_value=_Response(status_code=500)):
            with self.assertRaises(RuntimeError):
                legistar.list_recent({"client": "sanjose"}, SINCE)

    def test_primegov_lists_archived_and_todays_meetings_once(self):
        agenda = [{"id": 8847, "compileOutputType": 3, "templateName": "City Council Agenda"}]
        archived = [
            {"id": 2726, "title": "City Council Meeting", "dateTime": "2026-10-19T18:30:00", "documentList": agenda},
            {"id": 2727, "title": "City Council Meeting - Versión en Español", "dateTime": "2026-10-19T18:30:00", "documentList": agenda},
            {"id": 2700, "title": "City Council Meeting", "dateTime": "2026-09-21T18:30:00", "documentList": agenda},
            {"id": 2728, "title": "Planning Board", "dateTime": "2026-10-26T18:00:00", "documentList": []},
        ]
        upcoming = [{"id": 2726, "title": "City Council Meeting", "dateTime": "2026-10-19T18:30:00", "documentList": agenda}]
        responses = {"ListArchivedMeetings?year=2026": archived, "ListUpcomingMeetings": upcoming}
        get = lambda url, **kw: _Response(data=responses[url.rsplit("/", 1)[-1]])
        with mock.patch("engine.adapters.primegov.requests.get", side_effect=get):
            rows = primegov.list_recent({"client": "cityofoviedo"}, SINCE)
        self.assertEqual([r["id"] for r in rows], ["2726"], "once, without the Spanish copy, the older meeting or the one with no agenda")
        self.assertEqual(rows[0]["url"], "https://cityofoviedo.primegov.com/Portal/Meeting?compiledMeetingDocumentFileId=8847")


class PublishTest(unittest.TestCase):
    def setUp(self):
        raw = {"instance": {"name": "Louisville Meetings", "newsroom": "Boulder Reporting Lab", "newsroom_url": "https://boulderreportinglab.org",
                            "region": "Louisville, Colorado", "timezone": "America/Denver", "max_meetings": 50},
               "theme": {}, "jurisdictions": [{"key": "louisville", "name": "Louisville", "short": "CITY", "accent": "#123456", "video": None,
                                               "agendas": {"adapter": "civicclerk", "api_base": "https://louisvilleco.api.civicclerk.com/v1",
                                                           "portal": "https://louisvilleco.portal.civicclerk.com"}}]}
        self.cfg = InstanceConfig(raw, Path("instance.json"))

    def test_portal_ids_are_synthetic(self):
        for vid in ("cc_44", "lg_8108", "pg_2726", "bb_748411"):
            self.assertTrue(publish._is_synthetic(vid), vid)
        self.assertFalse(publish._is_synthetic("dQw4w9WgXcQ"))
        self.assertFalse(publish._is_synthetic("ab_12345678"))

    def test_portal_meeting_links_to_its_portal_page_and_votes_end_agenda_only(self):
        info = {"title": "City Council - 2026-10-20", "source": "louisville", "doc_url": "https://louisvilleco.portal.civicclerk.com/event/44/files",
                "meeting_date": "20261020"}
        votes = {"items": [{"number": "1", "name": "Budget", "desc": "", "votes": [{"motion": "Approve", "passed": True}], "docs": [], "children": []}]}
        m = publish.build_meeting(self.cfg, "cc_44", info, {"overview": "x", "_source": "agenda_with_votes", "agenda": [{"time": "N/A", "item": "Budget"}]}, votes)
        self.assertEqual(m["url"], info["doc_url"])
        self.assertEqual(m["date"], "October 20, 2026")
        self.assertEqual(m["title"], "City Council")
        self.assertFalse(m["isAgendaOnly"], "a summary from the agenda and the recorded votes reports outcomes")
        self.assertEqual(len(m["civicItems"]), 1)
        before = publish.build_meeting(self.cfg, "cc_44", info, {"overview": "x", "_source": "agenda", "agenda": []}, None)
        self.assertTrue(before["isAgendaOnly"])


if __name__ == "__main__":
    unittest.main()
