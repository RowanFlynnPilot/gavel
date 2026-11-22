"""Plain language, the fixed issue list, and votes by member: what the prompts
ask for and what is kept of the answer. No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from engine import pages, previews, publish, summarize
from engine.config import InstanceConfig
from engine.errors import ConfigError
from helpers import make_config, raw_instance

ISSUES = [{"key": "budget_taxes", "label": "Budgets & Taxes", "about": "budgets, levies"},
          {"key": "public_safety", "label": "Public Safety"},
          {"key": "roads_transportation", "label": "Roads & Transportation", "about": "streets"}]
COMMON = dict(org_label="City of Oak", region="Riverton", jurisdiction="city", meeting_id="m1")


def _ask(fn, *args, reply: dict, **kw):
    """Call a summarize function with Claude replaced; returns (result, prompt)."""
    with mock.patch.object(summarize, "call_claude", return_value=json.dumps(reply)) as claude:
        result = fn(*args, **kw)
    return result, claude.call_args.kwargs["prompt"]


class PromptTest(unittest.TestCase):
    def test_every_prompt_carries_the_plain_language_rules(self):
        calls = [
            (summarize.summarize_meeting, ("transcript text", "Council", "u"), COMMON),
            (summarize.summarize_from_minutes, ("minutes text", "Council", "u"), COMMON),
            (summarize.summarize_from_agenda, ("agenda text", "Council", "u"), COMMON),
            (summarize.summarize_from_agenda_with_votes, ("agenda text", "Council", "u", {"items": []}), COMMON),
            (summarize.summarize_from_boardbook,
             ({"items": ["1. Call to order"], "agenda_url": "a", "packet_url": "p"}, "Regular Meeting"),
             dict(district_label="Riverton Schools", newsroom="Ledger", jurisdiction="schools", meeting_id="m1")),
        ]
        for fn, args, kw in calls:
            _, prompt = _ask(fn, *args, reply={"overview": "x"}, **kw)
            self.assertIn("Write for a neighbor, not an insider.", prompt, fn.__name__)
            self.assertIn("Plain is not vague.", prompt, fn.__name__)
            self.assertNotIn('"issues"', prompt, fn.__name__)        # no list, no field
            _, prompt = _ask(fn, *args, reply={"overview": "x"}, issues=ISSUES, **kw)
            self.assertIn('"issues": ["one to three keys', prompt, fn.__name__)
            self.assertIn("- budget_taxes: Budgets & Taxes (budgets, levies)", prompt, fn.__name__)
            self.assertIn("- public_safety: Public Safety\n", prompt, fn.__name__)

    def test_only_the_record_prompts_ask_who_voted_no(self):
        for fn, args, word in ((summarize.summarize_meeting, ("t", "Council", "u"), "the transcript says"),
                               (summarize.summarize_from_minutes, ("m", "Council", "u"), "the minutes record")):
            _, prompt = _ask(fn, *args, reply={"overview": "x"}, **COMMON)
            self.assertIn('"no": ["members who voted no"]', prompt)
            self.assertIn('"attendance": {"present"', prompt)
            self.assertIn(f"name a member ONLY when {word} how that member voted", prompt)
            self.assertIn("Never work out who voted no from the tally", prompt)
        _, prompt = _ask(summarize.summarize_from_agenda, "a", "Council", "u",
                         reply={"overview": "x"}, **COMMON)
        self.assertNotIn("voted no", prompt)
        self.assertNotIn("attendance", prompt)

    def test_preview_prompt_and_parse_use_the_issue_list(self):
        raw = raw_instance()
        raw["issues"] = ISSUES
        cfg = InstanceConfig(raw, Path("instance.json"))
        entry = {"date": "2027-03-09", "time": "6:00 PM", "name": "Common Council", "source": "city"}
        prompt = previews.build_prompt(cfg, entry, "1. Paving contract")
        self.assertIn("Write for a neighbor, not an insider.", prompt)
        self.assertIn("- roads_transportation: Roads & Transportation (streets)", prompt)
        reply = json.dumps({"overview": "The council is scheduled to vote on paving.", "items": [],
                            "issues": ["roads_transportation", "made_up"]})
        self.assertEqual(previews.parse_preview(reply, cfg.issues)["issues"], ["roads_transportation"])
        self.assertNotIn("issues", previews.parse_preview(reply))
        self.assertNotIn('"issues"', previews.build_prompt(make_config(), entry, "1. Paving"))


class AnswerTest(unittest.TestCase):
    def summary(self, reply, **kw):
        result, _ = _ask(summarize.summarize_meeting, "t", "Council", "u", reply=reply, **COMMON, **kw)
        return result

    def test_issue_keys_off_the_list_are_dropped_and_three_is_the_most(self):
        got = self.summary({"overview": "x", "issues": ["public_safety", "weather", "budget_taxes",
                                                        "public_safety", "roads_transportation", 7]},
                           issues=ISSUES)
        self.assertEqual(got["issues"], ["public_safety", "budget_taxes", "roads_transportation"])
        self.assertEqual(self.summary({"overview": "x", "issues": "budget_taxes"}, issues=ISSUES)["issues"], [])
        self.assertNotIn("issues", self.summary({"overview": "x", "issues": ["budget_taxes"]}))

    def test_names_on_a_no_vote_must_agree_with_the_tally(self):
        votes = [
            {"item": "Lead pipes", "outcome": "Failed", "tally": "5-6",
             "no": ["A", "B", "C", "D", "E", "F"], "abstain": []},
            {"item": "Paving", "outcome": "Approved", "tally": "9 to 2", "no": ["A"]},          # one of two
            {"item": "Minutes", "outcome": "Approved", "tally": "Unanimous", "no": ["A"]},
            {"item": "Budget", "outcome": "Approved", "tally": None, "no": ["A", " "], "abstain": "B"},
            {"item": "Rezoning", "outcome": "Approved", "tally": "7-3-1", "no": ["A", "B", "C"], "abstain": ["D"]},
        ]
        with self.assertLogs("engine.summarize", level="WARNING") as logs:
            got = self.summary({"overview": "x", "votes": votes,
                                "attendance": {"present": ["A", "B", ""], "absent": None}})["votes"]
        self.assertEqual(len(got[0]["no"]), 6)
        self.assertEqual(got[1]["no"], [], "a name count that disagrees with the tally is not published")
        self.assertEqual(got[2]["no"], [])
        self.assertEqual((got[3]["no"], got[3]["abstain"]), (["A"], []))
        self.assertEqual((got[4]["no"], got[4]["abstain"]), (["A", "B", "C"], ["D"]))
        self.assertEqual(len(logs.output), 2)

    def test_attendance_is_two_lists_of_names_or_absent(self):
        got = self.summary({"overview": "x", "votes": [], "attendance": {"present": ["A", "B", ""], "absent": None}})
        self.assertEqual(got["attendance"], {"present": ["A", "B"], "absent": []})
        self.assertNotIn("attendance", self.summary({"overview": "x", "attendance": ["A"]}))

    def test_an_agenda_summary_is_not_given_vote_fields(self):
        got, _ = _ask(summarize.summarize_from_agenda, "a", "Council", "u",
                      reply={"overview": "x", "attendance": {"present": ["A"]}}, issues=ISSUES, **COMMON)
        self.assertEqual(got["issues"], [])
        self.assertEqual(got["attendance"], {"present": ["A"]}, "left as the model gave it; publish ignores it")


class DownstreamTest(unittest.TestCase):
    INFO = {"title": "Common Council - 2027-03-09", "source": "city", "meeting_date": "20270309",
            "doc_url": "https://oak.portal.example/event/9/files"}

    def test_publish_carries_issues_and_attendance_when_the_summary_has_them(self):
        cfg = make_config()
        summary = {"overview": "x", "_source": "minutes", "agenda": [], "issues": ["budget_taxes"],
                   "attendance": {"present": ["A"], "absent": []},
                   "votes": [{"item": "Budget", "outcome": "Approved", "tally": "4-1", "no": ["B"], "abstain": []}]}
        m = publish.build_meeting(cfg, "cc_9", self.INFO, summary, None)
        self.assertEqual(m["issues"], ["budget_taxes"])
        self.assertEqual(m["attendance"], {"present": ["A"], "absent": []})
        self.assertEqual(m["votes"][0]["no"], ["B"])
        old = publish.build_meeting(cfg, "cc_9", self.INFO, {"overview": "x", "_source": "minutes", "agenda": []}, None)
        self.assertNotIn("issues", old)
        self.assertNotIn("attendance", old)
        agenda = publish.build_meeting(cfg, "cc_9", self.INFO,
                                       {"overview": "x", "_source": "agenda", "agenda": [], "issues": ["budget_taxes"],
                                        "attendance": {"present": ["A"]}}, None)
        self.assertEqual(agenda["issues"], ["budget_taxes"])
        self.assertNotIn("attendance", agenda, "an agenda says nothing about who came")

    def test_archive_page_names_who_voted_no(self):
        cfg = make_config()
        m = {"id": "cc_9", "source": "city", "title": "Common Council", "date": "March 9, 2027",
             "overview": "x", "isAgendaOnly": False,
             "votes": [{"item": "Budget", "outcome": "Approved", "tally": "4-1", "mover": "A",
                        "no": ["Pat Doe"], "abstain": ["Lee Roe"]},
                       {"item": "Minutes", "outcome": "Approved", "tally": "Unanimous", "no": []}]}
        page = pages.render_page(cfg, m, "https://pages.example/tracker", False)
        self.assertIn("Moved by A · Voted no: Pat Doe · Abstained: Lee Roe", page)
        self.assertEqual(page.count("Voted no"), 1)


class ConfigTest(unittest.TestCase):
    def cfg(self, issues):
        raw = raw_instance()
        raw["issues"] = issues
        return InstanceConfig(raw, Path("instance.json"))

    def test_issue_list_is_optional_and_validated(self):
        self.assertEqual(make_config().issues, [])
        self.assertEqual(len(self.cfg(ISSUES).issues), 3)
        for bad in ("budget", [{"label": "No key"}], [{"key": "Budget Taxes", "label": "x"}],
                    [{"key": "budget", "label": ""}], [{"key": "budget", "label": "x", "about": 3}],
                    [{"key": "a", "label": "x"}, {"key": "a", "label": "y"}], ["budget"]):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.cfg(bad)

    def test_the_reference_instance_has_a_valid_issue_list(self):
        repo = Path(__file__).resolve().parent.parent
        raw = json.loads((repo / "instance.json").read_text(encoding="utf-8"))
        cfg = InstanceConfig(raw, repo / "instance.json")
        self.assertGreaterEqual(len(cfg.issues), 8)
        self.assertTrue(all(i.get("about") for i in cfg.issues))


if __name__ == "__main__":
    unittest.main()
