"""Public-comment speaker counts.

The grant evaluation compares residents speaking per meeting, by board,
before and after a board joins the tracker. Summaries made from a recording
or minutes carry the count as `publicCommentSpeakers` (the prompts ask for
it). Summaries written before that field existed describe public comment
only in prose; `count_from_prose` reads the count out of that prose where it
is stated and returns None where it isn't, so a report can say how many
meetings the count is known for instead of guessing.
"""

from __future__ import annotations

import re

__all__ = ["count_from_prose", "speaker_count"]

_WORDS = {
    "no": 0, "zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "a single": 1, "a dozen": 12, "both": 2,
}
_NUM = r"(?:\d{1,3}|" + "|".join(sorted((re.escape(w) for w in _WORDS), key=len, reverse=True)) + r")"
_WHO = (r"(?:(?:public |in-person |online |remote )?(?:speakers?|commenters?)|residents?|people|"
        r"persons?|individuals?|citizens?|members of the public|community members?|"
        r"attendees?|parents?)")
_SPOKE = (r"(?:spoke|speaks|addressed|offered|commented|testified|appeared|signed up|"
          r"provided|gave|made|raised|shared|submitted|voiced|asked|urged)")

# A headline count: "Three speakers addressed the board", "One resident,
# identified as ..., spoke". The speaking verb must follow within the same
# clause, which keeps out "asked two individuals if they had anything to say"
# and "three calls for public comment".
_COUNT = re.compile(
    r"(?<![\w$#])(?P<n>" + _NUM + r")\s+(?:\w+\s+){0,2}?" + _WHO +
    r"\b(?P<gap>[^.;]{0,60}?)\b" + _SPOKE + r"\b", re.IGNORECASE)

# "Two written public comments were submitted" — comments received count as
# participants even when read into the record by the clerk.
_COMMENTS = re.compile(
    r"(?<![\w$#])(?P<n>" + _NUM + r")\s+(?:written |public |online |emailed ){0,3}comments?\s+"
    r"(?:were|was)\s+(?:received|offered|submitted|made|read)", re.IGNORECASE)

# Nobody spoke.
_NONE = re.compile(
    r"\bno (?:general |formal |other )?(?:public )?comments?\b|"
    r"\bno one (?:spoke|addressed|offered|commented|signed up|wished)\b|"
    r"\bnobody (?:spoke|offered)\b|\bno (?:speakers?|residents?|members of the public)\b|"
    r"\bnone (?:was|were) (?:offered|received|made)\b|\bno public input\b|"
    r"\bdid not (?:offer|speak|comment)\b|\bneither offered\b",
    re.IGNORECASE)


def _to_int(token: str) -> int | None:
    token = token.lower().strip()
    if token.isdigit():
        return int(token)
    return _WORDS.get(token)


def count_from_prose(text: str | None) -> int | None:
    """Speakers stated in a public-comment description, or None when the
    prose doesn't state a count.

    Descriptions lead with the total ("Eight speakers signed up ... seven
    spoke on the ordinance, two on ..."), so the first headline count is the
    answer and later numbers are its breakdown. An explicit "no public
    comment was offered" wins unless a headline count opens the text.
    """
    if not text or not text.strip():
        return None
    first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    if _NONE.search(first_sentence):
        return 0
    c = _COMMENTS.search(first_sentence)
    if c and _to_int(c.group("n")) is not None:
        return _to_int(c.group("n"))
    m = _COUNT.search(text)
    if m and "not" not in m.group("gap").lower().split():
        n = _to_int(m.group("n"))
        if n is not None and m.start() <= len(first_sentence):
            return n
        if n is not None and not _NONE.search(text):
            return n
    if _NONE.search(text):
        return 0
    return None


def speaker_count(summary: dict) -> int | None:
    """A summary's speaker count: the field when the model supplied one,
    else read from the prose. Agenda-only summaries have no count."""
    if summary.get("_source") not in ("transcript", "minutes", None):
        return None
    n = summary.get("publicCommentSpeakers")
    if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
        return n
    return count_from_prose(summary.get("publicComment"))
