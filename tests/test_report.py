"""The funder report: counted from state, summaries, the corrections log and
the cost ledger. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from engine import report
from helpers import make_config

START, END = date(2027, 1, 1), date(2027, 6, 30)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = make_config()
        self.state = {"processed": {}}

    def meeting(self, mid, source, day, summary=None, **info):
        """Add a meeting held on `day` ("20270309") with the given summary."""
        md = self.dir / f"{mid}.md"
        if summary is not None:
            (self.dir / f"{mid}_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        self.state["processed"][mid] = {"title": f"Meeting {mid}", "source": source,
                                        "meeting_date": day, "summary_file": str(md), **info}

    def build(self, **kw):
        kw.setdefault("ledger", [])
        kw.setdefault("corrections", [])
        kw.setdefault("displayed", [])
        return report.build(self.cfg, START, END, today=date(2027, 7, 1), state=self.state, **kw)

    def board(self, data, key):
        return next(b for b in data["boards"] if b["key"] == key)

    def test_meetings_are_split_by_what_the_summary_was_made_from(self):
        self.meeting("vid00000001", "county", "20270112", {"_source": "transcript"})
        self.meeting("vid00000002", "county", "20270209", {"overview": "from before the tag"})
        self.meeting("el_1", "village", "20270215", {"_source": "minutes"})
        self.meeting("el_2", "village", "20270301", {"_source": "agenda"})
        self.meeting("cc_1", "city", "20270302", {"_source": "agenda_with_votes"})
        self.meeting("cc_2", "city", "20270303")                    # sidecar missing
        self.meeting("vid00000003", "county", "20261215", {"_source": "transcript"})  # before
        d = self.build()
        self.assertEqual(d["totals"]["meetings"], 6)
        self.assertEqual((d["totals"]["from_record"], d["totals"]["with_votes_only"],
                          d["totals"]["agenda_only"]), (3, 1, 2))
        self.assertEqual(self.board(d, "county")["from_record"], 2)
        self.assertEqual(self.board(d, "village")["agenda_only"], 1)

    def test_speakers_use_the_field_then_the_prose_and_skip_the_unknown(self):
        self.meeting("vid00000001", "county", "20270112",
                     {"_source": "transcript", "publicCommentSpeakers": 4,
                      "publicComment": "Four residents spoke."})
        self.meeting("vid00000002", "county", "20270209",
                     {"_source": "transcript", "publicComment": "No public comment was offered."})
        self.meeting("vid00000003", "county", "20270309",
                     {"_source": "transcript", "publicComment": "Comment focused on the budget."})
        self.meeting("el_1", "village", "20270215",
                     {"_source": "agenda", "publicComment": "Two residents spoke."})  # agenda: no count
        d = self.build()
        s = self.board(d, "county")["speakers"]
        self.assertEqual((s["meetings_with_record"], s["meetings_counted"], s["speakers"]), (3, 2, 4))
        self.assertEqual(s["per_meeting"], 2.0)
        self.assertEqual(s["meetings_with_a_speaker"], 1)
        self.assertEqual(self.board(d, "village")["speakers"]["meetings_counted"], 0)
        self.assertIsNone(self.board(d, "village")["speakers"]["per_meeting"])

    def test_prior_period_is_the_same_length_immediately_before(self):
        self.meeting("vid00000001", "county", "20261110",
                     {"_source": "transcript", "publicCommentSpeakers": 1})
        self.meeting("vid00000002", "county", "20270112",
                     {"_source": "transcript", "publicCommentSpeakers": 3})
        d = self.build()
        self.assertEqual((d["prior_start"], d["prior_end"]), ("2026-07-04", "2026-12-31"))
        b = self.board(d, "county")
        self.assertEqual(b["speakers_prior"]["per_meeting"], 1.0)
        self.assertEqual(b["speakers"]["per_meeting"], 3.0)

    def test_a_board_first_seen_in_the_period_is_marked_added(self):
        self.meeting("vid00000001", "county", "20260301", {"_source": "transcript"})
        self.meeting("vid00000002", "county", "20270112", {"_source": "transcript"})
        self.meeting("cc_1", "city", "20270302", {"_source": "agenda"})
        d = self.build()
        self.assertFalse(self.board(d, "county")["added_this_period"])
        self.assertTrue(self.board(d, "city")["added_this_period"])
        self.assertIsNone(self.board(d, "town")["first_meeting"])
        self.assertEqual(d["totals"]["boards_added"], 1)

    def test_corrections_previews_and_spend_are_counted_by_date(self):
        corrections = [
            {"id": "a", "find": "x", "replace": "y", "date": "2027-02-01", "kind": "privacy"},
            {"id": "a", "find": "x2", "replace": "y", "date": "2027-02-01", "kind": "privacy"},
            {"id": "b", "find": "x", "replace": "y", "date": "2027-03-01"},
            {"id": "c", "find": "x", "replace": "y", "date": "2026-11-17", "kind": "privacy"}]
        ledger = [
            {"ts": "2027-01-05T10:00:00+00:00", "purpose": "agenda_preview", "meeting_id": "k1",
             "jurisdiction": "city", "usd": 0.004},
            {"ts": "2027-01-06T10:00:00+00:00", "purpose": "agenda_preview", "meeting_id": "k1",
             "jurisdiction": "city", "usd": 0.004},
            {"ts": "2027-01-06T10:00:00+00:00", "purpose": "transcript_summary", "usd": 0.09},
            {"ts": "2026-12-30T10:00:00+00:00", "purpose": "transcript_summary", "usd": 5.0},
            {"ts": "2027-02-01T10:00:00+00:00", "script": "legacy_script", "usd": 0.01}]
        d = self.build(corrections=corrections, ledger=ledger)
        self.assertEqual(d["corrections"], {"total": 3, "by_kind": {"privacy": 2, "correction": 1},
                                            "meetings": 2, "all_time": 4})
        self.assertEqual((d["previews"]["written"], d["previews"]["meetings"]), (2, 1))
        self.assertEqual(d["previews"]["by_board"], {"city": 2})
        self.assertEqual(d["spend"]["calls"], 4)
        self.assertAlmostEqual(d["spend"]["usd"], 0.11, places=2)
        self.assertIn("legacy_script", d["spend"]["by_purpose"])

    def test_markdown_states_what_it_knows_and_what_it_does_not(self):
        self.meeting("vid00000001", "county", "20270112",
                     {"_source": "transcript", "publicCommentSpeakers": 4,
                      "publicComment": "Four residents, including Pat Example of 12 Elm St, spoke."})
        ledger = [{"ts": "2027-03-01T10:00:00+00:00", "purpose": "transcript_summary", "usd": 0.09}]
        md = report.render_markdown(self.build(ledger=ledger))
        self.assertIn("report for January 1, 2027 to June 30, 2027", md)
        self.assertIn("**5 boards** covered", md)
        self.assertIn("| Riverton County | 1 | 1 | 4 | 4.00 | no data |", md)
        self.assertIn("analytics service", md)
        self.assertIn("The cost ledger starts on March 1, 2027", md)
        self.assertNotIn("per meeting summarized", md)      # ledger doesn't cover the period
        self.assertNotIn("Pat Example", md)                 # counts only, never who spoke
        self.assertNotIn("Elm St", md)

    def test_an_empty_tracker_still_reports(self):
        md = report.render_markdown(self.build())
        self.assertIn("**0 meetings** summarized", md)
        self.assertIn("No meeting in this period has a public-comment count yet.", md)
        self.assertIn("No corrections were logged", md)

    def test_meeting_day_precedence(self):
        self.assertEqual(report.meeting_day({"meeting_date": "20270309", "title": "Board - 1/1/2027"}),
                         date(2027, 3, 9))
        self.assertEqual(report.meeting_day({"title": "Special Meeting - 2027-04-30"}), date(2027, 4, 30))
        self.assertEqual(report.meeting_day({"title": "Board", "upload_date": "20270402"}), date(2027, 4, 2))
        self.assertEqual(report.meeting_day({"title": "Board", "processed_at": "2027-05-03T23:35:42+00:00"}),
                         date(2027, 5, 3))
        self.assertIsNone(report.meeting_day({"title": "Board"}))


if __name__ == "__main__":
    unittest.main()
