"""Boards fetched from a residential connection (portal_cache, fetch_portals)
and the source-health record behind the "site not answering" alert.
No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import fetch_portals, health, portal_cache, previews, upcoming
from engine.adapters import for_jurisdiction
from engine.config import InstanceConfig
from engine.errors import ConfigError, UpstreamUnavailableError
from helpers import raw_instance

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
TODAY = date.today()
URL = "https://maine.example/?p=101"
UP_URL = "https://maine.example/?p=102"


def _day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _config(**agendas):
    raw = raw_instance()
    raw["jurisdictions"].append({
        "key": "maine", "name": "Village of Maine", "short": "VILLAGE", "accent": "#1F5673",
        "video": None, "start_date": _day(-20),
        "agendas": {"adapter": "townweb", "base": "https://maine.example",
                    "portal": "https://maine.example/agendas-minutes/",
                    "fetch_from": "residential", **agendas}})
    return InstanceConfig(raw, Path("instance.json"))


class TempDirs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target, name, value in ((portal_cache, "CACHE_DIR", self.root / "portal_cache"),
                                    (health, "HEALTH_FILE", self.root / "source_health.json")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_cache(self, fetched_at=None, **payload):
        data = {"fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                "recent": [], "upcoming": [], "agenda": {}, "minutes": {}, **payload}
        path = portal_cache.cache_file("maine")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


class ConfigTest(unittest.TestCase):
    def test_residential_is_only_for_portal_first_boards_without_a_vote_api(self):
        self.assertTrue(portal_cache.is_residential(_config().jurisdiction("maine")))
        for change in ({"fetch_from": "cloud"},):
            with self.assertRaises(ConfigError):
                _config(**change)
        raw = raw_instance()
        raw["jurisdictions"][3]["agendas"]["fetch_from"] = "residential"     # civicclerk: has votes
        with self.assertRaises(ConfigError):
            InstanceConfig(raw, Path("instance.json"))


class CachedPortalTest(TempDirs):
    def test_pipeline_reads_listings_and_texts_from_the_cache(self):
        self.write_cache(recent=[{"id": "101", "name": "Board of Trustees", "date": _day(-5), "url": URL},
                                 {"id": "90", "name": "Board of Trustees", "date": _day(-40), "url": "old"}],
                         upcoming=[{"date": _day(3), "time": "5:30 PM", "name": "Board of Trustees",
                                    "url": UP_URL, "source": "maine"},
                                   {"date": _day(-1), "time": "", "name": "Past", "url": "x", "source": "maine"}],
                         agenda={UP_URL: "Agenda text " * 30}, minutes={URL: "Minutes text"})
        jur = _config().jurisdiction("maine")
        portal = for_jurisdiction(jur)
        self.assertIsInstance(portal, portal_cache.CachedPortal)
        self.assertEqual([m["id"] for m in portal.list_recent(jur.agendas, TODAY - timedelta(days=20))], ["101"])
        self.assertEqual([e["name"] for e in portal.fetch_upcoming(jur.agendas, "maine", 60)],
                         ["Board of Trustees"])
        self.assertEqual(portal.fetch_minutes_text(jur.agendas, URL), "Minutes text")
        self.assertIsNone(portal.fetch_agenda_text(jur.agendas, URL), "not fetched means not posted")
        # Previews read the same cache.
        text = previews.agenda_text_for(_config(), {"source": "maine", "url": UP_URL, "date": _day(3),
                                                    "name": "Board of Trustees"})
        self.assertTrue(text.startswith("Agenda text"))

    def test_a_missing_or_stale_cache_is_an_outage_not_an_empty_board(self):
        jur = _config().jurisdiction("maine")
        with self.assertRaises(UpstreamUnavailableError) as ctx:
            for_jurisdiction(jur).list_recent(jur.agendas, TODAY)
        self.assertIn("residential fetcher", str(ctx.exception))
        self.write_cache(fetched_at=datetime.now(timezone.utc) - timedelta(hours=portal_cache.MAX_AGE_HOURS + 1),
                         upcoming=[{"date": _day(3), "time": "", "name": "Board", "url": UP_URL, "source": "maine"}])
        with self.assertRaises(UpstreamUnavailableError):
            for_jurisdiction(jur).fetch_upcoming(jur.agendas, "maine", 60)
        self.assertIsNone(for_jurisdiction(jur).fetch_minutes_text(jur.agendas, URL))

    def test_a_board_fetched_directly_still_gets_its_module(self):
        cfg = InstanceConfig(raw_instance(), Path("instance.json"))
        self.assertEqual(for_jurisdiction(cfg.jurisdiction("city")).__name__, "engine.adapters.civicclerk")


class FetchPortalsTest(TempDirs):
    def adapter(self, minutes_for=(), agenda="Agenda. " * 40):
        a = mock.Mock()
        a.list_recent.return_value = [
            {"id": "101", "name": "Board of Trustees", "date": _day(-5), "url": URL},
            {"id": "100", "name": "Finance", "date": _day(-9), "url": "u100"},
            {"id": "99", "name": "Plan Commission", "date": _day(-12), "url": "u99"}]
        a.fetch_upcoming.return_value = [
            {"date": _day(3), "time": "5:30 PM", "name": "Board of Trustees", "url": UP_URL, "source": "maine"},
            {"date": _day(30), "time": "", "name": "Far Off", "url": "far", "source": "maine"}]
        a.fetch_minutes_text.side_effect = lambda cfg, url: "Minutes. " * 30 if url in minutes_for else None
        a.fetch_agenda_text.side_effect = lambda cfg, url: agenda
        return a

    def state(self, **sources):
        """processed entries for tw_<id>, each with a sidecar tagged _source."""
        processed = {}
        for mid, tag in sources.items():
            md = self.root / f"{mid}.md"
            (self.root / f"{mid}_summary.json").write_text(json.dumps({"_source": tag}), encoding="utf-8")
            processed[mid] = {"summary_file": str(md), "source": "maine"}
        return {"processed": processed}

    def collect(self, adapter, state):
        with mock.patch.object(fetch_portals, "get_adapter", return_value=adapter):
            return fetch_portals.collect(_config(), "maine", state)

    def test_only_pending_documents_are_fetched(self):
        a = self.adapter(minutes_for={"u100"})
        payload = self.collect(a, self.state(tw_100="agenda", tw_99="minutes"))
        # 101 is new with no minutes: its agenda. 100 is agenda-only and minutes are up: its minutes.
        # 99 is final: nothing. The near upcoming meeting: its agenda. The far one: nothing.
        self.assertEqual(set(payload["agenda"]), {URL, UP_URL})
        self.assertEqual(set(payload["minutes"]), {"u100"})
        fetched = {c.args[1] for c in a.fetch_minutes_text.call_args_list} | \
                  {c.args[1] for c in a.fetch_agenda_text.call_args_list}
        self.assertNotIn("u99", fetched)
        self.assertNotIn("far", fetched)
        self.assertEqual(len(payload["recent"]), 3)
        self.assertEqual(len(payload["upcoming"]), 2)

    def test_a_new_meeting_with_minutes_gets_minutes_not_its_agenda(self):
        a = self.adapter(minutes_for={URL})
        payload = self.collect(a, self.state(tw_100="minutes", tw_99="minutes"))
        self.assertEqual(set(payload["minutes"]), {URL})
        self.assertNotIn(URL, payload["agenda"])

    def test_the_listing_range_matches_the_pipelines(self):
        a = self.adapter()
        self.collect(a, {"processed": {}})
        self.assertEqual(a.list_recent.call_args.args[1], TODAY - timedelta(days=20),
                         "floored at the board's start_date, like fetch_portal_new")

    def test_unchanged_documents_are_rewritten_once_a_day_only(self):
        payload = {"recent": [], "upcoming": [], "agenda": {}, "minutes": {}}
        self.assertTrue(fetch_portals.write_if_changed("maine", payload, NOW))
        self.assertFalse(fetch_portals.write_if_changed("maine", payload, NOW + timedelta(hours=5)))
        self.assertTrue(fetch_portals.write_if_changed("maine", {**payload, "agenda": {URL: "a"}},
                                                       NOW + timedelta(hours=6)))
        self.assertTrue(fetch_portals.write_if_changed("maine", {**payload, "agenda": {URL: "a"}},
                                                       NOW + timedelta(hours=6 + fetch_portals.HEARTBEAT_HOURS)))
        saved = json.loads(portal_cache.cache_file("maine").read_text(encoding="utf-8"))
        self.assertEqual(saved["agenda"], {URL: "a"})

    def test_one_site_down_is_reported_and_exits_nonzero(self):
        with mock.patch.object(fetch_portals, "load_instance", return_value=_config()), \
             mock.patch.object(fetch_portals, "load_state", return_value={"processed": {}}), \
             mock.patch.object(fetch_portals, "collect", side_effect=UpstreamUnavailableError("403")), \
             mock.patch("sys.argv", ["engine.fetch_portals"]):
            self.assertEqual(fetch_portals.main(), 1)
        self.assertFalse(portal_cache.cache_file("maine").exists())


class HealthTest(TempDirs):
    def test_a_source_failing_long_enough_is_reported_and_one_success_clears_it(self):
        health.note("maine", "meetings", "HTTP 403", now=NOW)
        health.note("maine", "meetings", "HTTP 403 again", now=NOW + timedelta(hours=4))
        self.assertEqual(health.unreachable(now=NOW + timedelta(hours=11)), [])
        rows = health.unreachable(now=NOW + timedelta(hours=13))
        self.assertEqual([(r["key"], r["job"], r["hours"], r["checks"], r["last_error"]) for r in rows],
                         [("maine", "meetings", 13, 2, "HTTP 403 again")])
        health.note("maine", "meetings", None)
        self.assertEqual(health.unreachable(now=NOW + timedelta(days=3)), [])

    def test_jobs_are_tracked_separately(self):
        """The calendar answering must not hide that new meetings can't be read."""
        health.note("maine", "meetings", "HTTP 403", now=NOW)
        health.note("maine", "upcoming", None)
        self.assertEqual(len(health.unreachable(now=NOW + timedelta(hours=20))), 1)

    def test_a_healthy_run_leaves_no_file_and_removed_boards_are_forgotten(self):
        health.note("county", "upcoming", None)
        self.assertFalse(health.HEALTH_FILE.exists())
        health.note("gone", "meetings", "HTTP 500", now=NOW)
        self.assertEqual(health.unreachable({"maine"}, now=NOW + timedelta(days=2)), [])
        self.assertEqual(json.loads(health.HEALTH_FILE.read_text(encoding="utf-8")), {})

    def test_issue_body_names_the_board_and_what_to_do(self):
        body = health.render_markdown(
            [{"key": "maine", "job": "meetings", "hours": 14, "checks": 5, "last_error": "HTTP 403 | x"}],
            {"maine": "Village of Maine"})
        self.assertIn("| Village of Maine | finding new meetings | 14 hours (5 checks) | HTTP 403 \\| x |", body)
        self.assertIn('"fetch_from": "residential"', body)

    def test_upcoming_notes_health_only_on_real_runs(self):
        cfg = _config()
        self.write_cache(fetched_at=datetime.now(timezone.utc) - timedelta(days=30))
        with mock.patch.object(upcoming, "previews", create=True):
            upcoming.update_upcoming(cfg, data_path=self.root / "upcoming.json", sources=["maine"], previews="off")
            self.assertFalse(health.HEALTH_FILE.exists(), "a test or preview build must not write health")
            with mock.patch.object(upcoming, "UPCOMING_JSON", self.root / "real_upcoming.json"):
                upcoming.update_upcoming(cfg, sources=["maine"], previews="off")
        data = json.loads(health.HEALTH_FILE.read_text(encoding="utf-8"))
        self.assertIn("residential fetcher", data["maine:upcoming"]["last_error"])


if __name__ == "__main__":
    unittest.main()
