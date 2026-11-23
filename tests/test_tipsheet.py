"""Reporter tip sheet: what counts as a lead, and what never appears in one.
No network, no Claude.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from engine import tipsheet
from helpers import make_config

TODAY = date(2027, 3, 15)


def _long(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _meeting(mid, source="county", days_ago=2, **kw):
    return {"id": mid, "source": source, "title": kw.pop("title", f"Board {mid}"),
            "date": _long(TODAY - timedelta(days=days_ago)), "isAgendaOnly": False,
            "publicComment": "No public comment was offered.", "topics": [], "votes": [], **kw}


def _upcoming(days_ahead=2, source="city", name="Common Council", preview=None):
    e = {"date": (TODAY + timedelta(days=days_ahead)).isoformat(), "time": "6:00 PM",
         "name": name, "url": "https://oak.portal.example/event/9", "source": source}
    if preview:
        e["preview"] = preview
    return e


class ContestedTest(unittest.TestCase):
    def test_what_counts(self):
        cases = [
            ({"outcome": "Approved", "tally": "7-0"}, None),
            ({"outcome": "Approved", "tally": "Unanimous"}, None),
            ({"outcome": "Approved", "tally": None}, None),
            ({"outcome": "Approved", "tally": "Unanimous voice vote"}, None),
            ({"outcome": "Approved", "tally": "4-3"}, "split 4-3"),
            ({"outcome": "Approved", "tally": "4-1 (one trustee opposed)"}, "split 4-1"),
            ({"outcome": "Failed", "tally": "2-9"}, "failed 2-9"),
            ({"outcome": "Failed", "tally": None}, "failed"),
            ({"outcome": "Tabled", "tally": "Unanimous"}, "tabled"),
            ({"outcome": "Approved", "tally": "Not unanimous; exact count not stated"}, "not unanimous"),
            ({"outcome": "Approved", "tally": "Resolution 2026-028"}, None),   # a number, not a tally
        ]
        for vote, want in cases:
            self.assertEqual(tipsheet.contested(vote), want, vote)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def build(self, meetings=(), upcoming=None):
        return tipsheet.build(self.cfg, today=TODAY, meetings=list(meetings), upcoming=upcoming or {})

    def test_decisions_ahead_come_from_previews_votes_and_hearings_only(self):
        preview = {"overview": "x", "publicComment": True, "closedSession": False, "items": [
            {"item": "Paving contract", "why": "Awards it.", "action": "vote"},
            {"item": "Rezoning on Main", "why": "", "action": "hearing"},
            {"item": "Library update", "why": "", "action": "report"}]}
        d = self.build(upcoming={"city": [
            _upcoming(preview=preview),
            _upcoming(name="Reports Only", preview={"overview": "x", "items": [
                {"item": "Update", "action": "report"}]}),
            _upcoming(name="Closed Only", preview={"overview": "x", "items": [], "closedSession": True}),
            _upcoming(name="No Preview"),
            _upcoming(days_ahead=30, name="Far Off", preview=preview)],
            "county": [_upcoming(source="county", name="County Board")]})
        self.assertEqual([m["name"] for m in d["decisions"]], ["Closed Only", "Common Council"])
        council = d["decisions"][1]
        self.assertEqual([i["action"] for i in council["items"]], ["vote", "hearing"])
        self.assertTrue(council["publicComment"])
        self.assertEqual(d["unread"], {"City of Oak": 1, "Riverton County": 1})

    def test_contested_votes_and_crowds_look_back_one_week_at_real_records(self):
        split = {"item": "Adopt the budget", "outcome": "Approved", "tally": "4-3"}
        d = self.build(meetings=[
            _meeting("a", votes=[split, {"item": "Minutes", "outcome": "Approved", "tally": "7-0"}],
                     publicCommentSpeakers=5),
            _meeting("b", publicComment="Three residents spoke about the pool."),
            _meeting("c", publicComment="Two residents spoke."),
            _meeting("old", days_ago=20, votes=[split], publicCommentSpeakers=9),
            _meeting("agenda", isAgendaOnly=True, votes=[split], publicCommentSpeakers=9),
            _meeting("future", days_ago=-3, votes=[split])])
        self.assertEqual([(v["id"], v["why"]) for v in d["votes"]], [("a", "split 4-3")])
        self.assertEqual([(c["id"], c["speakers"]) for c in d["crowds"]], [("a", 5), ("b", 3)])

    def test_recurring_specifics_skip_catch_alls_and_closed_sessions(self):
        d = self.build(meetings=[
            _meeting("a", topics=["Budget", "Highway J Extension"]),
            _meeting("b", source="city", days_ago=20, topics=["Budget", "Highway J Extension"]),
            _meeting("c", topics=["Pool Referendum"]),
            _meeting("d", days_ago=40, topics=["Pool Referendum"]),         # outside 30 days
            _meeting("e", title="Closed Meeting", topics=["Expulsion Hearing"]),
            _meeting("f", title="Closed Meeting", days_ago=5, topics=["Expulsion Hearing"])])
        self.assertEqual([t["topic"] for t in d["topics"]], ["Highway J Extension"])
        self.assertEqual(d["topics"][0]["boards"], ["City of Oak", "Riverton County"])

    def test_overdue_meetings_are_listed(self):
        d = self.build(meetings=[_meeting("late", days_ago=10, isAgendaOnly=True)])
        self.assertEqual([(o["title"], o["waited"]) for o in d["overdue"]], [("Board late", 10)])

    def test_links_go_to_the_static_page_when_there_is_one(self):
        self.assertEqual(tipsheet.meeting_url(self.cfg, "abc"), "https://ledger.example/#m/abc")
        self.cfg.seo = {"pages_base_url": "https://meetings.example/"}
        self.assertEqual(tipsheet.meeting_url(self.cfg, "abc"), "https://meetings.example/meetings/abc/")


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_counts_only_never_who_spoke(self):
        m = _meeting("a", publicComment="Four residents spoke. Pat Example of 12 Elm St opposed it.",
                     votes=[{"item": "Adopt <b>budget</b>", "outcome": "Failed", "tally": "3-4"}])
        d = tipsheet.build(self.cfg, today=TODAY, meetings=[m], upcoming={})
        md, page = tipsheet.render_markdown(d), tipsheet.render_html(self.cfg, d)
        for text in (md, page):
            self.assertIn("4 residents spoke", text)
            self.assertNotIn("Pat Example", text)
            self.assertNotIn("Elm St", text)
        self.assertIn("FAILED 3-4: Adopt &lt;b&gt;budget&lt;/b&gt;", page)   # escaped
        self.assertIn('name="robots" content="noindex, nofollow"', page)

    def test_an_empty_week_says_so_in_every_section(self):
        d = tipsheet.build(self.cfg, today=TODAY, meetings=[], upcoming={})
        md = tipsheet.render_markdown(d)
        self.assertEqual(md.count("## "), 8)
        self.assertIn("Nothing is overdue.", md)
        self.assertIn("Every meeting in the next week has a preview.", md)

    def test_a_vote_names_who_voted_no_where_the_record_does(self):
        split = _meeting("v1", votes=[
            {"item": "Written comment policy", "outcome": "Approved", "tally": "4-3",
             "no": ["Lisa Beck", "Brent Olson", "Katrina Clark"], "abstain": []},
            {"item": "Site plan", "outcome": "Approved", "tally": "5-2"},          # names not on record
            {"item": "Minutes", "outcome": "Approved", "tally": "7-0", "no": []}])
        d = tipsheet.build(self.cfg, today=TODAY, meetings=[split], upcoming={})
        self.assertEqual([(v["item"], v["who"]) for v in d["votes"]],
                         [("Written comment policy", " (no: Lisa Beck, Brent Olson, Katrina Clark)"),
                          ("Site plan", "")])
        md = tipsheet.render_markdown(d)
        self.assertIn("SPLIT 4-3: Written comment policy (no: Lisa Beck, Brent Olson, Katrina Clark)", md)

    def test_portal_vote_records_are_used_when_a_meeting_has_them(self):
        portal = [{"name": '<span style="background-color: rgb(250, 197, 28);">Lead service lines</span>', "votes": [
                       {"motion": "Adopt", "passed": False, "yes": ["A", "B"], "no": ["C", "D", "E"], "abstain": []}],
                   "children": [{"name": "Bond issue", "children": [], "votes": [
                       {"motion": "Approve", "passed": True, "yes": ["A", "B", "C", "D"], "no": ["E"], "abstain": ["F"]}]}]},
                  {"name": "Minutes", "children": [], "votes": [
                      {"motion": "Approve", "passed": True, "yes": ["A", "B", "C", "D", "E"], "no": [], "abstain": []}]},
                  # Listed in the portal with nobody recorded either way: never entered, not failed.
                  {"name": "Reconvene into open session", "children": [], "votes": [
                      {"motion": "", "passed": False, "yes": [], "no": [], "abstain": []}]}]
        m = _meeting("c1", source="city", civicItems=portal,
                     votes=[{"item": "Lead pipes (from the recording)", "outcome": "Failed", "tally": "2-3"}])
        twin = {**m, "id": "c2", "title": "Board c1 Pt.2"}                         # same meeting, second recording
        d = tipsheet.build(self.cfg, today=TODAY, meetings=[m, twin], upcoming={})
        self.assertEqual([(v["item"], v["why"], v["who"]) for v in d["votes"]],
                         [("Lead service lines [Adopt]", "failed 2-3", " (yes: A, B; no: C, D, E)"),
                          ("Bond issue [Approve]", "split 4-1", " (no: E; abstained: F)")])
        # E voted no twice in the month, counted once per vote though two recordings carry it.
        self.assertEqual([(o["member"], o["count"]) for o in d["opposed"]], [("E", 2)])
        self.assertIn("E (City of Oak): 2 no votes", tipsheet.render_markdown(d))

    def test_meetings_are_grouped_under_the_instances_issues(self):
        from pathlib import Path
        from engine.config import InstanceConfig
        from helpers import raw_instance
        raw = raw_instance()
        raw["issues"] = [{"key": "budget_taxes", "label": "Budgets & Taxes"},
                         {"key": "public_safety", "label": "Public Safety"},
                         {"key": "parks_recreation", "label": "Parks & Recreation"}]
        cfg = InstanceConfig(raw, Path("instance.json"))
        meetings = [_meeting("a1", issues=["budget_taxes", "public_safety"]),
                    _meeting("a2", issues=["budget_taxes", "not_on_the_list"], isAgendaOnly=True),
                    _meeting("old", days_ago=20, issues=["parks_recreation"])]
        upcoming = {"city": [_upcoming(preview={"overview": "x", "items": [], "issues": ["public_safety"],
                                                "hearings": ["Rezoning 12 acres for apartments"]})]}
        d = tipsheet.build(cfg, today=TODAY, meetings=meetings, upcoming=upcoming)
        self.assertEqual([(b["issue"], len(b["ahead"]), len(b["past"])) for b in d["by_issue"]],
                         [("Budgets & Taxes", 0, 2), ("Public Safety", 1, 1)])
        md = tipsheet.render_markdown(d)
        self.assertIn("- Public Safety: 1 ahead, 1 in the past week", md)
        self.assertIn("  - AHEAD: Wednesday, Mar 17: City of Oak, Common Council", md)
        # A hearing the preview lists is a decision coming up even when it is not among its items.
        self.assertEqual(d["decisions"][0]["items"],
                         [{"item": "Rezoning 12 acres for apartments", "why": "", "action": "hearing"}])

    def test_same_inputs_same_bytes(self):
        m = [_meeting("a", topics=["Pool Referendum"]), _meeting("b", topics=["Pool Referendum"])]
        one = tipsheet.render_html(self.cfg, tipsheet.build(self.cfg, today=TODAY, meetings=m, upcoming={}))
        two = tipsheet.render_html(self.cfg, tipsheet.build(self.cfg, today=TODAY, meetings=m, upcoming={}))
        self.assertEqual(one, two)


if __name__ == "__main__":
    unittest.main()
