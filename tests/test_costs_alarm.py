"""Spending alarm over the cost ledger. No network, no API calls.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import costs

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _row(hours_ago: float, usd: float, meeting="vidA", purpose="transcript_summary",
         model="claude-sonnet-4-6") -> dict:
    return {"ts": (NOW - timedelta(hours=hours_ago)).isoformat(), "meeting_id": meeting,
            "jurisdiction": "county", "purpose": purpose, "model": model,
            "input_tokens": 1000, "output_tokens": 500, "usd": usd}


class AlarmTest(unittest.TestCase):
    def test_window_is_by_timestamp(self):
        rows = [_row(1, 0.1), _row(23.9, 0.1), _row(24.1, 0.1), _row(200, 0.1)]
        self.assertEqual(len(costs.window(rows, 24, NOW)), 2)
        self.assertEqual(len(costs.window(rows, 24 * 7, NOW)), 3)

    def test_trips_on_spend_or_on_call_count(self):
        self.assertFalse(costs.is_tripped([_row(1, 1.99)], 2.00, 40))
        self.assertTrue(costs.is_tripped([_row(1, 1.50), _row(2, 0.51)], 2.00, 40))
        self.assertFalse(costs.is_tripped([_row(1, 0.01)] * 40, 2.00, 40))
        self.assertTrue(costs.is_tripped([_row(1, 0.01)] * 41, 2.00, 40))

    def test_breakdown_and_repeats(self):
        rows = [_row(1, 0.30), _row(2, 0.30), _row(3, 0.02, meeting="kw_1", purpose="agenda_summary",
                                                   model="claude-haiku-4-5-20251001")]
        self.assertEqual(costs.breakdown(rows)[0][:3], ("transcript_summary", "claude-sonnet-4-6", 2))
        self.assertEqual(costs.repeated_calls(rows), [("vidA", "transcript_summary", 2)])

    def test_markdown_names_totals_thresholds_and_the_repeat(self):
        day = [_row(1, 1.25), _row(2, 1.25)]
        body = costs.render_markdown(day, day + [_row(100, 0.5)], 2.00, 40)
        self.assertIn("**2 Claude call(s) costing $2.50**", body)
        self.assertIn("$2.00 or 40 calls", body)
        self.assertIn("| transcript_summary | claude-sonnet-4-6 | 2 | $2.50 |", body)
        self.assertIn("- `vidA` transcript_summary: 2 calls", body)
        self.assertIn("Last 7 days: 3 calls, $3.00.", body)


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ledger = self.dir / "costs.json"
        p = mock.patch.object(costs, "COSTS_JSON", self.ledger)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, *argv, env=None) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["costs", *argv]), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(costs.main(), 0)
        return out.getvalue().strip()

    def _write(self, usd_each: float, n: int):
        now = datetime.now(timezone.utc)
        rows = [{**_row(0, usd_each), "ts": (now - timedelta(minutes=i)).isoformat()} for i in range(n)]
        self.ledger.write_text(json.dumps(rows), encoding="utf-8")

    def test_ok_writes_no_issue_body(self):
        self._write(0.10, 3)
        body = self.dir / "spend.md"
        self.assertEqual(self._run("--check", "--print-status", "--markdown", str(body)), "OK 0.30")
        self.assertFalse(body.exists())

    def test_tripped_writes_the_issue_body(self):
        self._write(0.75, 3)
        body = self.dir / "spend.md"
        self.assertEqual(self._run("--check", "--print-status", "--markdown", str(body)), "TRIPPED 2.25")
        self.assertIn("costing $2.25", body.read_text(encoding="utf-8"))

    def test_thresholds_come_from_the_environment(self):
        self._write(0.10, 3)
        self.assertEqual(self._run("--check", "--print-status", env={"SPEND_ALARM_DAY_CALLS": "2"}),
                         "TRIPPED 0.30")
        self.assertEqual(self._run("--check", "--print-status", env={"SPEND_ALARM_DAY_USD": "0.25"}),
                         "TRIPPED 0.30")

    def test_no_ledger_yet_is_ok(self):
        self.assertEqual(self._run("--check", "--print-status"), "OK 0.00")

    def test_summary_keeps_stdout_clean(self):
        self._write(0.10, 3)
        self.assertEqual(self._run("--summary"), "")


if __name__ == "__main__":
    unittest.main()
