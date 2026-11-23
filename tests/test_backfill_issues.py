"""Filing older summaries under the issue list: one cheap call each, written
into the sidecar and the displayed record in place. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import backfill_issues
from engine.config import InstanceConfig
from helpers import make_config, raw_instance

ISSUES = [{"key": "budget_taxes", "label": "Budgets & Taxes", "about": "budgets, levies"},
          {"key": "public_safety", "label": "Public Safety"}]


class BackfillTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        raw = raw_instance()
        raw["issues"] = ISSUES
        raw["jurisdictions"][4]["hidden"] = True                    # "town" is under review
        self.cfg = InstanceConfig(raw, Path("instance.json"))
        self.state = {"processed": {}}
        self.data = self.dir / "meetings.json"
        self.displayed = []

    def meeting(self, mid, source="county", displayed=True, **summary):
        summary = {"overview": f"What happened at {mid}.", "topics": ["Budget"],
                   "discussions": [{"item": "Levy", "body": "The board set the levy."}], **summary}
        (self.dir / f"{mid}_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        self.state["processed"][mid] = {"title": f"Meeting {mid}", "source": source,
                                        "summary_file": str(self.dir / f"{mid}.md")}
        if displayed:
            self.displayed.append({"id": mid, "source": source, "title": f"Meeting {mid}", "badge": None})

    def run_it(self, reply='{"issues": ["budget_taxes"]}', **kw):
        self.data.write_text(json.dumps(self.displayed), encoding="utf-8")
        side = reply if isinstance(reply, (list, Exception)) else None
        with mock.patch.object(backfill_issues, "call_claude", side_effect=side,
                               return_value=None if side else reply) as claude:
            stats = backfill_issues.run(self.cfg, state=self.state, meetings_json=self.data, **kw)
        return stats, claude

    def sidecar(self, mid):
        return json.loads((self.dir / f"{mid}_summary.json").read_text(encoding="utf-8"))

    def record(self, mid):
        return next(m for m in json.loads(self.data.read_text(encoding="utf-8")) if m["id"] == mid)

    def test_a_displayed_summary_is_filed_in_its_sidecar_and_on_its_record(self):
        self.meeting("a1")
        stats, claude = self.run_it('```json\n{"issues": ["budget_taxes", "weather"]}\n```')
        self.assertEqual((stats["pending"], stats["filed"]), (1, 1))
        kw = claude.call_args.kwargs
        self.assertEqual((kw["purpose"], kw["meeting_id"], kw["jurisdiction"]), ("issues_backfill", "a1", "county"))
        self.assertIn("haiku", kw["model"])
        self.assertIn("- budget_taxes: Budgets & Taxes (budgets, levies)", kw["prompt"])
        self.assertIn("The board set the levy.", kw["prompt"])
        self.assertEqual(self.sidecar("a1")["issues"], ["budget_taxes"])
        self.assertEqual(self.record("a1")["issues"], ["budget_taxes"])
        self.assertIsNone(self.record("a1")["badge"], "filed in place: nothing is re-published")
        # Run again: nothing left to ask.
        stats, claude = self.run_it()
        self.assertEqual(stats["pending"], 0)
        claude.assert_not_called()

    def test_scope_is_the_displayed_list_of_published_boards_unless_widened(self):
        self.meeting("a1")
        self.meeting("old", displayed=False)                        # rotated off the list
        self.meeting("pg_1", source="town")                         # a hidden board
        self.meeting("new", issues=["public_safety"])               # written under the new prompts
        self.meeting("none", issues=[])                             # already asked: nothing fit
        stats, claude = self.run_it()
        self.assertEqual([c.kwargs["meeting_id"] for c in claude.call_args_list], ["a1"])
        _, claude = self.run_it(include_hidden=True)
        self.assertEqual([c.kwargs["meeting_id"] for c in claude.call_args_list], ["pg_1"])
        _, claude = self.run_it(all_summaries=True)
        self.assertEqual([c.kwargs["meeting_id"] for c in claude.call_args_list], ["old"])

    def test_nothing_fits_is_recorded_and_an_unusable_reply_is_asked_again(self):
        self.meeting("a1")
        self.meeting("a2")
        stats, _ = self.run_it(['{"issues": []}', "I can't decide."])
        self.assertEqual((stats["none_fit"], stats["unusable"], stats["filed"]), (1, 1, 0))
        self.assertEqual(self.sidecar("a1")["issues"], [])
        self.assertNotIn("issues", self.record("a1"), "an empty list is left off the record, as publish does")
        self.assertNotIn("issues", self.sidecar("a2"))
        stats, claude = self.run_it()
        self.assertEqual([c.kwargs["meeting_id"] for c in claude.call_args_list], ["a2"])

    def test_a_claude_failure_stops_the_run_and_keeps_what_was_filed(self):
        for mid in ("a1", "a2", "a3"):
            self.meeting(mid)
        with self.assertLogs("engine.backfill_issues", level="ERROR"):
            stats, claude = self.run_it(['{"issues": ["public_safety"]}', RuntimeError("401")])
        self.assertTrue(stats["stopped"])
        self.assertEqual(claude.call_count, 2)
        self.assertEqual(self.record("a1")["issues"], ["public_safety"])
        self.assertNotIn("issues", self.sidecar("a3"))

    def test_limit_dry_run_and_an_instance_without_issues(self):
        for mid in ("a1", "a2", "a3"):
            self.meeting(mid)
        stats, claude = self.run_it(limit=2)
        self.assertEqual((stats["pending"], claude.call_count), (3, 2))
        stats, claude = self.run_it(dry_run=True)
        self.assertEqual((stats["pending"], claude.call_count), (1, 0))
        self.cfg = make_config()
        stats, claude = self.run_it()
        self.assertEqual(stats["pending"], 0)
        claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
