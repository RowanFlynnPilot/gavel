"""Upcoming feed: retries, last-known-good when an upstream doesn't answer, and
a rule projection dropped when a posted meeting covers it. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import requests

from engine import net, upcoming
from engine.adapters import boardbook, civicclerk, municode, rule_schedule
from engine.config import load_instance
from engine.errors import AdapterStructureError, UpstreamUnavailableError
from helpers import FakeResponse, make_config

TODAY = date.today()


def _day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


@mock.patch("engine.net.time.sleep")
class NetGetTest(unittest.TestCase):
    def test_succeeds_after_two_failures(self, sleep):
        answers = [requests.Timeout("slow"), FakeResponse(503), FakeResponse(200, text="ok")]
        with mock.patch("engine.net.requests.get", side_effect=answers) as get:
            r = net.get("https://portal.example/events", headers={"A": "b"})
        self.assertEqual(r.text, "ok")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args.kwargs, {"timeout": net.TIMEOUT_SECONDS, "headers": {"A": "b"}})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 8])

    def test_no_answer_after_three_attempts_is_a_named_error(self, sleep):
        with mock.patch("engine.net.requests.get", side_effect=requests.ConnectionError("down")) as get:
            with self.assertRaises(UpstreamUnavailableError) as ctx:
                net.get("https://portal.example/events")
        self.assertEqual(get.call_count, 3)
        self.assertIn("https://portal.example/events", str(ctx.exception))

    def test_a_body_that_is_not_json_is_no_answer(self, sleep):
        with mock.patch("engine.net.requests.get", return_value=FakeResponse(200, data=None)):
            with self.assertRaises(UpstreamUnavailableError):
                net.get_json("https://portal.example/events")


@mock.patch("engine.net.time.sleep")
class AdapterNoAnswerTest(unittest.TestCase):
    """An outage reaches the caller as UpstreamUnavailableError, never as []."""

    def test_civicclerk(self, _sleep):
        cfg = {"api_base": "https://oak.api.example/v1", "portal": "https://oak.portal.example"}
        with mock.patch("engine.net.requests.get", side_effect=requests.Timeout("slow")):
            with self.assertRaises(UpstreamUnavailableError):
                civicclerk.fetch_upcoming(cfg, "city", 45)

    def test_boardbook_does_not_fall_back_to_rules_alone(self, _sleep):
        cfg = {"base": "https://bb.example", "org_id": 7,
               "fallback_rules": [{"name": "Regular Meeting", "weekday": 0, "nth": 2, "time": "5:00 PM"}]}
        with mock.patch("engine.net.requests.get", return_value=FakeResponse(502)):
            with self.assertRaises(UpstreamUnavailableError):
                boardbook.fetch_upcoming(cfg, "schools", 60)

    def test_municode_first_page(self, _sleep):
        cfg = {"base": "https://elm.example", "blob_base": "https://blob.example"}
        with mock.patch("engine.net.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(UpstreamUnavailableError):
                municode.fetch_upcoming(cfg, "village", 60)

    def test_boardbook_page_without_agenda_rows_is_structure_drift(self, _sleep):
        with mock.patch("engine.net.requests.get", return_value=FakeResponse(200, text="<html>redesigned</html>")):
            with self.assertRaises(AdapterStructureError):
                boardbook.fetch_upcoming({"base": "https://bb.example", "org_id": 7}, "schools", 60)


class LastKnownGoodTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "upcoming.json"
        self.previous = {
            "city": [{"date": _day(-2), "time": "6:00 PM", "name": "Council (held)", "url": "", "source": "city"},
                     {"date": _day(0), "time": "6:00 PM", "name": "Council (today)", "url": "", "source": "city"},
                     {"date": _day(9), "time": "6:00 PM", "name": "Plan Commission", "url": "", "source": "city"}],
            "town": [{"date": _day(5), "time": "7:00 PM", "name": "Town Board", "url": "", "source": "town"}],
            "schools": [{"date": _day(3), "time": "5:00 PM", "name": "Regular Meeting", "url": "", "source": "schools"}],
        }
        self.path.write_text(json.dumps(self.previous), encoding="utf-8")

    def _run(self, answers: dict):
        """answers maps a jurisdiction key to a list, or to an exception to raise."""
        def fetch(_cfg, key):
            a = answers[key]
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch("engine.upcoming.fetch_for_jurisdiction", side_effect=fetch):
            return upcoming.update_upcoming(self.cfg, data_path=self.path, sources=list(answers))

    def test_no_answer_keeps_published_future_meetings(self):
        payload = self._run({"city": None, "town": []})
        self.assertEqual([e["name"] for e in payload["city"]], ["Council (today)", "Plan Commission"],
                         "kept, minus the meeting already held")
        self.assertEqual(payload["town"], [], "an upstream that answered with nothing is believed")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), payload)

    def test_fresh_list_replaces_the_previous_one(self):
        fresh = [{"date": _day(1), "time": "", "name": "Special Meeting", "url": "", "source": "city"}]
        self.assertEqual(self._run({"city": fresh})["city"], fresh)

    def test_structure_drift_keeps_last_known_good_and_still_fails_loud(self):
        with self.assertRaises(AdapterStructureError):
            self._run({"schools": AdapterStructureError("page changed"), "town": []})
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["schools"], self.previous["schools"])
        self.assertEqual(written["town"], [])

    def test_no_previous_file_means_an_empty_list(self):
        self.path.unlink()
        self.assertEqual(self._run({"city": None})["city"], [])

    def test_adapter_outage_becomes_none(self):
        with mock.patch("engine.adapters.civicclerk.fetch_upcoming",
                        side_effect=UpstreamUnavailableError("no answer")):
            self.assertIsNone(upcoming.fetch_for_jurisdiction(self.cfg, "city"))


class RulePostedDedupeTest(unittest.TestCase):
    RULE = {"name": "Regular School Board Meeting", "weekday": 2, "nth": 3, "time": "6:30 PM"}

    def _projection(self) -> dict:
        return rule_schedule.project([self.RULE], "schools", 60)[0]

    def test_posted_meeting_covers_the_rule_under_different_wording(self):
        p = self._projection()
        for posted_name in ("Regular Meeting", "Regular School Board Meeting Including a Public Hearing on the Budget"):
            self.assertTrue(rule_schedule.covered_by_posted(p, [{"date": p["date"], "name": posted_name}]), posted_name)

    def test_a_different_meeting_or_date_does_not_cover_it(self):
        p = self._projection()
        self.assertFalse(rule_schedule.covered_by_posted(p, [{"date": p["date"], "name": "Special Meeting"}]))
        self.assertFalse(rule_schedule.covered_by_posted(p, [{"date": _day(-400), "name": "Regular Meeting"}]))

    def test_boardbook_upcoming_suppresses_the_covered_projection(self):
        first = self._projection()
        posted = [{"meeting_id": "1", "date": first["date"], "time": "6:30 PM", "name": "Regular Meeting",
                   "url": "https://bb.example/Public/Agenda/7?meeting=1", "packet_url": ""}]
        cfg = {"base": "https://bb.example", "org_id": 7, "fallback_rules": [self.RULE]}
        with mock.patch("engine.adapters.boardbook.list_meetings", return_value=posted):
            rows = boardbook.fetch_upcoming(cfg, "schools", 60)
        on_that_day = [r["name"] for r in rows if r["date"] == first["date"]]
        self.assertEqual(on_that_day, ["Regular Meeting"], "the posted meeting, not a second projected one")
        later = [r for r in rows if r["date"] > first["date"]]
        self.assertTrue(all(r["name"] == self.RULE["name"] for r in later), "unposted months are still projected")

    def test_reference_instance_projects_the_third_wednesday(self):
        cfg = load_instance(Path(__file__).parent.parent / "instance.json")
        dce, wsd = cfg.by_key["dc_everest"].agendas, cfg.by_key["school_board"].agendas
        self.assertEqual(dce["fallback_rules"],
                         [{"name": "Regular School Board Meeting", "weekday": 2, "nth": 3, "time": "6:30 PM"}])
        self.assertEqual(rule_schedule.nth_weekday(2026, 9, 2, 3), date(2026, 9, 16))
        self.assertEqual(rule_schedule.nth_weekday(2026, 9, 2, 3).strftime("%A"), "Wednesday")
        monday = wsd["fallback_rules"][0]
        self.assertEqual(rule_schedule.nth_weekday(2026, 9, monday["weekday"], monday["nth"]).strftime("%A"),
                         "Monday", "same weekday convention (0=Monday) as the existing school_board rules")


if __name__ == "__main__":
    unittest.main()
