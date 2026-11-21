"""Portal-first boards with minutes (config.MINUTES_ADAPTERS): a meeting is
summarized from its agenda when posted, and again from the minutes once they
appear. A meeting first seen with minutes already posted goes straight to
them. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from engine import overdue, pipeline, publish
from engine.config import (AGENDA_ADAPTERS, MINUTES_ADAPTERS, PORTAL_ADAPTERS, PORTAL_PREFIX,
                           InstanceConfig)
from engine.errors import ConfigError
from helpers import raw_instance

URL = "https://schools.example/Portal/MeetingInformation.aspx?Id=77"
RECENT = (date.today() - timedelta(days=10)).isoformat()


def _config(**agendas):
    raw = raw_instance()
    raw["jurisdictions"].append({
        "key": "district", "name": "Elm School District", "short": "SCHOOL", "accent": "#666666",
        "video": None, "officials": ["Pat Example (Board President)"],
        "agendas": {"adapter": "diligent", "base": "https://schools.example",
                    "portal": "https://schools.example/Portal", **agendas}})
    return InstanceConfig(raw, Path("instance.json"))


class FakeAdapter:
    def __init__(self, agenda="Agenda text " * 30, minutes=None):
        self.agenda, self.minutes = agenda, minutes
        self.calls = []

    def fetch_agenda_text(self, cfg, url):
        self.calls.append(("agenda", url))
        return self.agenda

    def fetch_minutes_text(self, cfg, url):
        self.calls.append(("minutes", url))
        return self.minutes


class RegistryTest(unittest.TestCase):
    def test_every_minutes_adapter_is_a_registered_portal_adapter_with_a_prefix(self):
        self.assertLessEqual(MINUTES_ADAPTERS, PORTAL_ADAPTERS)
        self.assertLessEqual(PORTAL_ADAPTERS, AGENDA_ADAPTERS)
        self.assertEqual(set(PORTAL_PREFIX), PORTAL_ADAPTERS)
        self.assertEqual(len(set(PORTAL_PREFIX.values())), len(PORTAL_PREFIX), "prefixes are unique")
        for prefix in PORTAL_PREFIX.values():
            self.assertTrue(publish._is_synthetic(f"{prefix}_20261117"), prefix)
        self.assertFalse(publish._is_synthetic("dg_abcdefgh"), "letters after the prefix: a YouTube ID")

    def test_new_adapters_require_their_fields(self):
        for agendas in ({"adapter": "diligent", "base": "https://x.example"},          # no portal
                        {"adapter": "iqm2", "portal": "https://x.example"},            # no base
                        {"adapter": "heygov", "portal": "https://x.example"},          # no site
                        {"adapter": "revize", "portal": "https://x.example"},          # no pages
                        {"adapter": "revize", "portal": "https://x.example", "pages": [{"body": "Board"}]}):
            raw = raw_instance()
            raw["jurisdictions"][0]["agendas"] = agendas
            raw["jurisdictions"][0]["video"] = None
            with self.assertRaises(ConfigError, msg=str(agendas)):
                InstanceConfig(raw, Path("instance.json"))

    def test_overdue_knows_minutes_are_coming(self):
        jur = _config().jurisdiction("district")
        self.assertIn("minutes", overdue._upgrade_path(jur))

    def test_committee_badge_comes_from_the_portals_meeting_name(self):
        raw = raw_instance()
        raw["jurisdictions"].append({
            "key": "district", "name": "Elm School District", "short": "SCHOOL", "accent": "#666666",
            "video": None, "committee_map": {"regular school board": "Regular Meeting"},
            "agendas": {"adapter": "diligent", "base": "https://schools.example",
                        "portal": "https://schools.example/Portal"}})
        cfg = InstanceConfig(raw, Path("instance.json"))
        for title, model_said, want in (
                ("Regular School Board Meeting - 2026-11-17", "Elm School Board", "Regular Meeting"),
                ("Personnel Committee Meeting - 2026-11-17", "Personnel", "Personnel Committee Meeting"),
                ("Common Council - Special Meeting - 2026-11-02", "", "Common Council")):
            self.assertEqual(publish.infer_committee(cfg, title, "district", model_said), want, title)
        # A board with recordings keeps the existing order: the model's wording, mapped.
        self.assertEqual(publish.infer_committee(cfg, "County Board 11/17/26", "county", "Board of Supervisors"),
                         "Board of Supervisors")

    def test_card_links_to_the_public_portal(self):
        self.assertEqual(publish._synthetic_fallback_url(_config(), "dg_77", "district"),
                         "https://schools.example/Portal")


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.cfg = _config()
        self.state = {"processed": {}}
        self.saved = []
        patches = [
            mock.patch.object(pipeline, "save_summary", side_effect=self._save),
            mock.patch.object(pipeline, "save_state"),
            mock.patch.object(pipeline.summarize, "summarize_from_minutes",
                              return_value={"overview": "from minutes", "_source": "minutes"}),
            mock.patch.object(pipeline.summarize, "summarize_from_agenda",
                              return_value={"overview": "from agenda", "_source": "agenda"}),
        ]
        self.save, _, self.from_minutes, self.from_agenda = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def _save(self, title, url, source_key, org_label, summary, **kw):
        self.saved.append((kw.get("video_id"), summary["_source"]))
        return f"summaries/{kw.get('video_id')}.md"

    def use(self, adapter):
        p = mock.patch.object(pipeline, "get_adapter", return_value=adapter)
        p.start()
        self.addCleanup(p.stop)
        return adapter


class ProcessTest(PipelineTestCase):
    M = {"id": "77", "name": "Regular Board Meeting", "date": RECENT, "url": URL}

    def test_agenda_first_when_minutes_are_not_posted(self):
        a = self.use(FakeAdapter(minutes=None))
        self.assertTrue(pipeline.process_portal_meeting(self.cfg, "district", self.M, self.state))
        self.assertEqual(a.calls, [("minutes", URL), ("agenda", URL)])
        self.assertEqual(self.saved, [("dg_77", "agenda")])
        info = self.state["processed"]["dg_77"]
        self.assertEqual((info["source"], info["meeting_date"], info["doc_url"]),
                         ("district", RECENT.replace("-", ""), URL))

    def test_minutes_already_posted_are_summarized_directly(self):
        a = self.use(FakeAdapter(minutes="Moved by A, seconded by B. Carried 9-0. " * 20))
        self.assertTrue(pipeline.process_portal_meeting(self.cfg, "district", self.M, self.state))
        self.assertEqual(a.calls, [("minutes", URL)], "no agenda fetch, no agenda summary")
        self.from_agenda.assert_not_called()
        kw = self.from_minutes.call_args.kwargs
        self.assertEqual(kw["officials"], ["Pat Example (Board President)"])
        self.assertEqual(kw["meeting_date"], RECENT.replace("-", ""))
        self.assertEqual(self.saved, [("dg_77", "minutes")])

    def test_nothing_posted_yet_is_not_marked_processed(self):
        self.use(FakeAdapter(agenda=None, minutes=None))
        self.assertFalse(pipeline.process_portal_meeting(self.cfg, "district", self.M, self.state))
        self.assertEqual(self.state["processed"], {})


class LookBackTest(PipelineTestCase):
    """How far back fetch_portal_new asks the portal to list."""

    def since(self, days=14, **jurisdiction):
        raw = raw_instance()
        raw["jurisdictions"].append({
            "key": "district", "name": "Elm School District", "short": "SCHOOL", "accent": "#666666",
            "video": None, "agendas": {"adapter": "diligent", "base": "https://schools.example",
                                       "portal": "https://schools.example/Portal"}, **jurisdiction})
        cfg = InstanceConfig(raw, Path("instance.json"))
        adapter = mock.Mock()
        adapter.list_recent.return_value = []
        self.use(adapter)
        pipeline.fetch_portal_new(cfg, "district", self.state, days)
        return adapter.list_recent.call_args.args[1]

    def test_without_a_start_date_only_the_days_asked_for(self):
        self.assertEqual(self.since(), date.today() - timedelta(days=14))

    def test_with_a_start_date_the_whole_minutes_window_floored_at_the_start(self):
        long_ago = (date.today() - timedelta(days=400)).isoformat()
        self.assertEqual(self.since(start_date=long_ago),
                         date.today() - timedelta(days=pipeline.MINUTES_WINDOW_DAYS),
                         "a scanned agenda's meeting is still listed when its minutes arrive")
        recent = date.today() - timedelta(days=20)
        self.assertEqual(self.since(start_date=recent.isoformat()), recent,
                         "nothing before the board was added is ever created")

    def test_a_bad_start_date_is_refused(self):
        with self.assertRaises(ConfigError):
            self.since(start_date="Nov. 1")


class UpgradeTest(PipelineTestCase):
    def seed(self, vid="dg_77", days_ago=10, source_tag="agenda", source="district"):
        d = (date.today() - timedelta(days=days_ago)).strftime("%Y%m%d")
        self.state["processed"][vid] = {"title": f"Meeting {vid}", "source": source, "doc_url": URL,
                                        "meeting_date": d, "summary_file": f"summaries/{vid}.md"}
        self.tags = getattr(self, "tags", {})
        self.tags[vid] = source_tag

    def run_upgrade(self):
        with mock.patch.object(pipeline, "_summary_source",
                               side_effect=lambda info: (self.tags[Path(info["summary_file"]).stem], None)):
            return pipeline.upgrade_portal_from_minutes(self.cfg, "district", self.state)

    def test_agenda_only_meeting_is_redone_when_minutes_appear(self):
        self.seed()
        a = self.use(FakeAdapter(minutes=None))
        self.assertEqual(self.run_upgrade(), [])
        self.from_minutes.assert_not_called()
        a.minutes = "Minutes of the regular meeting. Carried 9-0. " * 20
        self.assertEqual(self.run_upgrade(), ["dg_77"])
        self.assertEqual(self.saved, [("dg_77", "minutes")])
        self.assertEqual(self.from_minutes.call_args.kwargs["meeting_id"], "dg_77")

    def test_only_recent_agenda_only_meetings_of_this_board_are_checked(self):
        self.seed("dg_1")                                   # candidate
        self.seed("dg_2", source_tag="minutes")             # already final
        self.seed("dg_3", days_ago=pipeline.MINUTES_WINDOW_DAYS + 5)   # too old
        self.seed("dg_4", source="city")                    # another board's
        self.seed("kPvpA6dRbYM")                            # a video, not a portal meeting
        a = self.use(FakeAdapter(minutes="Minutes. " * 60))
        self.assertEqual(self.run_upgrade(), ["dg_1"])
        self.assertEqual(len(a.calls), 1)

    def test_window_is_configurable_per_board(self):
        self.cfg = _config(minutes_upgrade_window_days=120)
        self.seed("dg_3", days_ago=100)
        self.use(FakeAdapter(minutes="Minutes. " * 60))
        self.assertEqual(self.run_upgrade(), ["dg_3"])

    def test_one_failing_page_or_summary_does_not_stop_the_rest(self):
        self.seed("dg_1")
        self.seed("dg_2")
        self.seed("dg_3")
        adapter = FakeAdapter()

        def minutes(cfg, url, _n=[0]):
            _n[0] += 1
            if _n[0] == 1:
                raise OSError("portal hiccup")
            return "Minutes. " * 60
        adapter.fetch_minutes_text = minutes
        self.use(adapter)
        self.from_minutes.side_effect = [RuntimeError("Claude outage"),
                                         {"overview": "ok", "_source": "minutes"}]
        self.assertEqual(self.run_upgrade(), ["dg_3"])

    def test_a_board_without_minutes_support_is_skipped(self):
        raw = raw_instance()
        cfg = InstanceConfig(raw, Path("instance.json"))
        self.assertEqual(pipeline.upgrade_portal_from_minutes(cfg, "city", self.state), [])


if __name__ == "__main__":
    unittest.main()
