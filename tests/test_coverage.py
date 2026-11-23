"""The coverage check: was each meeting on the tracker, was its agenda read
before it, and is there a summary of what happened. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from engine import coverage
from engine.config import InstanceConfig
from helpers import make_config, raw_instance

START, END = date(2027, 3, 1), date(2027, 3, 31)
TODAY = date(2027, 4, 20)
def el(n: int) -> str:
    """A municode-style id: the board's prefix and a 32-character guid."""
    return f"el_{n:032x}"


# March 2027: the county's rule is the 4th Tuesday (the 23rd); the schools'
# fallback rule is the 3rd Wednesday (the 17th).


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = make_config()
        self.state = {"processed": {}}
        self.seen = {}

    def meeting(self, mid, source, day, title, kind="transcript", processed=None):
        (self.dir / f"{mid}_summary.json").write_text(json.dumps({"_source": kind}), encoding="utf-8")
        self.state["processed"][mid] = {
            "title": title, "source": source, "meeting_date": day,
            "processed_at": f"{processed or '2027-04-01'}T12:00:00+00:00",
            "summary_file": str(self.dir / f"{mid}.md")}

    def saw(self, source, day, name, when):
        self.seen[f"{source}{day}{name}"] = {"source": source, "date": day, "name": name,
                                             "seen": f"{when}T12:00:00+00:00"}

    def build(self, today=TODAY, cfg=None, **kw):
        return coverage.build(cfg or self.cfg, START, END, today=today, state=self.state,
                              seen=self.seen, **kw)

    def board(self, data, key):
        return next(b for b in data["boards"] if b["key"] == key)

    def row(self, board, name):
        return next(r for r in board["rows"] if r["name"] == name)

    def test_a_regular_meeting_is_matched_to_its_card_and_a_missing_one_is_flagged(self):
        self.meeting("vid00000001", "county", "20270323", "Riverton County Board Meeting")
        d = self.build()
        county = self.board(d, "county")
        self.assertEqual(county["regular"], {"scheduled": 1, "on_tracker": 1, "waiting": 0, "no_card": 0})
        self.assertTrue(self.row(county, "Riverton County Board Meeting")["regular"])
        self.assertEqual(county["look"], [])
        # The schools' regular meeting on the 17th has no card at all.
        schools = self.board(d, "schools")
        self.assertEqual(schools["regular"]["no_card"], 1)
        self.assertEqual(schools["look"][0]["why"], "regular meeting with no card under that name")
        self.assertEqual(schools["look"][0]["same_day"], [])

    def test_a_card_under_other_wording_is_listed_beside_the_regular_meeting_not_matched(self):
        self.meeting("bb_1", "schools", "20270317", "Board Workshop - 2027-03-17", kind="minutes")
        schools = self.board(self.build(), "schools")
        self.assertEqual(schools["regular"]["on_tracker"], 0)
        self.assertEqual(schools["look"][0]["same_day"], ["Board Workshop - 2027-03-17"])
        self.assertEqual(schools["meetings"], 1)

    def test_a_regular_meeting_inside_the_lag_is_waiting_not_missing(self):
        d = self.build(today=date(2027, 3, 25))          # two days after the county's meeting, lag 3
        county = self.board(d, "county")
        self.assertEqual(county["regular"], {"scheduled": 1, "on_tracker": 0, "waiting": 1, "no_card": 0})
        self.assertEqual(county["look"], [])
        self.assertEqual(d["end"], "2027-03-25")         # days after today are left out

    def test_summary_after_is_done_waiting_overdue_or_closed(self):
        self.meeting(el(1), "village", "20270302", "Village Board Meeting - 2027-03-02", kind="minutes")
        self.meeting(el(2), "village", "20270309", "Plan Commission Meeting - 2027-03-09", kind="agenda")
        self.meeting(el(3), "village", "20270310", "Closed Session - 2027-03-10", kind="agenda")
        self.meeting("cc_1", "city", "20270311", "Common Council - 2027-03-11", kind="agenda_with_votes")
        d = self.build()
        village = self.board(d, "village")
        self.assertEqual(village["after"], {"summary": 1, "overdue": 1, "closed": 1})
        self.assertEqual([x["why"] for x in village["look"]], ["still agenda-only past 7 days"])
        self.assertEqual(self.board(d, "city")["after"], {"summary": 1})
        soon = self.board(self.build(today=date(2027, 3, 12)), "village")
        self.assertEqual(soon["after"]["waiting"], 1)

    def test_agenda_before_from_the_card_the_record_or_neither(self):
        # A card made from the agenda the day before the meeting.
        self.meeting(el(1), "village", "20270302", "Village Board Meeting - 2027-03-02",
                     kind="minutes", processed="2027-03-01")
        # A card made two weeks late, but the agenda was read two days ahead.
        self.meeting(el(2), "village", "20270309", "Plan Commission Meeting - 2027-03-09",
                     kind="minutes", processed="2027-03-23")
        self.saw("village", "2027-03-09", "Plan Commission Meeting", "2027-03-07")
        # A card made late and no agenda read.
        self.meeting(el(3), "village", "20270316", "Finance Committee Meeting - 2027-03-16",
                     kind="minutes", processed="2027-03-30")
        village = self.board(self.build(), "village")
        self.assertEqual(village["before"], {"yes": 2, "no": 1})
        self.assertEqual(self.row(village, "Finance Committee Meeting - 2027-03-16")["before"], "no")
        self.assertIn("no agenda was read before the meeting", [x["why"] for x in village["look"]])

    def test_an_agenda_read_after_the_meeting_does_not_count(self):
        self.meeting(el(2), "village", "20270309", "Plan Commission Meeting - 2027-03-09",
                     kind="minutes", processed="2027-03-23")
        self.saw("village", "2027-03-09", "Plan Commission Meeting", "2027-03-10")
        self.saw("village", "2027-02-01", "Village Board Meeting", "2027-01-30")   # starts the record
        self.assertEqual(self.board(self.build(), "village")["before"], {"no": 1})

    def test_a_recording_processed_on_the_meeting_day_is_not_an_agenda(self):
        self.saw("schools", "2027-02-01", "Regular Meeting", "2027-01-30")
        self.meeting("vid00000002", "schools", "20270317", "Regular School Board Meeting",
                     processed="2027-03-17")
        self.assertEqual(self.board(self.build(), "schools")["before"], {"no": 1})

    def test_before_the_record_began_is_unknown_and_a_rule_board_has_no_source(self):
        self.meeting(el(3), "village", "20270316", "Finance Committee Meeting - 2027-03-16",
                     kind="minutes", processed="2027-03-30")
        self.meeting("vid00000001", "county", "20270323", "Riverton County Board Meeting")
        d = self.build()                                  # nothing in the record yet
        self.assertIsNone(d["agenda_record_since"])
        self.assertEqual(self.board(d, "village")["before"], {"unknown": 1})
        county = self.board(d, "county")
        self.assertFalse(county["agenda_source"])
        self.assertEqual(county["before"], {"no_source": 1})

    def test_hidden_boards_are_left_out_unless_asked_for(self):
        raw = raw_instance()
        raw["jurisdictions"][3]["hidden"] = True
        cfg = InstanceConfig(raw, Path("instance.json"))
        d = self.build(cfg=cfg)
        self.assertNotIn("city", [b["key"] for b in d["boards"]])
        self.assertEqual(d["not_published"], ["City of Oak"])
        self.assertIn("city", [b["key"] for b in self.build(cfg=cfg, include_hidden=True)["boards"]])

    def test_markdown_names_what_needs_a_look_and_what_it_cannot_know(self):
        self.meeting("bb_1", "schools", "20270317", "Board Workshop - 2027-03-17", kind="minutes")
        text = coverage.render_markdown(self.build())
        self.assertIn("coverage check, March 1, 2027 to March 31, 2027", text)
        self.assertIn("**Riverton Schools**, Mar 17: Regular School Board Meeting", text)
        self.assertIn("The tracker has these that day: Board Workshop - 2027-03-17.", text)
        self.assertIn("**No regular schedule on file** for Village of Elm, City of Oak, Town of Ash", text)
        self.assertIn("**No agenda source** for Riverton County", text)

    def test_one_file_a_month_for_this_month_and_the_ones_before(self):
        self.assertEqual(coverage.months_back(date(2027, 1, 15), 3),
                         [(date(2026, 11, 1), date(2026, 11, 30)), (date(2026, 12, 1), date(2026, 12, 31)),
                          (date(2027, 1, 1), date(2027, 1, 31))])
        self.meeting("vid00000001", "county", "20270323", "Riverton County Board Meeting")
        out = self.dir / "coverage"
        files = coverage.write_months(self.cfg, out, months=2, today=date(2027, 4, 20),
                                      state=self.state, seen=self.seen)
        self.assertEqual([p.name for p in files], ["2027-03.md", "2027-04.md"])
        march = (out / "2027-03.md").read_text(encoding="utf-8")
        self.assertIn("coverage check, March 1, 2027 to March 31, 2027", march)
        self.assertIn("| Riverton County | 1 | 1 | 1 |", march)
        # The month in progress stops at today.
        self.assertIn("coverage check, April 1, 2027 to April 20, 2027", (out / "2027-04.md").read_text(encoding="utf-8"))

    def test_default_period_is_the_last_calendar_month(self):
        class Args:
            start = end = month = None
        self.assertEqual(coverage._period(Args, date(2027, 1, 5)),
                         (date(2026, 12, 1), date(2026, 12, 31)))
        Args.month = "2027-02"
        self.assertEqual(coverage._period(Args, date(2027, 4, 5)),
                         (date(2027, 2, 1), date(2027, 2, 28)))


if __name__ == "__main__":
    unittest.main()
