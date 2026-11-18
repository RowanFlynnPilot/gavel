"""Overdue-upgrade check: agenda-only meetings past their jurisdiction's
expected lag. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from datetime import date

from engine import overdue
from helpers import make_config

TODAY = date(2026, 9, 25)


def _meeting(mid: str, source: str, held: str, *, agenda_only=True, title="Regular Meeting") -> dict:
    return {"id": mid, "source": source, "title": title, "date": held, "isAgendaOnly": agenda_only}


class FindOverdueTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def _ids(self, meetings, **kw) -> list[str]:
        return [m["id"] for m in overdue.find_overdue(self.cfg, meetings, TODAY, **kw)]

    def test_lag_is_per_jurisdiction(self):
        meetings = [
            _meeting("county_late", "county", "September 21, 2026"),     # 4 days, lag 3
            _meeting("county_ok", "county", "September 22, 2026"),       # 3 days, lag 3
            _meeting("schools_ok", "schools", "September 5, 2026"),      # 20 days, lag 21
            _meeting("schools_late", "schools", "September 3, 2026"),    # 22 days, lag 21
            _meeting("village_default", "village", "September 17, 2026"),  # 8 days, default 7
            _meeting("village_ok", "village", "September 18, 2026"),     # 7 days, default 7
        ]
        self.assertEqual(self._ids(meetings), ["schools_late", "village_default", "county_late"],
                         "longest wait first")
        found = overdue.find_overdue(self.cfg, meetings, TODAY)[0]
        self.assertEqual((found["_waited"], found["_lag"]), (22, 21))

    def test_closed_sessions_and_upgraded_meetings_are_not_flagged(self):
        meetings = [
            _meeting("closed", "county", "August 1, 2026", title="Executive Committee Closed Session"),
            _meeting("upgraded", "county", "August 1, 2026", agenda_only=False),
            _meeting("undated", "county", "sometime"),
        ]
        self.assertEqual(self._ids(meetings), [])

    def test_portal_first_meetings(self):
        meetings = [
            # Summarized from agenda + recorded votes: published as an outcome summary.
            _meeting("cc_1", "city", "August 1, 2026", agenda_only=False),
            # Agenda posted, the portal has recorded no votes yet, past the lag: late.
            _meeting("cc_2", "city", "August 1, 2026"),
            # PrimeGov posts no votes and the town has no video: agenda-only is final.
            _meeting("pg_1", "town", "August 1, 2026"),
        ]
        self.assertEqual(self._ids(meetings), ["cc_2"])
        self.assertIn("vote records", overdue.find_overdue(self.cfg, meetings, TODAY)[0]["_where"])

    def test_source_filter_and_unknown_jurisdiction(self):
        meetings = [_meeting("a", "county", "August 1, 2026"), _meeting("b", "schools", "August 1, 2026"),
                    _meeting("c", "retired", "August 1, 2026")]
        self.assertEqual(self._ids(meetings, source="schools"), ["b"])
        orphan = next(m for m in overdue.find_overdue(self.cfg, meetings, TODAY) if m["id"] == "c")
        self.assertEqual(orphan["_lag"], 7)

    def test_where_to_look_follows_the_upgrade_path(self):
        where = {m["source"]: m["_where"] for m in overdue.find_overdue(self.cfg, [
            _meeting("a", "county", "August 1, 2026"), _meeting("b", "schools", "August 1, 2026"),
            _meeting("c", "village", "August 1, 2026")], TODAY)}
        self.assertIn("caption sweep", where["county"])
        self.assertIn("recording match", where["schools"])
        self.assertIn("minutes", where["village"])
        self.assertIn("audio", where["village"])

    def test_markdown_has_one_row_per_meeting(self):
        found = overdue.find_overdue(self.cfg, [_meeting("a", "county", "September 1, 2026", title="County Board")], TODAY)
        body = overdue.render_markdown(found, TODAY)
        self.assertIn("| County Board | county | September 1, 2026 | 24 days | 3 days |", body)
        self.assertIn("checked 2026-09-25", body)


if __name__ == "__main__":
    unittest.main()
