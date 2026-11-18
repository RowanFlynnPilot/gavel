"""Tests for engine.vote_announcements and engine.audit_votes (no API calls).

  python -m pytest tests/test_vote_announcements.py
"""

import pytest

from engine.audit_votes import compare
from engine.vote_announcements import announcements, prompt_block

# Lines as the Sept. 8 and Oct. 13, 2026 Wausau council transcripts have them.
SEPT8 = (
    "[26:09] Motion passes 11 to zero. Right. Under ordinances and resolutions. "
    "Voting is now open. motion failed. 0 to 11. Right. Up next is a is resolution "
    "&gt;&gt; Thanks, Motion passes 10 to 1. All right. "
    "is That passed five to zero at finance. Okay. "
    "[2:06:01] Voting is now open. Motion failed. 5 to six. | Mr. Mayor "
    "[2:32:07] possible. Motion passes seven to four. All right. "
    "&gt;&gt; Motion passes 10 to one. Okay."
)


def tallies(text):
    return [(a["passed"], a["yes"], a["no"], a["abstain"]) for a in announcements(text)]


def test_reads_each_result_the_chair_announced():
    assert tallies(SEPT8) == [(True, 11, 0, 0), (False, 0, 11, 0), (True, 10, 1, 0),
                              (False, 5, 6, 0), (True, 7, 4, 0), (True, 10, 1, 0)]


@pytest.mark.parametrize("text", [
    "is That passed five to zero at finance.",
    "uh that did pass 4 to 1 at the arts commission. so they have",
    "I believe it did pass 5 to 0 is what we have recorded.",
    "we've seen over the past 4 to 5 years.",
])
def test_recalled_votes_and_spans_of_time_are_not_results(text):
    assert announcements(text) == []


def test_july_14_style_announcements():
    # The chair didn't say "motion"; the captions spell "passed" as "past".
    text = ("Voting is now open. Vote passed 11 to zero. Up next. Voting is now open. past 8 to three. Okay. "
            "Voting is open. >> Passed 9 to2. >> Okay. Voting is now open. Pass seven to four. All right. "
            "Voting was opened and it has passed 11 to zero.")
    assert tallies(text) == [(True, 11, 0, 0), (True, 8, 3, 0), (True, 9, 2, 0), (True, 7, 4, 0), (True, 11, 0, 0)]


def test_timestamps_and_spellings():
    a = announcements(SEPT8)
    assert [x["at"] for x in a][-3:] == ["2:06:01", "2:32:07", "2:32:07"]
    assert tallies("the motion carries twenty-five to thirteen.") == [(True, 25, 13, 0)]
    assert tallies("Motion passes 9 to2.") == [(True, 9, 2, 0)]


def test_abstentions_and_a_corrected_tally():
    assert tallies("Motion passes 9 to one with one abstaining.") == [(True, 9, 1, 1)]
    assert tallies("Motion passes 9 to 11 or 11 to zero, sorry.") == [(True, 11, 0, 0)]


def test_an_impossible_pass_is_flagged_and_left_out_of_the_prompt():
    a = announcements("Voting is now open. Motion passes 9 to 11. Next.")
    assert a[0]["check"] is True
    assert prompt_block("Voting is now open. Motion passes 9 to 11. Next.") == ""


def test_prompt_block_lists_every_result():
    block = prompt_block(SEPT8)
    assert '"motion failed. 5 to 6": Failed 5-6' in block
    assert block.count("\n- [") == 6
    assert prompt_block("All in favor? Motion carries.") == ""


@pytest.mark.parametrize("votes, missing, contradicted", [
    # Sept. 8's summary: the lead ordinance "Approved" with no tally; the 7-4 and second 10-1 absent.
    ([{"outcome": "Approved", "tally": "11-0"}, {"outcome": "Failed", "tally": "0-11"},
      {"outcome": "Approved", "tally": "10-1"}, {"item": "Lead ordinance", "outcome": "Approved", "tally": None}],
     ["motion failed. 5 to 6", "motion passes 7 to 4", "motion passes 10 to 1"], 0),
    # A summary that got the outcome backward.
    ([{"outcome": "Approved", "tally": "11-0"}, {"outcome": "Failed", "tally": "0-11"}, {"outcome": "Approved", "tally": "10-1"},
      {"outcome": "Approved", "tally": "5-6"}, {"outcome": "Approved", "tally": "7-4"}, {"outcome": "Approved", "tally": "10-1"}],
     ["motion failed. 5 to 6"], 1),
    # Everything there; "Unanimous" answers for an 11-0.
    ([{"outcome": "Approved", "tally": "Unanimous"}, {"outcome": "Failed", "tally": "0-11"}, {"outcome": "Passed", "tally": "10-1"},
      {"outcome": "Failed", "tally": "5-6"}, {"outcome": "Approved", "tally": "7-4"}, {"outcome": "Approved", "tally": "10–1"}],
     [], 0),
])
def test_compare(votes, missing, contradicted):
    result = compare(votes, announcements(SEPT8))
    assert [a["said"] for a in result["missing"]] == missing
    assert len(result["contradicted"]) == contradicted
