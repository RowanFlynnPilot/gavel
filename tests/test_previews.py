"""Agenda previews: what gets a preview, what it costs, and what survives a
failure. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import previews, upcoming
from helpers import make_config

TODAY = date.today()
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
AGENDA = "1. Call to order\n2. Resolution to award the 2027 street paving contract\n" * 8
REPLY = json.dumps({
    "overview": "The council is scheduled to vote on a paving contract.",
    "items": [{"item": "Street paving contract", "why": "Awards the 2027 contract.",
               "action": "vote"}],
    "publicComment": True, "closedSession": False})


def _day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _entry(offset=2, source="city", name="Common Council", url="https://oak.portal.example/event/9/overview"):
    return {"date": _day(offset), "time": "6:00 PM", "name": name, "url": url, "source": source}


class PreviewTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "previews.json"
        self.cfg = make_config()

    def attach(self, payload, now=NOW, generate=True, text=AGENDA, reply=REPLY):
        """Run attach() with the agenda fetch and Claude replaced. `reply` may
        be a string, a list of strings/exceptions, or an exception."""
        side = reply if isinstance(reply, (list, Exception)) else None
        with mock.patch.object(previews, "agenda_text_for",
                               side_effect=text if callable(text) else None,
                               return_value=None if callable(text) else text) as fetch, \
             mock.patch.object(previews, "call_claude", side_effect=side,
                               return_value=None if side else reply) as claude:
            stats = previews.attach(self.cfg, payload, generate=generate, now=now,
                                    cache_file=self.cache)
        return stats, fetch, claude


class AttachTest(PreviewTestCase):
    def test_posted_agenda_gets_a_preview_through_the_cheap_model(self):
        payload = {"city": [_entry()]}
        stats, _, claude = self.attach(payload)
        self.assertEqual(stats["generated"], 1)
        p = payload["city"][0]["preview"]
        self.assertEqual(p["items"][0]["action"], "vote")
        self.assertTrue(p["publicComment"])
        self.assertEqual(p["generated"], NOW.isoformat(timespec="seconds"))
        kw = claude.call_args.kwargs
        self.assertEqual(kw["model"], previews.CLAUDE_MODEL_AGENDA)
        self.assertEqual(kw["purpose"], "agenda_preview")
        self.assertEqual(kw["jurisdiction"], "city")

    def test_unchanged_agenda_never_costs_a_second_call(self):
        self.attach({"city": [_entry()]})
        later = NOW + timedelta(hours=previews.PREVIEW_RECHECK_HOURS + 1)
        payload = {"city": [_entry()]}
        stats, fetch, claude = self.attach(payload, now=later)
        self.assertEqual(fetch.call_count, 1)          # re-read, same text
        claude.assert_not_called()
        self.assertEqual(stats["attached"], 1)
        self.assertIn("preview", payload["city"][0])

    def test_agenda_is_not_reread_inside_the_recheck_window(self):
        self.attach({"city": [_entry()]})
        _, fetch, claude = self.attach({"city": [_entry()]}, now=NOW + timedelta(hours=4))
        fetch.assert_not_called()
        claude.assert_not_called()

    def test_revised_agenda_is_previewed_again_up_to_the_cap(self):
        when = NOW
        for n in range(previews.MAX_GENERATIONS + 2):
            _, _, claude = self.attach({"city": [_entry()]}, now=when,
                                       text=AGENDA + f"\nAddendum {n}")
            when += timedelta(hours=previews.PREVIEW_RECHECK_HOURS + 1)
            self.assertEqual(claude.call_count, 1 if n < previews.MAX_GENERATIONS else 0, n)

    def test_per_run_cap_defers_the_rest_to_the_next_run(self):
        entries = [_entry(name=f"Committee {i}") for i in range(previews.PREVIEW_MAX_PER_RUN + 3)]
        stats, _, claude = self.attach({"city": entries})
        self.assertEqual(claude.call_count, previews.PREVIEW_MAX_PER_RUN)
        self.assertEqual(stats["deferred"], 3)
        # Next run, minutes later: the deferred ones go first, the done ones cost nothing.
        again = [dict(e) for e in entries]
        for e in again:
            e.pop("preview", None)
        stats, _, claude = self.attach({"city": again}, now=NOW + timedelta(minutes=5))
        self.assertEqual(claude.call_count, 3)
        self.assertEqual(stats["attached"], len(entries))

    def test_meetings_outside_the_window_and_unposted_agendas_get_nothing(self):
        far = _entry(offset=previews.PREVIEW_DAYS + 1, name="Far Off")
        past = _entry(offset=-1, name="Yesterday")
        payload = {"city": [far, past]}
        _, fetch, claude = self.attach(payload)
        fetch.assert_not_called()
        claude.assert_not_called()
        payload = {"city": [_entry()]}
        stats, fetch, claude = self.attach(payload, text=None)
        claude.assert_not_called()
        self.assertNotIn("preview", payload["city"][0])
        # ...and the agenda is looked for again on the very next run.
        _, fetch, _ = self.attach({"city": [_entry()]}, now=NOW + timedelta(minutes=5), text=None)
        self.assertEqual(fetch.call_count, 1)

    def test_a_read_agenda_is_recorded_once_and_outlives_the_cache(self):
        seen_file = previews.seen_path(self.cache)
        self.attach({"city": [_entry()]}, text=None)             # not posted yet: nothing recorded
        self.assertEqual(previews.load_seen(seen_file), {})
        self.attach({"city": [_entry()]}, now=NOW + timedelta(minutes=5))
        rec, = previews.load_seen(seen_file).values()
        self.assertEqual((rec["source"], rec["date"], rec["name"]), ("city", _day(2), "Common Council"))
        first = rec["seen"]
        # A later read of a revised agenda keeps the first time it was seen.
        self.attach({"city": [_entry()]}, now=NOW + timedelta(days=1), text=AGENDA + "\nrevised")
        self.assertEqual(next(iter(previews.load_seen(seen_file).values()))["seen"], first)
        # A run with other meetings drops the passed one from the cache, not from the record.
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        key, = cache
        cache[key]["date"] = _day(-5)
        self.cache.write_text(json.dumps(cache), encoding="utf-8")
        self.attach({"city": [_entry(name="Plan Commission")]}, now=NOW + timedelta(days=2))
        self.assertNotIn(key, json.loads(self.cache.read_text(encoding="utf-8")))
        self.assertIn(key, previews.load_seen(seen_file))
        # Cached mode (--publish-only) reads no agenda and records nothing.
        seen_file.unlink()
        self.attach({"city": [_entry()]}, generate=False)
        self.assertFalse(seen_file.exists())

    def test_claude_outage_stops_the_run_and_charges_no_attempt(self):
        entries = [_entry(name="A"), _entry(name="B"), _entry(name="C")]
        stats, _, claude = self.attach({"city": entries}, reply=RuntimeError("401"))
        self.assertEqual(claude.call_count, 1)         # no hammering a dead key
        self.assertEqual(stats["generated"], 0)
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertTrue(all(not r.get("generations") for r in cache.values()))
        # Back up minutes later: all three are written.
        stats, _, claude = self.attach({"city": [dict(e) for e in entries]},
                                       now=NOW + timedelta(minutes=5))
        self.assertEqual(stats["generated"], 3)

    def test_unusable_reply_is_counted_and_keeps_the_last_good_preview(self):
        self.attach({"city": [_entry()]})
        payload = {"city": [_entry()]}
        stats, _, _ = self.attach(payload, text=AGENDA + "\nrevised", reply="Sorry, I can't.",
                                  now=NOW + timedelta(hours=previews.PREVIEW_RECHECK_HOURS + 1))
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(payload["city"][0]["preview"]["overview"],
                         "The council is scheduled to vote on a paving contract.")

    def test_portal_error_costs_only_that_meeting(self):
        def text(cfg, entry):
            if entry["name"] == "Broken":
                raise OSError("portal down")
            return AGENDA
        payload = {"city": [_entry(name="Broken"), _entry(name="Fine")]}
        stats, _, _ = self.attach(payload, text=text)
        self.assertEqual(stats["generated"], 1)
        self.assertNotIn("preview", payload["city"][0])
        self.assertIn("preview", payload["city"][1])

    def test_cached_mode_attaches_without_fetching_or_writing(self):
        self.attach({"city": [_entry()]})
        before = self.cache.read_bytes()
        payload = {"city": [_entry(), _entry(name="No Preview Yet")]}
        stats, fetch, claude = self.attach(payload, generate=False,
                                           now=NOW + timedelta(days=1))
        fetch.assert_not_called()
        claude.assert_not_called()
        self.assertEqual(stats["attached"], 1)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_stale_preview_on_a_carried_over_record_is_dropped(self):
        entry = _entry()
        entry["preview"] = {"overview": "old"}
        self.attach({"city": [entry]}, text=None)
        self.assertNotIn("preview", entry)

    def test_past_meetings_leave_the_cache(self):
        self.cache.write_text(json.dumps({
            "old": {"date": _day(-5), "preview": {"overview": "x"}},
            "kept": {"date": _day(1), "preview": {"overview": "y"}}}), encoding="utf-8")
        self.attach({"city": []})
        self.assertEqual(list(json.loads(self.cache.read_text(encoding="utf-8"))), ["kept"])


class ParseTest(unittest.TestCase):
    def test_fenced_json_and_unknown_actions(self):
        raw = "```json\n" + json.dumps({
            "overview": " A look ahead. ",
            "items": [{"item": "Budget", "why": "Adopts it.", "action": "Vote"},
                      {"item": "Tour", "action": "field trip"},
                      {"why": "no item text"}, "not a dict"],
            "publicComment": "yes", "closedSession": True}) + "\n```"
        p = previews.parse_preview(raw)
        self.assertEqual(p["overview"], "A look ahead.")
        self.assertEqual([i["action"] for i in p["items"]], ["vote", "discussion"])
        self.assertFalse(p["publicComment"])           # only a real true counts
        self.assertTrue(p["closedSession"])

    def test_at_most_six_items(self):
        raw = json.dumps({"overview": "x", "items": [{"item": f"i{n}"} for n in range(9)]})
        self.assertEqual(len(previews.parse_preview(raw)["items"]), 6)

    def test_not_a_preview(self):
        for raw in ("I can't help with that.", "[]", '{"items": []}', '{"overview": "  "}'):
            self.assertIsNone(previews.parse_preview(raw), raw)


class AgendaTextTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_rule_projected_meetings_have_no_agenda_to_read(self):
        county = _entry(source="county", url="https://county.example/calendar")
        self.assertIsNone(previews.agenda_text_for(self.cfg, county))
        projected = _entry(source="schools", url="https://bb.example/Public/Organization/7")
        with mock.patch("engine.adapters.boardbook.fetch_agenda") as fetch:
            self.assertIsNone(previews.agenda_text_for(self.cfg, projected))
        fetch.assert_not_called()

    def test_boardbook_agenda_is_read_by_its_meeting_id(self):
        posted = _entry(source="schools", url="https://bb.example/Public/Agenda/7?meeting=4321")
        with mock.patch("engine.adapters.boardbook.fetch_agenda",
                        return_value={"items": ["Approve the 2027-28 calendar " * 5] * 3}) as fetch:
            text = previews.agenda_text_for(self.cfg, posted)
        self.assertEqual(fetch.call_args.args[1], "4321")
        self.assertIn("2027-28 calendar", text)

    def test_boardbook_attachment_lists_are_dropped(self):
        posted = _entry(source="schools", url="https://bb.example/Public/Agenda/7?meeting=4321")
        items = [f"VI.K.{n}. po55{n:02d} Student Policy Attachments: ( 1 ) po55{n:02d} POLICY "
                 f"10/31/2026 at 4:16 PM | [Attachment: This is the policy text.]" for n in range(40)]
        items += ["X.A. Budget Calendar Presenter: Business Manager Attachments: ( 2 ) Memo.docx",
                  "XIII. The Board will consider adjournment to Closed Session", "[Detail: Description]"]
        with mock.patch("engine.adapters.boardbook.fetch_agenda", return_value={"items": items}):
            text = previews.agenda_text_for(self.cfg, posted)
        self.assertNotIn("Attachment", text)
        self.assertNotIn("[Detail:", text)
        self.assertIn("VI.K.39. po5539 Student Policy", text)
        self.assertTrue(text.endswith("XIII. The Board will consider adjournment to Closed Session"))
        self.assertLess(len(text), 2000)

    def test_municode_agenda_is_found_by_date_and_name(self):
        e = _entry(source="village", name="Village Board Meeting", url="https://elm.example/bc-vb/page/x")
        rows = [{"guid": "aa" * 16, "date": e["date"].replace("-", ""), "name": "Plan Commission"},
                {"guid": None, "date": e["date"].replace("-", ""), "name": "Village Board Meeting"}]
        with mock.patch("engine.adapters.municode.list_meetings", return_value=rows), \
             mock.patch("engine.adapters.municode.fetch_agenda_text") as fetch:
            self.assertIsNone(previews.agenda_text_for(self.cfg, e))   # agenda not posted yet
            fetch.assert_not_called()
            rows[1]["guid"] = "bb" * 16
            fetch.return_value = AGENDA
            self.assertEqual(previews.agenda_text_for(self.cfg, e), AGENDA)
            self.assertEqual(fetch.call_args.args[1], "bb" * 16)

    def test_a_stub_of_an_agenda_is_not_worth_a_call(self):
        with mock.patch("engine.adapters.civicclerk.fetch_agenda_text", return_value="Agenda to follow."):
            self.assertIsNone(previews.agenda_text_for(self.cfg, _entry()))


class PromptTest(unittest.TestCase):
    def test_prompt_carries_the_privacy_rules_and_forbids_prediction(self):
        prompt = previews.build_prompt(make_config(), _entry(), AGENDA)
        self.assertIn("NEVER name a student", prompt)
        self.assertIn("Do not predict outcomes", prompt)
        self.assertIn("City of Oak", prompt)
        self.assertIn("street paving contract", prompt)
        self.assertNotIn("cut off here", prompt)

    def test_a_very_long_agenda_is_cut_and_the_model_is_told(self):
        long_agenda = "Item of business. " * 3000 + "LAST ITEM: closed session"
        prompt = previews.build_prompt(make_config(), _entry(), long_agenda)
        self.assertNotIn("LAST ITEM", prompt)
        self.assertIn("agenda cut off here for length", prompt)


class PromptVersionTest(PreviewTestCase):
    def test_a_prompt_change_rewrites_cached_previews_once(self):
        self.attach({"city": [_entry()]})
        soon = NOW + timedelta(minutes=30)           # well inside the recheck window
        with mock.patch.object(previews, "PROMPT_VERSION", "next"):
            _, _, claude = self.attach({"city": [_entry()]}, now=soon)
            self.assertEqual(claude.call_count, 1)
            _, fetch, claude = self.attach({"city": [_entry()]}, now=soon + timedelta(minutes=30))
            fetch.assert_not_called()
            claude.assert_not_called()

    def test_a_meeting_at_its_cap_is_not_refetched_every_run_after_a_prompt_change(self):
        when = NOW
        for n in range(previews.MAX_GENERATIONS):
            self.attach({"city": [_entry()]}, now=when, text=AGENDA + f"\nAddendum {n}")
            when += timedelta(hours=previews.PREVIEW_RECHECK_HOURS + 1)
        with mock.patch.object(previews, "PROMPT_VERSION", "next"):
            _, fetch, claude = self.attach({"city": [_entry()]}, now=when)
            self.assertEqual((fetch.call_count, claude.call_count), (1, 0))
            _, fetch, _ = self.attach({"city": [_entry()]}, now=when + timedelta(minutes=30))
            fetch.assert_not_called()


class UpcomingWiringTest(unittest.TestCase):
    def test_update_upcoming_writes_previews_beside_the_data_file(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "upcoming.json"
            with mock.patch.object(upcoming, "fetch_for_jurisdiction",
                                   side_effect=lambda c, k: [_entry()] if k == "city" else []), \
                 mock.patch.object(previews, "agenda_text_for", return_value=AGENDA), \
                 mock.patch.object(previews, "call_claude", return_value=REPLY) as claude:
                upcoming.update_upcoming(cfg, data_path=data, previews="generate")
                self.assertEqual(claude.call_count, 1)
                out = json.loads(data.read_text(encoding="utf-8"))
                self.assertIn("preview", out["city"][0])
                self.assertTrue((Path(tmp) / "previews.json").exists())
                # A publish-only rebuild reuses it and calls nothing.
                upcoming.update_upcoming(cfg, data_path=data)
                self.assertEqual(claude.call_count, 1)
                self.assertIn("preview", json.loads(data.read_text(encoding="utf-8"))["city"][0])
                # "off" publishes the schedule alone.
                upcoming.update_upcoming(cfg, data_path=data, previews="off")
                self.assertNotIn("preview", json.loads(data.read_text(encoding="utf-8"))["city"][0])

    def test_a_preview_crash_never_costs_the_schedule(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "upcoming.json"
            with mock.patch.object(upcoming, "fetch_for_jurisdiction",
                                   side_effect=lambda c, k: [_entry()] if k == "city" else []), \
                 mock.patch.object(previews, "attach", side_effect=RuntimeError("boom")):
                upcoming.update_upcoming(cfg, data_path=data, previews="generate")
            self.assertEqual(len(json.loads(data.read_text(encoding="utf-8"))["city"]), 1)


if __name__ == "__main__":
    unittest.main()
