"""The NEW badge: a meeting carries it for its first day on the tracker, and
rebuilding the list does not take it away. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import publish
from helpers import make_config

NOW = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


class IsNewTest(unittest.TestCase):
    def test_a_meeting_is_new_for_its_first_day(self):
        self.assertTrue(publish.is_new({"processed_at": _at(0.1)}, NOW))
        self.assertTrue(publish.is_new({"processed_at": _at(23.5)}, NOW))
        self.assertFalse(publish.is_new({"processed_at": _at(24.5)}, NOW))
        self.assertTrue(publish.is_new({"processed_at": _at(2).replace("+00:00", "Z")}, NOW))

    def test_no_usable_time_is_not_new(self):
        for info in (None, {}, {"processed_at": ""}, {"processed_at": "last week"},
                     {"processed_at": _at(-3)}):                 # a clock that ran ahead
            self.assertFalse(publish.is_new(info, NOW), info)


class PublishTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = make_config()
        self.state = {"processed": {}}
        self.injected: set[str] = set()
        self.data = self.dir / "data" / "meetings.json"
        state_file = self.dir / "processed_meetings.json"
        for target, attr, value in (
                (publish, "STATE_FILE", state_file),
                (publish, "load_injected", lambda: set(self.injected)),
                (publish, "save_injected", lambda ids: self.injected.update(ids))):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.state_file = state_file

    def meeting(self, mid, day, hours_ago):
        (self.dir / f"{mid}_summary.json").write_text(
            json.dumps({"overview": "x", "_source": "minutes", "agenda": [], "discussions": []}),
            encoding="utf-8")
        self.state["processed"][mid] = {
            "title": f"Village Board Meeting - {day[:4]}-{day[4:6]}-{day[6:]}", "source": "village",
            "meeting_date": day, "processed_at": _at(hours_ago),
            "summary_file": str(self.dir / f"{mid}.md")}

    def publish(self, now=NOW):
        self.state_file.write_text(json.dumps(self.state), encoding="utf-8")
        publish.publish(self.cfg, data_path=self.data, now=now)
        return {m["id"]: m["badge"] for m in json.loads(self.data.read_text(encoding="utf-8"))}

    def test_the_badge_survives_a_second_publish_and_ends_after_a_day(self):
        fresh, old = "el_" + "a" * 32, "el_" + "b" * 32
        self.meeting(fresh, "20260309", hours_ago=2)
        self.meeting(old, "20260301", hours_ago=200)
        self.assertEqual(self.publish(), {fresh: "new", old: None})
        # Every run publishes twice; the second pass used to wipe the badge.
        self.assertEqual(self.publish(), {fresh: "new", old: None})
        self.assertEqual(self.publish(now=NOW + timedelta(hours=23)), {fresh: None, old: None})

    def test_a_meeting_put_back_on_the_list_is_not_new_again(self):
        old = "el_" + "b" * 32
        self.meeting(old, "20260301", hours_ago=200)
        self.publish()
        self.injected.clear()                                    # an upgrade pass re-publishes it
        self.assertEqual(self.publish(), {old: None})


if __name__ == "__main__":
    unittest.main()
