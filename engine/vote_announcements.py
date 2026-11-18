"""
engine.vote_announcements — The chair's vote results, found in a transcript by code.
=================================================================================
"Motion passes 9 to2", "motion failed. 5 to six", "the motion carries
twenty-five to thirteen": when a chair announces a result with a tally, code
can find it without a model. Two uses:

  - summarize_meeting() lists them in the transcript prompt, so the summary's
    votes match what the chair said even where the model would have guessed
    (Sept. 8, 2026: the summary called the lead ordinance "Approved"; the
    chair said "Motion failed. 5 to six.");
  - engine.audit_votes compares each transcript summary's votes against them.

Chairs say it many ways: "Motion passes 9 to2" (Oct. 13, 2026), "Vote passed 11
to zero", "and that passed 11 to zero", "Pass seven to four", and "Past 8 to
three", the captions' spelling of "passed" (July 14). What isn't a result:
  - a span of time: "over the past 3 to 5 years";
  - a vote recalled from elsewhere: "That passed five to zero at finance",
    "that did pass 4 to 1 at the arts commission", "it did pass 5 to 0 is
    what we have recorded".
Results announced without a tally ("motion carries") aren't found; nor are
bodies that don't say tallies aloud (Marathon County's board).
"""

import html
import re

SMALL = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen", "twenty"]
TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
UNITS = SMALL[1:10]
PASSED = r"pass|passes|passed|past|carries|carried|approved|adopted"
FAILED = r"fail|fails|failed|defeated"
ANNOUNCEMENT = re.compile(
    rf"(?P<lead>\S*)\s(?P<outcome>{PASSED}|{FAILED})\b[.,]?\s+(?P<yes>\d+)\s*(?:to|-|–)\s*(?P<no>\d+)\b"
    # A chair who misspeaks corrects it in the same breath: "Motion passes 9 to 11
    # or 11 to zero, sorry" (Oct. 13, 2026). The tally after "or" is the result.
    r"(?:\s+or\s+(?P<yes2>\d+)\s*(?:to|-|–)\s*(?P<no2>\d+))?"
    r"(?:[,.]?\s+with\s+(?P<abstain>\d+)\s+(?:\w+\s+)?(?:abstain\w*|abstention\w*))?")
# After the tally: a unit (a span of time, not a vote), or the place an earlier vote was taken.
NOT_A_RESULT = re.compile(
    r"\s*(?:(?:years?|months?|weeks?|days?|hours?|minutes?|percent|feet|foot|miles?)\b"
    r"|(?:at|in|out of|from|by)\s+(?:the\s+)?(?:\w+\s+)?(?:committee|commission|finance|board|council|ed|hr)\b"
    r"|is what we have recorded|(?:last|earlier|previously)\b)")
TIMESTAMP = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")


def normalize(text: str) -> str:
    """Lowercase; caption '>>' markers (or '&gt;&gt;') and extra spaces gone;
    number words to digits, compounds ("twenty-five") first; "9 to2" → "9 to 2"."""
    text = re.sub(r"\s+", " ", html.unescape(text).replace(">>", " ").lower())
    text = re.sub(r"\b(" + "|".join(TENS) + r")(?:[- ](" + "|".join(UNITS) + r"))?\b",
                  lambda m: str(20 + 10 * TENS.index(m.group(1)) + (UNITS.index(m.group(2)) + 1 if m.group(2) else 0)),
                  text)
    text = re.sub(r"\b(" + "|".join(SMALL) + r")\b", lambda m: str(SMALL.index(m.group(1))), text)
    return re.sub(r"(?<=[a-z])(?=\d)", " ", text)


def announcements(transcript: str) -> list[dict]:
    """Every announced result with a tally, in order: {passed, yes, no, abstain, at, said, check}.
    `at` is the nearest earlier transcript timestamp ("2:32:07"), or None.
    `check` is true when the words can't be right as heard (a pass with yes <= no)."""
    said = normalize(transcript)
    stamps = [(m.start(), m.group(1)) for m in TIMESTAMP.finditer(said)]
    found = []
    for m in ANNOUNCEMENT.finditer(said):
        # "the past 3 to 5", "did pass" (recalling a vote), or a unit or venue after the tally.
        if m.group("lead") in ("the", "did") or NOT_A_RESULT.match(said, m.end()):
            continue
        at = next((s for pos, s in reversed(stamps) if pos < m.start()), None)
        passed = not re.fullmatch(FAILED, m.group("outcome"))
        yes, no = (int(m.group("yes2")), int(m.group("no2"))) if m.group("yes2") else (int(m.group("yes")), int(m.group("no")))
        found.append({
            "passed": passed,
            "yes": yes,
            "no": no,
            "abstain": int(m.group("abstain") or 0),
            "at": at,
            "said": m.group(0).strip(),
            "check": passed and yes <= no,
        })
    return found


def prompt_block(transcript: str) -> str:
    """The announcements as a block for the summarization prompt ('' if none)."""
    found = [a for a in announcements(transcript) if not a["check"]]
    if not found:
        return ""
    lines = "\n".join(f"- [{a['at'] or '?'}] \"{a['said']}\": {'Approved' if a['passed'] else 'Failed'} "
                      f"{a['yes']}-{a['no']}" + (f", {a['abstain']} abstaining" if a["abstain"] else "")
                      for a in found)
    return f"""
VOTE RESULTS THE CHAIR ANNOUNCED (found in the transcript by code, in order, with the nearest timestamp):
{lines}
Each line above is one vote's result as the chair said it. Every one of them belongs in "votes", with that outcome and tally, matched to the item being voted on just before it. Never record a vote as Approved when the chair announced that it failed, and don't drop one because it comes late in the meeting.
"""
