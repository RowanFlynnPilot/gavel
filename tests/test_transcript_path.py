"""Residential transcript path: the fetcher's matching, gap sweep, sidecars and unavailable-video bookkeeping,
and the ingest tool's sidecar-first metadata resolution. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from engine import fetch_transcripts as ft
from engine import ingest_transcript as ingest
from engine.config import InstanceConfig


def _quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y%m%d")


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class AudioTitleDateTest(unittest.TestCase):
    def test_every_title_form_seen_on_the_audio_feed(self):
        cases = {
            "2026 09 21 PC Meeting Recording": "20260921",
            "August 24, 2026 Village Board Meeting Recording": "20260824",
            "09082026 Parks Recording": "20260908",
            "May 19th 2026 UC": "20260519",
            "April  30, 2026 Special Village Board": "20260430",
            "6/8/2026 Plan Commission": "20260608",
            "2026-09-21 PC Meeting Recording": "20260921",
        }
        for title, expected in cases.items():
            self.assertEqual(ft._parse_audio_track_date(title), expected, title)

    def test_undatable_titles_parse_to_nothing(self):
        for title in ("260407_1437", "Village Board Meeting Recording", "2026 13 45 PC Meeting"):
            self.assertEqual(ft._parse_audio_track_date(title), "", title)


class AudioTrackPickTest(unittest.TestCase):
    def test_identical_titles_are_one_recording_and_the_newest_upload_wins(self):
        newest = {"title": "2026 09 21 PC Meeting Recording", "url": "https://audio/newest"}
        older = {"title": "2026 09 21  PC meeting recording ", "url": "https://audio/older"}
        self.assertIs(ft._pick_audio_track([newest, older]), newest)

    def test_different_titles_are_ambiguous(self):
        a = {"title": "2026 09 21 PC Meeting Recording", "url": "https://audio/a"}
        b = {"title": "2026 09 21 PC Meeting Recording Part 2", "url": "https://audio/b"}
        self.assertIsNone(ft._pick_audio_track([a, b]))

    def test_one_or_no_candidates(self):
        only = {"title": "2026 09 21 PC Meeting Recording", "url": "https://audio/only"}
        self.assertIs(ft._pick_audio_track([only]), only)
        self.assertIsNone(ft._pick_audio_track([]))


class AudioMatchTest(unittest.TestCase):
    def setUp(self):
        raw = {"instance": {"name": "Tracker", "newsroom": "Newsroom", "newsroom_url": "https://example.org",
                            "region": "Somewhere", "timezone": "America/Chicago", "max_meetings": 50},
               "theme": {}, "jurisdictions": [
                   {"key": "village", "name": "Village", "short": "VIL", "accent": "#123456", "video": None,
                    "audio_url": "https://audio.example/village",
                    "agendas": {"adapter": "municode", "base": "https://x", "ada_url": "https://x/ada",
                                "blob_base": "https://x/blob", "id_prefix": "vl"}}]}
        self.cfg = InstanceConfig(raw, Path("instance.json"))
        self.meetings = [{"id": "vl_1", "source": "village", "isAgendaOnly": True, "date": "September 21, 2026",
                          "title": "Plan Commission", "committee": "Plan Commission"}]

    def _match(self, titles):
        listing = "\n".join(json.dumps({"title": t, "url": f"https://audio.example/{i}"}) for i, t in enumerate(titles))
        with mock.patch.object(ft, "_load_meetings", return_value=self.meetings), \
                mock.patch.object(ft, "_ytdlp_cmd", return_value=["yt-dlp"]), \
                mock.patch.object(ft.subprocess, "run", return_value=_proc(stdout=listing)):
            return _quiet(ft.find_audio_matches, self.cfg)

    def test_duplicate_upload_matches_the_newest_copy(self):
        matches, out = self._match(["2026 09 21 PC Meeting Recording", "2026 09 21 PC Meeting Recording",
                                    "2026 09 14 VB Meeting Recording"])
        self.assertEqual([(m["id"], m["fetch_url"]) for m in matches], [("vl_1", "https://audio.example/0")])
        self.assertNotIn("[warn]", out)

    def test_warns_when_a_newest_title_has_no_date(self):
        titles = ["PC Meeting Recording"] + [f"2026 09 {d:02d} VB Meeting Recording" for d in range(1, 6)]
        _matches, out = self._match(titles)
        self.assertIn("[warn] village: 1 of the 5 newest audio titles", out)
        self.assertIn("'PC Meeting Recording'", out)

    def test_old_undated_titles_are_not_news(self):
        titles = [f"2026 09 {d:02d} VB Meeting Recording" for d in range(1, 6)] + ["260407_1437"]
        _matches, out = self._match(titles)
        self.assertNotIn("[warn]", out)


class GoneSignalTest(unittest.TestCase):
    def test_explicit_permanent_signals(self):
        for text in (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
            "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
            "ERROR: [youtube] abc: This video is no longer available due to a copyright claim",
            "ERROR: [youtube] abc: Video unavailable. This video is no longer available because the YouTube "
            "account associated with this video has been terminated.",
        ):
            self.assertTrue(ft._is_gone(text), text)

    def test_bare_video_unavailable_is_not_permanent(self):
        for text in ("ERROR: [youtube] abc: Video unavailable", "ERROR: [youtube] abc: Only images are available",
                     "ERROR: [youtube] abc: Sign in to confirm you're not a bot", ""):
            self.assertFalse(ft._is_gone(text), text)

    def test_caption_fetch_raises_video_gone_with_the_error_line(self):
        stderr = "WARNING: something else\nERROR: [youtube] abc: Private video. Sign in if you've been granted access"
        with mock.patch.object(ft, "_ytdlp_cmd", return_value=["yt-dlp"]), \
                mock.patch.object(ft.subprocess, "run", return_value=_proc(1, stderr=stderr)):
            with self.assertRaises(ft.VideoGone) as raised:
                ft.fetch_transcript_ytdlp("abc")
        self.assertTrue(str(raised.exception).startswith("ERROR: [youtube] abc: Private video"))

    def test_bare_video_unavailable_is_an_ordinary_failure(self):
        with mock.patch.object(ft, "_ytdlp_cmd", return_value=["yt-dlp"]), \
                mock.patch.object(ft.subprocess, "run",
                                  return_value=_proc(1, stderr="ERROR: [youtube] abc: Video unavailable")):
            text, _out = _quiet(ft.fetch_transcript_ytdlp, "abc")
        self.assertIsNone(text)


class YtdlpProbeTest(unittest.TestCase):
    def test_path_binary_that_exits_nonzero_falls_to_the_module(self):
        with mock.patch.object(ft.subprocess, "run", side_effect=[_proc(1), _proc(0)]):
            cmd, out = _quiet(ft._ytdlp_cmd)
        self.assertEqual(cmd, [sys.executable, "-m", "yt_dlp"])
        self.assertEqual(out, "")

    def test_missing_path_binary_falls_to_the_module(self):
        with mock.patch.object(ft.subprocess, "run", side_effect=[FileNotFoundError(), _proc(0)]):
            cmd, _out = _quiet(ft._ytdlp_cmd)
        self.assertEqual(cmd, [sys.executable, "-m", "yt_dlp"])

    def test_module_probe_that_exits_nonzero_is_not_a_working_install(self):
        with mock.patch.object(ft.subprocess, "run", side_effect=[FileNotFoundError(), _proc(1)]):
            cmd, out = _quiet(ft._ytdlp_cmd)
        self.assertIsNone(cmd)
        self.assertIn("[err]", out)
        self.assertIn(sys.executable, out)


class AudioCapTest(unittest.TestCase):
    def test_captions_run_first_and_audio_is_capped(self):
        jobs = [{"save": "a1", "method": "whisper"}, {"save": "c1"}, {"save": "a2", "method": "whisper"},
                {"save": "a3", "method": "whisper"}, {"save": "c2"}, {"save": "a4", "method": "whisper"}]
        ordered, deferred = ft._cap_audio_jobs(jobs, 3)
        self.assertEqual([j["save"] for j in ordered], ["c1", "c2", "a1", "a2", "a3"])
        self.assertEqual(deferred, 1)

    def test_nothing_deferred_under_the_cap(self):
        jobs = [{"save": "a1", "method": "whisper"}, {"save": "c1"}]
        ordered, deferred = ft._cap_audio_jobs(jobs, 3)
        self.assertEqual([j["save"] for j in ordered], ["c1", "a1"])
        self.assertEqual(deferred, 0)

    def test_cap_of_zero_defers_all_audio(self):
        ordered, deferred = ft._cap_audio_jobs([{"save": "a1", "method": "whisper"}, {"save": "c1"}], 0)
        self.assertEqual([j["save"] for j in ordered], ["c1"])
        self.assertEqual(deferred, 1)


class GapSweepTest(unittest.TestCase):
    def setUp(self):
        video = {"adapter": "youtube", "channel_url": "https://www.youtube.com/@x/streams"}
        raw = {"instance": {"name": "Tracker", "newsroom": "Newsroom", "newsroom_url": "https://example.org",
                            "region": "Somewhere", "timezone": "America/Chicago", "max_meetings": 50,
                            "date_cutoff": _days_ago(20), "skip_ids": ["skipped0001"]},
               "theme": {}, "jurisdictions": [
                   {"key": "county", "name": "County", "short": "CO", "accent": "#123456", "video": video,
                    "agendas": {"adapter": "rule_schedule", "rules": []}},
                   {"key": "city", "name": "City", "short": "CI", "accent": "#123456", "video": video,
                    "agendas": {"adapter": "civicclerk", "api_base": "https://x/v1", "portal": "https://x"}},
                   {"key": "schools", "name": "Schools", "short": "SB", "accent": "#123456", "video": video,
                    "agendas": {"adapter": "boardbook", "base": "https://x", "org_id": 1, "label": "Schools"}},
                   {"key": "village", "name": "Village", "short": "VIL", "accent": "#123456", "video": None,
                    "agendas": {"adapter": "municode", "base": "https://x", "ada_url": "https://x/ada",
                                "blob_base": "https://x/blob", "id_prefix": "vl"}},
                   {"key": "town", "name": "Town", "short": "TN", "accent": "#123456", "video": None,
                    "agendas": {"adapter": "legistar", "client": "town"}}]}
        self.cfg = InstanceConfig(raw, Path("instance.json"))
        self.state = {"processed": {"processed01": {"title": "already in the tracker"}}}

    def _sweep(self, listings, failures=None):
        def list_videos(_video_cfg, key, _label):
            if isinstance(listings[key], Exception):
                raise listings[key]
            return listings[key]

        with mock.patch.object(ft.youtube, "list_videos", side_effect=list_videos) as listed, \
                mock.patch.object(ft, "load_state", return_value=self.state):
            found, _out = _quiet(ft.find_unprocessed_channel_videos, self.cfg, [] if failures is None else failures)
        return found, [call.args[1] for call in listed.call_args_list]

    def test_only_youtube_first_channels_are_swept(self):
        _found, listed = self._sweep({"county": [], "city": []})
        self.assertEqual(listed, ["county", "city"], "not the agenda-platform-first or video-less jurisdictions")

    def test_unprocessed_recent_videos_are_found_with_their_listing_metadata(self):
        recent = _days_ago(3)
        county = [
            {"id": "gapvideo001", "title": "Board - 9/1/26", "meeting_date": recent, "upload_date": _days_ago(2), "duration": 3720},
            {"id": "processed01", "title": "Board", "meeting_date": recent, "upload_date": recent},
            {"id": "skipped0001", "title": "Test stream", "meeting_date": recent, "upload_date": recent},
            {"id": "tooold00001", "title": "Board", "meeting_date": _days_ago(30), "upload_date": _days_ago(30)},
            {"id": "undated0001", "title": "Board", "meeting_date": "", "upload_date": ""},
        ]
        city = [{"id": "gapvideo002", "title": "Council", "meeting_date": "", "upload_date": recent, "duration": None}]
        found, _listed = self._sweep({"county": county, "city": city})
        self.assertEqual(found, [
            {"id": "gapvideo001", "title": "Board - 9/1/26", "source": "county", "meeting_date": recent,
             "upload_date": _days_ago(2), "duration": 3720},
            {"id": "gapvideo002", "title": "Council", "source": "city", "meeting_date": "",
             "upload_date": recent, "duration": None},
        ])

    def test_window_bounds_the_sweep_when_it_is_newer_than_the_date_cutoff(self):
        county = [{"id": "gapvideo001", "title": "Board", "meeting_date": _days_ago(10), "upload_date": ""},
                  {"id": "gapvideo002", "title": "Board", "meeting_date": _days_ago(3), "upload_date": ""}]
        with mock.patch.object(ft, "GAP_SWEEP_DAYS", 5):
            found, _listed = self._sweep({"county": county, "city": []})
        self.assertEqual([v["id"] for v in found], ["gapvideo002"])

    def test_a_channel_that_cannot_be_listed_is_reported_and_the_rest_still_sweep(self):
        failures: list[str] = []
        city = [{"id": "gapvideo002", "title": "Council", "meeting_date": _days_ago(3), "upload_date": ""}]
        found, _listed = self._sweep({"county": RuntimeError("yt-dlp failed"), "city": city}, failures)
        self.assertEqual([v["id"] for v in found], ["gapvideo002"])
        self.assertEqual(len(failures), 1)
        self.assertIn("county", failures[0])


class _TranscriptsDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in (("TRANSCRIPTS_DIR", self.dir), ("UNAVAILABLE_FILE", self.dir / "unavailable.json")):
            patcher = mock.patch.object(ft, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class SidecarAndUnavailableTest(_TranscriptsDir):
    META = {"title": "Board - 9/1/26", "source": "county", "meeting_date": "20260901",
            "upload_date": "20260902", "duration": 3720}

    def test_sidecar_carries_the_listing_metadata(self):
        _quiet(ft._write_meta_sidecar, {"save": "gapvideo001", "meta": self.META})
        self.assertEqual(json.loads((self.dir / "gapvideo001.meta.json").read_text(encoding="utf-8")), self.META)

    def test_existing_sidecar_is_left_alone_and_other_jobs_get_none(self):
        (self.dir / "gapvideo001.meta.json").write_text('{"title": "kept"}', encoding="utf-8")
        _quiet(ft._write_meta_sidecar, {"save": "gapvideo001", "meta": self.META})
        _quiet(ft._write_meta_sidecar, {"save": "bb_1"})
        self.assertEqual((self.dir / "gapvideo001.meta.json").read_text(encoding="utf-8"), '{"title": "kept"}')
        self.assertEqual([p.name for p in self.dir.iterdir()], ["gapvideo001.meta.json"])

    def test_unavailable_videos_accumulate(self):
        self.assertEqual(ft._load_unavailable(), {})
        ft._mark_unavailable("gone0000001", "ERROR: Private video", "Board")
        ft._mark_unavailable("gone0000002", "x" * 500)
        gone = ft._load_unavailable()
        self.assertEqual(sorted(gone), ["gone0000001", "gone0000002"])
        self.assertEqual((gone["gone0000001"]["reason"], gone["gone0000001"]["title"]), ("ERROR: Private video", "Board"))
        self.assertEqual(len(gone["gone0000002"]["reason"]), 200)
        self.assertIn("detected", gone["gone0000001"])

    def test_corrupt_unavailable_file_is_not_read_as_empty(self):
        (self.dir / "unavailable.json").write_text("<<<<<<< HEAD", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            ft._load_unavailable()


class RunTest(_TranscriptsDir):
    """--all end to end with every finder and fetch mocked."""

    def _run(self, gap, audio=(), captions=None, push=False):
        captions = captions or {}
        calls: list[str] = []

        def fetch_transcript(video_id):
            calls.append(video_id)
            result = captions.get(video_id, "caption text " * 30)
            if isinstance(result, Exception):
                raise result
            return result

        def fetch_audio(url, whisper_hint=""):
            calls.append(url)
            return "whisper text " * 30

        git = mock.Mock(return_value=_proc(0))
        with mock.patch.object(ft, "find_agenda_only_meetings", return_value=[]), \
                mock.patch.object(ft, "find_boardbook_video_matches", return_value=[]), \
                mock.patch.object(ft, "find_audio_matches", return_value=list(audio)), \
                mock.patch.object(ft, "find_unprocessed_channel_videos", return_value=list(gap)), \
                mock.patch.object(ft, "fetch_transcript", side_effect=fetch_transcript), \
                mock.patch.object(ft, "fetch_transcript_whisper_url", side_effect=fetch_audio), \
                mock.patch.object(ft.subprocess, "run", git):
            _none, out = _quiet(ft._run, argparse.Namespace(all=True, video_ids=[], push=push), None, [])
        return calls, out, git

    @staticmethod
    def _gap(video_id):
        return {"id": video_id, "title": f"Meeting {video_id}", "source": "county", "meeting_date": "20260901",
                "upload_date": "20260902", "duration": 60}

    def test_audio_runs_after_captions_and_the_rest_is_deferred(self):
        audio = [{"id": f"vl_{i}", "fetch_url": f"https://audio.example/{i}", "title": f"Audio {i}"} for i in range(5)]
        calls, out, _git = self._run([self._gap("gapvideo001")], audio)
        self.assertEqual(calls, ["gapvideo001", "https://audio.example/0", "https://audio.example/1", "https://audio.example/2"])
        self.assertIn("[defer] 2 audio transcription(s)", out)
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.txt")),
                         ["gapvideo001.txt", "vl_0.txt", "vl_1.txt", "vl_2.txt"])

    def test_sidecar_is_written_even_when_the_transcript_already_exists(self):
        (self.dir / "gapvideo001.txt").write_text("fetched before sidecars existed", encoding="utf-8")
        calls, _out, _git = self._run([self._gap("gapvideo001")])
        self.assertEqual(calls, [])
        meta = json.loads((self.dir / "gapvideo001.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta, {"title": "Meeting gapvideo001", "source": "county", "meeting_date": "20260901",
                                "upload_date": "20260902", "duration": 60})

    def test_gone_video_is_recorded_then_skipped_on_the_next_run(self):
        gap = [self._gap("gonevideo01"), self._gap("gapvideo002")]
        calls, out, _git = self._run(gap, captions={"gonevideo01": ft.VideoGone("ERROR: Private video")})
        self.assertEqual(calls, ["gonevideo01", "gapvideo002"])
        self.assertIn("[gone]", out)
        self.assertEqual(list(ft._load_unavailable()), ["gonevideo01"])
        self.assertFalse((self.dir / "gonevideo01.txt").exists())

        (self.dir / "gapvideo002.txt").unlink()
        calls, out, _git = self._run(gap)
        self.assertEqual(calls, ["gapvideo002"])
        self.assertIn("video is private/removed", out)

    def test_push_stages_the_whole_transcripts_directory(self):
        _calls, _out, git = self._run([self._gap("gapvideo001")], push=True)
        self.assertEqual(git.call_args_list[0].args[0], ["git", "add", str(self.dir)])


class IngestSidecarTest(unittest.TestCase):
    META = {"title": "County Board Pt.1 - 8/6/26", "source": "county", "meeting_date": "20260806",
            "upload_date": "20260807", "duration": 3720}

    def test_sidecar_fills_everything_for_a_video_the_pipeline_never_saw(self):
        resolved, _out = _quiet(ingest._apply_sidecar, self.META, "gapvideo001", None, "gapvideo001", None, None)
        self.assertEqual(resolved, ("county", "County Board Pt.1", "20260806", "20260807"))
        self.assertFalse(ingest._needs_youtube_query("gapvideo001", *resolved[1:3]))

    def test_state_and_cli_values_win_over_the_sidecar(self):
        resolved, _out = _quiet(ingest._apply_sidecar, self.META, "gapvideo001", "city", "Common Council", "20260805", "20260806")
        self.assertEqual(resolved, ("city", "Common Council", "20260805", "20260806"))

    def test_iso_date_suffix_is_stripped_from_the_title(self):
        meta = {"title": "Regular Meeting - 2026-08-06"}
        resolved, _out = _quiet(ingest._apply_sidecar, meta, "gapvideo001", "city", "gapvideo001", None, None)
        self.assertEqual(resolved, ("city", "Regular Meeting", None, None))

    def test_youtube_is_asked_only_when_title_or_date_is_still_missing(self):
        self.assertTrue(ingest._needs_youtube_query("gapvideo001", "gapvideo001", "20260806"))
        self.assertTrue(ingest._needs_youtube_query("gapvideo001", "County Board", None))
        self.assertTrue(ingest._needs_youtube_query("gapvideo001", "County Board", ""))
        self.assertFalse(ingest._needs_youtube_query("gapvideo001", "County Board", "20260806"))
        self.assertFalse(ingest._needs_youtube_query("-SFidjEgoHg", "County Board", "20260806"))

    def test_synthetic_ids_are_never_youtube_videos(self):
        for meeting_id in ("bb_719203", "vl_3f2a9c1e-77aa-4c0e-9d7e-0123456789ab", "cc_44"):
            self.assertFalse(ingest._needs_youtube_query(meeting_id, meeting_id, None), meeting_id)


if __name__ == "__main__":
    unittest.main()
