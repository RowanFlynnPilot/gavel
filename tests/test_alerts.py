"""The alert feed: one entry the first time the tracker has an agenda preview
or a summary of what happened, and never a second. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import alerts
from engine.config import InstanceConfig
from helpers import raw_instance

NOW = datetime(2027, 3, 15, 11, 0, tzinfo=timezone.utc)
ISSUES = [{"key": "budget_taxes", "label": "Budgets & Taxes"},
          {"key": "roads_transportation", "label": "Roads & Transportation"}]


def _long(d) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _cfg(hide_town=True):
    raw = raw_instance()
    raw["issues"] = ISSUES
    raw["seo"] = {"pages_base_url": "https://pages.example/tracker"}
    raw["jurisdictions"][4]["hidden"] = hide_town
    return InstanceConfig(raw, Path("instance.json"))


def _upcoming(source="city", days_ahead=2, name="Common Council", preview=True):
    e = {"date": (NOW.date() + timedelta(days=days_ahead)).isoformat(), "time": "6:00 PM",
         "name": name, "url": "https://oak.portal.example/event/9", "source": source}
    if preview:
        e["preview"] = {"overview": "The council is scheduled to vote on a paving contract.",
                        "items": [{"item": "Paving contract", "why": "Awards it.", "action": "vote"}],
                        "hearings": ["Rezoning 12 acres for apartments"], "publicComment": True,
                        "issues": ["roads_transportation", "not_on_the_list"]}
    return e


def _meeting(mid, source="county", days_ago=1, **kw):
    return {"id": mid, "source": source, "title": kw.pop("title", f"Board {mid}"),
            "date": _long(NOW.date() - timedelta(days=days_ago)), "isAgendaOnly": False,
            "overview": "The board set the levy.", "issues": ["budget_taxes"],
            "votes": [{"item": "2027 levy", "outcome": "Approved", "tally": "5-2"}], **kw}


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def run_feed(self, feed=None, now=NOW, meetings=(), upcoming=None):
        return alerts.build(self.cfg, feed, now=now, meetings=list(meetings), upcoming=upcoming or {})

    def test_a_previewed_agenda_and_a_summary_of_what_happened_each_get_one_entry(self):
        feed = self.run_feed(meetings=[_meeting("m1")], upcoming={"city": [_upcoming()]})
        agenda = next(e for e in feed["entries"] if e["kind"] == "agenda")
        result = next(e for e in feed["entries"] if e["kind"] == "result")
        self.assertEqual((agenda["board"], agenda["meeting"], agenda["time"]), ("City of Oak", "Common Council", "6:00 PM"))
        self.assertEqual(agenda["issues"], ["roads_transportation"], "keys off the instance's list are dropped")
        self.assertEqual(agenda["hearings"], ["Rezoning 12 acres for apartments"])
        self.assertEqual(agenda["items"], [{"item": "Paving contract", "action": "vote"}])
        self.assertTrue(agenda["publicComment"])
        self.assertEqual(agenda["url"], "https://oak.portal.example/event/9")
        self.assertEqual((result["id"], result["board"], result["issues"]), ("r_m1", "Riverton County", ["budget_taxes"]))
        self.assertEqual(result["contested"], [{"item": "2027 levy", "why": "split 5-2"}])
        self.assertEqual(result["url"], "https://pages.example/tracker/meetings/m1/")
        self.assertEqual(result["first_seen"], "2027-03-15T11:00:00+00:00")
        # The next run, with nothing new, changes nothing.
        again = self.run_feed(feed, now=NOW + timedelta(hours=4), meetings=[_meeting("m1")],
                              upcoming={"city": [_upcoming()]})
        self.assertEqual(again, feed)

    def test_an_upgrade_from_the_agenda_is_announced_when_it_happens(self):
        feed = self.run_feed(meetings=[_meeting("m1", isAgendaOnly=True)])
        self.assertEqual(feed["entries"], [])
        later = NOW + timedelta(days=9)
        feed = self.run_feed(feed, now=later, meetings=[_meeting("m1")])
        self.assertEqual([(e["id"], e["first_seen"][:10]) for e in feed["entries"]], [("r_m1", "2027-03-24")])

    def test_what_is_not_announced(self):
        feed = self.run_feed(
            meetings=[_meeting("old", days_ago=alerts.RESULT_MAX_AGE_DAYS + 5),      # first met too late to be news
                      _meeting("pg_1", source="town"),                               # a board not yet published
                      _meeting("x", source="nowhere")],
            upcoming={"city": [_upcoming(preview=False),                             # agenda not read yet
                               _upcoming(days_ahead=-1, name="Yesterday")],          # already held
                      "town": [_upcoming(source="town", name="Town Board")]})
        self.assertEqual(feed["entries"], [])
        self.assertIn("r_old", feed["seen"], "remembered, so it is never announced later either")
        self.assertNotIn("r_pg_1", feed["seen"], "a hidden board enters the feed when it is published")
        # Published: its upcoming meeting and its recent summary are news then.
        self.cfg = _cfg(hide_town=False)
        feed = self.run_feed(feed, meetings=[_meeting("pg_1", source="town")],
                             upcoming={"town": [_upcoming(source="town", name="Town Board")]})
        self.assertEqual(sorted(e["kind"] for e in feed["entries"]), ["agenda", "result"])

    def test_a_meeting_put_back_on_the_list_or_a_revised_agenda_is_not_announced_twice(self):
        feed = self.run_feed(meetings=[_meeting("m1")], upcoming={"city": [_upcoming()]})
        much_later = NOW + timedelta(days=alerts.KEEP_DAYS + 1)
        revised = _upcoming(days_ahead=alerts.KEEP_DAYS + 2)
        feed = self.run_feed(feed, now=much_later, meetings=[_meeting("m1", days_ago=alerts.KEEP_DAYS + 2)],
                             upcoming={"city": [revised]})
        self.assertEqual([e["kind"] for e in feed["entries"]], ["agenda"], "old entries leave the feed")
        self.assertIn("r_m1", feed["seen"])
        revised["preview"]["overview"] = "A revised agenda adds a budget amendment."
        again = self.run_feed(feed, now=much_later + timedelta(hours=4), upcoming={"city": [revised]})
        self.assertEqual(len(again["entries"]), 1)

    def test_new_since_is_what_a_send_covers(self):
        feed = self.run_feed(meetings=[_meeting("m1")])
        feed = self.run_feed(feed, now=NOW + timedelta(hours=8), meetings=[_meeting("m1"), _meeting("m2")],
                             upcoming={"city": [_upcoming()]})
        self.assertEqual(len(alerts.new_since(feed, "2027-03-15T00:00:00Z")), 3)
        late = alerts.new_since(feed, "2027-03-15T11:00:00+00:00")
        self.assertEqual(sorted(e["id"][:2] for e in late), ["a_", "r_"])
        self.assertEqual(alerts.new_since(feed, "2027-03-16T00:00:00"), [])


if __name__ == "__main__":
    unittest.main()
