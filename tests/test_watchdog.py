"""Stuck-video watchdog, the run report, and the running-time backfill.
No network; state files live in a temp directory.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import pipeline, watchdog
from engine.adapters import youtube
from helpers import make_config

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _seen(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.skipped = Path(tmp.name) / "skipped_videos.json"
        self.unavailable = Path(tmp.name) / "unavailable.json"
        for name, path in (("SKIPPED_FILE", self.skipped), ("UNAVAILABLE_FILE", self.unavailable)):
            p = mock.patch.object(watchdog, name, path)
            p.start()
            self.addCleanup(p.stop)

    def _write(self, entries: dict):
        self.skipped.write_text(json.dumps(entries), encoding="utf-8")

    def _watched(self) -> set:
        return set(json.loads(self.skipped.read_text(encoding="utf-8")))

    def test_first_sighting_is_recorded_once(self):
        watchdog.note_skipped_video("vidA", "Board - 11/16/26", "county")
        first = json.loads(self.skipped.read_text(encoding="utf-8"))["vidA"]
        self.assertEqual((first["title"], first["source"]), ("Board - 11/16/26", "county"))
        watchdog.note_skipped_video("vidA", "retitled", "county")
        self.assertEqual(json.loads(self.skipped.read_text(encoding="utf-8"))["vidA"], first)

    def test_only_videos_past_the_threshold_are_stale(self):
        self._write({"old": {"first_seen": _seen(40), "title": "Old", "source": "county"},
                     "edge": {"first_seen": _seen(36), "title": "Edge", "source": "county"},
                     "new": {"first_seen": _seen(35), "title": "New", "source": "county"}})
        stale = watchdog.stale_skipped_videos({}, frozenset(), {}, now=NOW)
        self.assertEqual({s["id"]: s["age_hours"] for s in stale}, {"old": 40, "edge": 36})
        self.assertEqual(stale[0], {"id": "old", "title": "Old", "source": "county", "age_hours": 40})
        self.assertEqual(self._watched(), {"old", "edge", "new"}, "nothing dismissed")

    def test_processed_skipped_and_unavailable_videos_are_dismissed(self):
        self._write({v: {"first_seen": _seen(50), "title": v, "source": "county"}
                     for v in ("done", "operator_skip", "private", "stuck")})
        self.unavailable.write_text(json.dumps({"private": {"reason": "private video"}}), encoding="utf-8")
        stale = watchdog.stale_skipped_videos({"done": {}}, frozenset({"operator_skip"}), {}, now=NOW)
        self.assertEqual([s["id"] for s in stale], ["stuck"])
        self.assertEqual(self._watched(), {"stuck"})

    def test_video_gone_from_its_channel_listing_is_dismissed(self):
        self._write({"gone": {"first_seen": _seen(50), "title": "Gone", "source": "county"},
                     "listed": {"first_seen": _seen(50), "title": "Listed", "source": "county"},
                     "unlisted_source": {"first_seen": _seen(50), "title": "Other", "source": "schools"}})
        stale = watchdog.stale_skipped_videos({}, frozenset(), {"county": {"listed", "another"}}, now=NOW)
        self.assertEqual({s["id"] for s in stale}, {"listed", "unlisted_source"},
                         "a channel that was not listed this run proves nothing about its videos")
        self.assertEqual(self._watched(), {"listed", "unlisted_source"})

    def test_markdown_lists_each_video_and_the_dismiss_lever(self):
        body = watchdog.render_markdown([{"id": "vidA", "title": "Board", "source": "county", "age_hours": 40}])
        self.assertIn("- [county] [Board](https://www.youtube.com/watch?v=vidA) — unprocessed for 40h", body)
        self.assertIn("instance.skip_ids", body)

    def test_run_report_is_written_with_no_agenda_only_meetings(self):
        report = Path(self.skipped.parent) / "report.json"
        # write_run_report reads the real clock, so "moments ago" must too.
        just_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write({"stuck": {"first_seen": just_now, "title": "Fresh", "source": "county"}})
        with mock.patch.object(pipeline, "RUN_REPORT", report), \
                mock.patch.object(youtube, "LISTED_IDS", {}):
            pipeline.write_run_report(make_config(), {"processed": {}}, ok=0)
        written = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(written["agenda_only"], [])
        self.assertEqual(written["stale_unprocessed"], [], "seen moments ago: watched, not yet stale")


class ListingRegistryTest(unittest.TestCase):
    LISTING = "\n".join(json.dumps(v) for v in [
        {"id": "aaaaaaaaaaa", "title": "County Board - 11/10/26", "duration": 4800},
        {"id": "bbbbbbbbbbb", "title": "County Board - 9/1/26", "duration": None},
    ])

    def _list(self, returncode=0, stdout=None):
        result = mock.Mock(returncode=returncode, stdout=self.LISTING if stdout is None else stdout, stderr="")
        with mock.patch("engine.adapters.youtube.subprocess.run", return_value=result), \
                mock.patch.object(youtube, "_YTDLP_BASE", ["yt-dlp"]), \
                mock.patch.object(youtube, "LISTED_IDS", {}) as ids, \
                mock.patch.object(youtube, "LISTED_DURATIONS", {}) as durations, \
                contextlib.redirect_stdout(io.StringIO()):  # the progress line isn't cp1252-safe
            videos = youtube.list_videos({"channel_url": "https://youtube.example/@county"}, "county",
                                         "Riverton County", dateafter="20261101")
            return videos, dict(ids), dict(durations)

    def test_listing_is_recorded_before_the_date_filter(self):
        videos, ids, durations = self._list()
        self.assertEqual([v["id"] for v in videos], ["aaaaaaaaaaa"], "the date filter still applies to the result")
        self.assertEqual(ids, {"county": {"aaaaaaaaaaa", "bbbbbbbbbbb"}})
        self.assertEqual(durations, {"aaaaaaaaaaa": 4800})

    def test_a_partial_listing_dismisses_nothing(self):
        _videos, ids, durations = self._list(returncode=1)
        self.assertEqual(ids, {}, "yt-dlp failed partway: the ID set is not trusted")
        self.assertEqual(durations, {"aaaaaaaaaaa": 4800})


class DurationBackfillTest(unittest.TestCase):
    def test_fills_only_missing_running_times(self):
        state = {"processed": {"has": {"duration": 3600}, "missing": {"duration": None},
                               "absent": {}, "bb_1": {"duration": None}}}
        n = pipeline.fill_missing_durations(state, {"has": 1, "missing": 4800, "absent": 600})
        self.assertEqual(n, 2)
        self.assertEqual({k: v.get("duration") for k, v in state["processed"].items()},
                         {"has": 3600, "missing": 4800, "absent": 600, "bb_1": None})


if __name__ == "__main__":
    unittest.main()
