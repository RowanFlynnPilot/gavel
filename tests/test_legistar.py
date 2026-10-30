"""Legistar adapter against saved API responses (tests/fixtures/legistar/, trimmed
from real Seattle, Denver and San Jose meetings in October 2026). No network.
The event-items fixtures serve both fetch_meeting_data and fetch_agenda_text.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from engine.adapters import legistar
from engine.pipeline import _has_votes

FIXTURES = Path(__file__).parent / "fixtures" / "legistar"


class _Response:
    def __init__(self, data, status_code=200):
        self._data, self.status_code = data, status_code

    def json(self):
        return self._data


def _fake_get(url, headers=None, timeout=None):
    """Serve webapi.legistar.com/v1/<client>/... from the fixtures."""
    client, path = re.match(r"https://webapi\.legistar\.com/v1/([^/]+)/(.*)", url).groups()
    m = re.match(r"events/(\d+)/eventitems", path)
    if m:
        name = f"{client}_{m.group(1)}_eventitems.json"
    elif m := re.match(r"eventitems/(\d+)/votes", path):
        name = f"{client}_votes_{m.group(1)}.json"
    elif m := re.match(r"events/(\d+)$", path):
        name = f"{client}_event_{m.group(1)}.json"
    elif path.startswith("events?"):
        name = f"{client}_events_oct.json"
    else:
        raise AssertionError(f"unexpected Legistar call: {url}")
    f = FIXTURES / name
    return _Response(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else _Response(None, 404)


@mock.patch("engine.adapters.legistar.requests.get", side_effect=_fake_get)
class LegistarTest(unittest.TestCase):
    def test_seattle_roll_calls_become_votes(self, _get):
        data = legistar.fetch_meeting_data(
            {"client": "seattle"}, "https://seattle.legistar.com/MeetingDetail.aspx?LEGID=6911&GID=393&G=X")
        self.assertEqual(data["event_id"], 6911)
        self.assertTrue(all(i["name"] for i in data["items"]), "blank separator rows are dropped")
        bills = [i for i in data["items"] if i["votes"]]
        self.assertEqual([i["number"] for i in bills], ["1", "2"])
        vote = bills[0]["votes"][0]
        self.assertEqual((vote["motion"], vote["passed"]), ("passed", True))
        self.assertEqual((len(vote["yes"]), vote["no"], vote["abstain"]), (9, [], []))
        self.assertEqual(bills[0]["desc"], "CB 121297 (Council Bill (CB))")
        self.assertTrue(any(d["type"] == "docx" for d in bills[0]["docs"]))
        self.assertTrue(_has_votes(data["items"]))

    def test_denver_mover_seconder_and_absent_member(self, _get):
        data = legistar.fetch_meeting_data({"client": "denver"}, "https://denver.legistar.com/MeetingDetail.aspx?LEGID=13014")
        adopted, approved = data["items"]
        vote = adopted["votes"][0]
        self.assertEqual((vote["initiator"], vote["seconder"]), ("Paul Kashmann", "Chris Hinds"))
        self.assertEqual((len(vote["yes"]), len(vote["no"])), (11, 0), "the absent member is in no list")
        self.assertEqual(approved["votes"], [], "an action with no pass/fail flag isn't a vote")

    def test_san_jose_agenda_before_votes(self, _get):
        data = legistar.fetch_meeting_data({"client": "sanjose"}, "https://sanjose.legistar.com/MeetingDetail.aspx?LEGID=8108&GID=317")
        self.assertTrue(data["items"])
        self.assertFalse(_has_votes(data["items"]), "no votes recorded yet, so the agenda prompt summarizes it")

    def test_agenda_text_is_the_item_list(self, _get):
        text = legistar.fetch_agenda_text({"client": "seattle"}, "https://seattle.legistar.com/MeetingDetail.aspx?LEGID=6911")
        lines = text.splitlines()
        self.assertIn("A. CALL TO ORDER", lines)
        self.assertTrue(any(line.startswith("1. An ordinance relating to traffic management") and line.endswith("[CB 121297]")
                            for line in lines))
        self.assertTrue(all(line.strip() for line in lines), "blank separator rows are dropped")

    def test_upcoming_skips_cancelled_meetings(self, _get):
        events = json.loads((FIXTURES / "seattle_events_oct.json").read_text(encoding="utf-8"))
        rows = legistar.fetch_upcoming({"client": "seattle"}, "seattle", 30)
        self.assertEqual(len(rows), len([e for e in events if not legistar._cancelled(e)]))
        self.assertLess(len(rows), len(events))
        self.assertEqual(set(rows[0]), {"date", "time", "name", "url", "source"})
        self.assertTrue(all(r["url"].startswith("https://seattle.legistar.com/") for r in rows))

    def test_not_a_legistar_url(self, get):
        self.assertIsNone(legistar.fetch_meeting_data({"client": "sanjose"}, "https://example.com/agenda.pdf"))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
