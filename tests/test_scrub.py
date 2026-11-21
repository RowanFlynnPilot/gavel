"""Residents' address scrub and the corrections log. No network; file targets
live in a temp directory.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import scrub, summarize
from engine.errors import CorrectionsFileError

PLACES = ["Springfield", "Shelby", "Shelbyville", "IL", "Illinois"]


class AddressScrubTest(unittest.TestCase):
    def setUp(self):
        self.s = scrub.Scrubber(PLACES)

    def test_address_attached_to_a_person_is_removed(self):
        for before, after in [
            ("Jane Doe (123 Elm St)", "Jane Doe"),
            ("Jane Doe of 123 Elm Street, Springfield", "Jane Doe"),
            ("a neighbor at 123 Elm St", "a neighbor"),
            ("Jane Doe (applicant, 123 Elm St) spoke.", "Jane Doe (applicant) spoke."),
            ("Jane Doe, 123 Elm Street, Springfield, IL, opposed the plan.", "Jane Doe opposed the plan."),
            ("Jane Doe of 123 Elm Street, Town of Shelbyville, Illinois, spoke.", "Jane Doe, spoke."),
            ("Jane Doe of 236617 County Road W objected.", "Jane Doe objected."),
            ("Dr. Jane Doe (address given as 4410 N. 28th Avenue) asked about drainage.",
             "Dr. Jane Doe asked about drainage."),
        ]:
            self.assertEqual(self.s.text(before), after, before)

    def test_address_that_is_the_subject_of_business_stays(self):
        for text in [
            "Rezoning of 123 Elm Street to PUD",
            "USM Storage (3107 Elm Street)",
            "Tommy's Tavern (212 Main St) applied for a license.",
            "The committee approved the sale of 500 Grand Avenue.",
            "Jane Doe (Finance Director) presented the budget.",
            "Jane Doe was at 123 Elm Street for the ribbon cutting.",
        ]:
            self.assertEqual(self.s.text(text), text)

    def test_public_comment_treats_a_name_before_an_address_as_a_speaker(self):
        garbled = "Mary CL (3107 Elm Street) opposed the rezoning."
        self.assertEqual(self.s.text(garbled), garbled, "outside public comment the all-caps token reads as a business")
        self.assertEqual(self.s.text(garbled, public_comment=True), "Mary CL opposed the rezoning.")
        self.assertEqual(self.s.text("Jane Doe from 123 Elm Street spoke.", public_comment=True), "Jane Doe spoke.")
        self.assertEqual(self.s.text("USM Storage (3107 Elm Street)", public_comment=True),
                         "USM Storage (3107 Elm Street)", "a listed business word still wins")

    def test_public_comment_address_without_a_street_suffix(self):
        """Captions drop the suffix or the street itself; after a speaker's
        name in public comment the parenthetical is still their address."""
        for before, after in [
            ("Jane Doe (1206 Elm) opposed it.", "Jane Doe opposed it."),
            ("Jane Doe (412 [street name inaudible]) spoke.", "Jane Doe spoke."),
            ("Jane Doe (3305 Oak Hill, Springfield) spoke.", "Jane Doe spoke."),
            ("Jane Doe (2153 [inaudible] Trail) spoke.", "Jane Doe spoke."),
            ("Jane Doe (address given as 2026 [inaudible]) spoke.", "Jane Doe spoke."),
        ]:
            self.assertEqual(self.s.text(before, public_comment=True), after, before)
            self.assertEqual(self.s.text(before), before, "only inside public comment")
        for text in [
            "Jane Doe (51 years a resident) spoke.",
            "Jane Doe (2027 Budget Committee) spoke.",
            "Jane Doe (100 per capita, 90% residential) spoke.",
            "Jane Doe (1206 Elm Church) spoke.",
            "Jane Doe (District 10) spoke.",
        ]:
            self.assertEqual(self.s.text(text, public_comment=True), text)

    def test_place_names_come_from_the_instance(self):
        text = "Jane Doe of 123 Elm Street, Springfield"
        self.assertEqual(scrub.Scrubber([]).text(text), "Jane Doe, Springfield",
                         "with no place names only the address itself is removed")
        self.assertEqual(scrub.Scrubber(["Shelby", "Shelbyville"]).text("Jane Doe of 9 Oak Ct, Shelbyville"),
                         "Jane Doe", "the longer name is tried first")

    def test_government_wording_keys_are_untouched(self):
        addr = "Jane Doe (123 Elm St)"
        record = {
            "title": addr, "overview": addr, "publicComment": "Mary CL (3107 Elm Street) spoke.",
            "agenda": [{"time": "0:10", "item": addr}],
            "discussions": [{"item": addr, "body": addr}],
            "votes": [{"item": addr, "motion": addr, "mover": addr}],
            "actionItems": [addr], "duration": None, "isAgendaOnly": False,
        }
        out = self.s.record(record)
        self.assertEqual(out["title"], addr)
        self.assertEqual(out["agenda"][0]["item"], addr)
        self.assertEqual(out["discussions"][0], {"item": addr, "body": "Jane Doe"})
        self.assertEqual(out["votes"][0], {"item": addr, "motion": addr, "mover": "Jane Doe"})
        self.assertEqual(out["overview"], "Jane Doe")
        self.assertEqual(out["actionItems"], ["Jane Doe"])
        self.assertEqual(out["publicComment"], "Mary CL spoke.")
        self.assertIs(out["duration"], None)

    def test_idempotent(self):
        text = ("Jane Doe (123 Elm St) and John Roe of 45 Oak Avenue, Springfield, spoke; a neighbor at "
                "9 Pine Ct agreed. Rezoning of 123 Elm Street to PUD was tabled.")
        once = self.s.text(text, public_comment=True)
        self.assertNotEqual(once, text)
        self.assertEqual(self.s.text(once, public_comment=True), once)
        self.assertIn("Rezoning of 123 Elm Street to PUD", once)


class PromptRulesTest(unittest.TestCase):
    def _prompt(self, fn, *args, **kw) -> str:
        with mock.patch("engine.summarize.call_claude", return_value="{}") as call:
            fn(*args, org_label="Riverton County", region="Riverton", jurisdiction="county",
               meeting_id="vidA", **kw)
        return call.call_args.kwargs["prompt"]

    def test_transcript_and_minutes_prompts_carry_the_privacy_rules(self):
        for prompt in (self._prompt(summarize.summarize_meeting, "transcript text", "Board", "https://v.example"),
                       self._prompt(summarize.summarize_from_minutes, "minutes text", "Board", "https://v.example")):
            self.assertIn(summarize.RESIDENT_PRIVACY_RULES, prompt)
            self.assertIn("following the privacy rules below", prompt)
            self.assertNotIn("ame names", prompt)
        self.assertIn("NEVER name a student", summarize.RESIDENT_PRIVACY_RULES)
        self.assertIn("home address", summarize.RESIDENT_PRIVACY_RULES)

    def test_transcript_and_minutes_prompts_state_the_meeting_date(self):
        """A recording rarely says the year; without the date the model
        invents one ("met on November 19, 2024" for a 2026 meeting)."""
        for fn in (summarize.summarize_meeting, summarize.summarize_from_minutes):
            dated = self._prompt(fn, "text", "Transit Commission", "https://v.example",
                                 meeting_date="20261119")
            self.assertIn("Meeting date: Thursday, November 19, 2026", dated)
            self.assertLess(dated.index("Meeting date:"), dated.index("Organization:"))
            for unknown in (None, "", "2026-11-19", "20261340"):
                self.assertNotIn("Meeting date:", self._prompt(
                    fn, "text", "Transit Commission", "https://v.example", meeting_date=unknown))
        self.assertIn("Never supply a date or year", self._prompt(
            summarize.summarize_meeting, "text", "Board", "https://v.example"))


class CorrectionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.summaries = self.root / "summaries"
        self.pages = self.root / "pages"
        self.summaries.mkdir()
        targets = {"SUMMARIES_DIR": self.summaries, "MEETINGS_JSON": self.root / "meetings.json",
                   "STATE_FILE": self.root / "processed_meetings.json",
                   "CORRECTIONS_FILE": self.root / "corrections.json"}
        for name, path in targets.items():
            p = mock.patch.object(scrub, name, path)
            p.start()
            self.addCleanup(p.stop)
        self.t = targets

    def _corrections(self, rows):
        self.t["CORRECTIONS_FILE"].write_text(json.dumps(rows), encoding="utf-8")

    def _seed(self):
        """One displayed meeting (bb_1) and one archive-only page (old_1)."""
        summary = {"title": "Regular Meeting - 2026-10-21", "_source": "transcript", "url": "https://v.example/Ava",
                   "overview": "Student representative Ava reported. Jane Doe (123 Elm St) spoke.",
                   "discussions": [{"item": "Student Representative Report (Ava)", "body": "Ava & friends presented."}],
                   "publicComment": "No public comment was offered."}
        sjson = self.summaries / "schools-regular-meeting-bb-1_summary.json"
        sjson.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        md = self.summaries / "schools-regular-meeting-bb-1.md"
        md.write_text("# Regular Meeting\n\nStudent representative Ava reported. Jane Doe (123 Elm St) spoke.\n",
                      encoding="utf-8")
        self.t["STATE_FILE"].write_text(json.dumps({"processed": {"bb_1": {"summary_file": str(md)}}}),
                                        encoding="utf-8")
        meeting = {"id": "bb_1", "source": "schools", "title": "Regular Meeting", "date": "October 21, 2026",
                   "overview": summary["overview"], "discussions": summary["discussions"],
                   "agenda": [{"time": "0:05", "item": "Jane Doe (123 Elm St) appeal"}]}
        self.t["MEETINGS_JSON"].write_text(json.dumps([meeting], indent=2) + "\n", encoding="utf-8")
        for mid, body in (("bb_1", "<h3>Jane Doe (123 Elm St) appeal</h3><p>Ava &amp; friends presented.</p>"),
                          ("old_1", "<p>John Roe (45 Oak Avenue) spoke. Ava &amp; friends presented.</p>")):
            (self.pages / mid).mkdir(parents=True)
            (self.pages / mid / "index.html").write_text(body, encoding="utf-8")
        return sjson, md

    def test_scrub_and_corrections_reach_every_target(self):
        sjson, md = self._seed()
        self._corrections([
            {"id": "bb_1", "find": "Student representative Ava", "replace": "A student representative",
             "date": "2026-11-17", "kind": "privacy", "note": "student named"},
            {"id": "bb_1", "find": "Student Representative Report (Ava)", "replace": "Student Representative Report",
             "date": "2026-11-17", "kind": "privacy", "note": "student named"},
            {"id": "bb_1", "find": "Ava & friends", "replace": "Students", "date": "2026-11-17",
             "kind": "correction", "note": "wording"},
        ])
        changed = scrub.scrub_files(["Springfield"], self.pages)
        self.assertEqual(changed, {"summaries": 1, "mirrors": 1, "meetings": 1, "pages": 2, "corrections": 3})

        summary = json.loads(sjson.read_text(encoding="utf-8"))
        self.assertEqual(summary["overview"], "A student representative reported. Jane Doe spoke.")
        self.assertEqual(summary["discussions"][0], {"item": "Student Representative Report", "body": "Students presented."},
                         "a correction reaches item titles, which the address scrub leaves alone")
        self.assertEqual(summary["url"], "https://v.example/Ava", "links are never edited")
        self.assertIn("A student representative reported. Jane Doe spoke.", md.read_text(encoding="utf-8"))

        meeting = json.loads(self.t["MEETINGS_JSON"].read_text(encoding="utf-8"))[0]
        self.assertEqual(meeting["overview"], "A student representative reported. Jane Doe spoke.")
        self.assertEqual(meeting["agenda"][0]["item"], "Jane Doe (123 Elm St) appeal")

        displayed = (self.pages / "bb_1" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(displayed, "<h3>Jane Doe (123 Elm St) appeal</h3><p>Students presented.</p>",
                         "a displayed page gets corrections (HTML-escaped too) but no raw address scrub")
        archive = (self.pages / "old_1" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(archive, "<p>John Roe spoke. Ava &amp; friends presented.</p>",
                         "an archive page is address-scrubbed; another meeting's corrections don't apply")

        self.assertEqual(scrub.scrub_files(["Springfield"], self.pages),
                         {"summaries": 0, "mirrors": 0, "meetings": 0, "pages": 0, "corrections": 3},
                         "a second run changes nothing")

    def test_mirror_public_comment_section_gets_the_speaker_rules(self):
        sjson, md = self._seed()
        summary = json.loads(sjson.read_text(encoding="utf-8"))
        summary["overview"] = "Jane Doe (1206 Elm) is a committee, not a speaker, out here."
        summary["publicComment"] = "Two spoke. John Roe (1206 Elm) opposed it."
        sjson.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        md.write_text("# Regular Meeting\n\nJane Doe (1206 Elm) is out here.\n\n"
                      "## Public Comment\nTwo spoke. John Roe (1206 Elm) opposed it.\n\n"
                      "## Action Items\n- Hire John Roe (1206 Elm)\n", encoding="utf-8")
        scrub.scrub_files(["Springfield"], self.pages)
        self.assertEqual(json.loads(sjson.read_text(encoding="utf-8"))["publicComment"],
                         "Two spoke. John Roe opposed it.")
        self.assertEqual(md.read_text(encoding="utf-8"),
                         "# Regular Meeting\n\nJane Doe (1206 Elm) is out here.\n\n"
                         "## Public Comment\nTwo spoke. John Roe opposed it.\n\n"
                         "## Action Items\n- Hire John Roe (1206 Elm)\n")

    def test_check_reports_without_writing(self):
        sjson, _md = self._seed()
        before = sjson.read_text(encoding="utf-8")
        changed = scrub.scrub_files([], self.pages, check=True)
        self.assertEqual((changed["summaries"], changed["meetings"], changed["pages"]), (1, 1, 1))
        self.assertEqual(sjson.read_text(encoding="utf-8"), before)

    def test_missing_log_is_empty_and_a_bad_one_fails_loud(self):
        self.assertEqual(scrub.load_corrections(), [])
        for bad in ([{"id": "bb_1", "find": "x"}], [{"find": "x", "replace": "y"}], {"id": "bb_1"},
                    [{"id": "bb_1", "find": "x", "replace": "y", "kind": "typo"}]):
            self._corrections(bad)
            with self.assertRaises(CorrectionsFileError):
                scrub.load_corrections()
        self.t["CORRECTIONS_FILE"].write_text("[not json", encoding="utf-8")
        with self.assertRaises(CorrectionsFileError):
            scrub.load_corrections()

    def test_reference_log_is_valid(self):
        log = Path(__file__).parent.parent / "corrections.json"
        rows = scrub.load_corrections(log)
        self.assertTrue(all(set(r) == {"id", "find", "replace", "date", "kind", "note"} for r in rows))


if __name__ == "__main__":
    unittest.main()
