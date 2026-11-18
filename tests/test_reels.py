"""engine.reels: only meetings that became transcript-built during the run, and
only the bodies the instance names. engine.rosters: the exported roster file.
No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from engine import reels, rosters
from engine.config import InstanceConfig
from engine.errors import ConfigError
from helpers import make_config, raw_instance

BODIES = r"\bcity council\b|\bcounty board\b"
REPO_ROOT = Path(__file__).resolve().parent.parent
_REEL_READY = reels.reel_ready       # the real one; the CLI tests patch the module's name


class ReelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "summaries").mkdir()
        (self.root / "transcripts").mkdir()

    def write(self, vid, title, source="transcript", transcript=True):
        (self.root / "summaries" / f"{vid.lower()}_summary.json").write_text(
            json.dumps({"title": title, "url": f"https://www.youtube.com/watch?v={vid}",
                        "_source": source}), encoding="utf-8")
        if transcript:
            (self.root / "transcripts" / f"{vid}.txt").write_text("transcript", encoding="utf-8")

    def ready(self):
        return _REEL_READY(self.root / "summaries", self.root / "transcripts")

    def test_reel_ready_needs_a_transcript_built_summary_and_the_transcript(self):
        self.write("7OLPXH8UYC8", "Wausau City council Meeting")
        self.write("-GFcsOK1Wbs", "Marathon County Board Meeting", source="agenda")
        self.write("AAAAAAAAAAA", "Wausau City Council Meeting", transcript=False)
        self.assertEqual(self.ready(), {"7OLPXH8UYC8": "Wausau City council Meeting"})

    def test_only_the_named_bodies_full_meetings_want_reels(self):
        yes = ["Wausau City council Meeting - 10/13/26", "Marathon County Board Regular Meeting",
               "Marathon County Board Special Meeting - 10/1/26"]
        no = ["Marathon County Board Educational Meeting", "Wausau Finance Committee Meeting",
              "Marathon County Public Safety Committee",
              # Real titles: committees carry the council's name, and split meetings.
              "Wausau City Council Economic Development Committee Meeting - 10/6/26",
              "Wausau City Council Joint Human Resources Committee and Finance Committee Meeting Pt.1 - 9/14/26",
              "Wausau City Council Meeting Pt.2 - 8/18/26",
              "Marathon County Board Regular Meeting Pt.1 - 8/25/26"]
        for title in yes:
            self.assertTrue(reels.wants_reel(title, BODIES), title)
        for title in no:
            self.assertFalse(reels.wants_reel(title, BODIES), title)
        self.assertFalse(reels.wants_reel(yes[0], ""), "no bodies named, no reels")

    def test_only_meetings_that_flipped_this_run_are_new(self):
        now = {"OLDOLDOLD01": "Wausau City Council Meeting", "NEWNEWNEW01": "Wausau City Council Meeting",
               "NEWNEWNEW02": "Wausau Finance Committee Meeting"}
        self.assertEqual(reels.new_candidates({"OLDOLDOLD01"}, now, BODIES), ["NEWNEWNEW01"])

    def run_cli(self, *args, cfg):
        out = io.StringIO()
        with mock.patch.object(reels, "load_instance", return_value=cfg), \
             mock.patch.object(reels, "reel_ready", side_effect=self.ready), \
             mock.patch.object(sys, "argv", ["engine.reels", *args]), redirect_stdout(out):
            reels.main()
        return out.getvalue()

    def test_snapshot_then_since_prints_only_what_is_new(self):
        cfg = InstanceConfig({**raw_instance(), "reels": {"repo": "ledger/reels", "bodies": BODIES}},
                             Path("instance.json"))
        self.write("OLDOLDOLD01", "Riverton City Council Meeting")
        snap = self.root / "snap.json"
        self.assertEqual(self.run_cli("--snapshot", str(snap), cfg=cfg), "")
        self.write("NEWNEWNEW01", "Riverton City Council Meeting")
        self.write("NEWNEWNEW02", "Riverton Finance Committee Meeting")
        self.assertEqual(self.run_cli("--since", str(snap), cfg=cfg), "NEWNEWNEW01\n")
        self.assertEqual(self.run_cli("--repo", cfg=cfg), "ledger/reels\n")

    def test_a_missing_or_malformed_snapshot_prints_nothing(self):
        cfg = InstanceConfig({**raw_instance(), "reels": {"repo": "ledger/reels", "bodies": BODIES}},
                             Path("instance.json"))
        self.write("NEWNEWNEW01", "Riverton City Council Meeting")
        bad = self.root / "bad.json"
        bad.write_text('{"not": "a list"}', encoding="utf-8")
        for since in (self.root / "nope.json", bad):
            self.assertEqual(self.run_cli("--since", str(since), cfg=cfg), "",
                             "a broken snapshot must never send the whole archive")

    def test_an_instance_without_a_reels_block_does_nothing(self):
        cfg = make_config()
        self.write("NEWNEWNEW01", "Riverton City Council Meeting")
        snap = self.root / "snap.json"
        self.assertEqual(self.run_cli("--repo", cfg=cfg), "")
        self.assertEqual(self.run_cli("--snapshot", str(snap), cfg=cfg), "")
        self.assertFalse(snap.exists())
        self.assertEqual(self.run_cli("--since", str(snap), cfg=cfg), "")

    def test_reels_config_is_validated(self):
        for bad in ({"repo": "not a repo"}, {"repo": "a/b", "bodies": "("}, ["a/b"]):
            with self.assertRaises(ConfigError, msg=str(bad)):
                InstanceConfig({**raw_instance(), "reels": bad}, Path("instance.json"))

    def test_the_reference_instance_config_loads_and_names_its_reel_repo(self):
        out = subprocess.run([sys.executable, "-m", "engine.reels", "--repo"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True)
        self.assertRegex(out.stdout.strip(), r"^[\w.-]+/[\w.-]+$")


class RostersTest(unittest.TestCase):
    def test_export_follows_instance_json(self):
        raw = raw_instance()
        raw["jurisdictions"][0]["officials"] = ["Pat Example (Chair)", "Sam Sample (Supervisor, District 2)"]
        data = rosters.build(InstanceConfig(raw, Path("instance.json")))
        self.assertIn("GENERATED", data["_readme"])
        self.assertEqual(data["county"], {"body": "Riverton County",
                                          "officials": ["Pat Example (Chair)",
                                                        "Sam Sample (Supervisor, District 2)"]})
        self.assertEqual(data["city"]["officials"], [])
        self.assertEqual(list(data)[1:], ["county", "schools", "village", "city", "town"])


if __name__ == "__main__":
    unittest.main()
