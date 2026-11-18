#!/usr/bin/env python3
"""
audit_votes.py — Check transcript summaries' votes against what the chair said.
================================================================================
For every summary built from a transcript, compares its `votes` with the
results the chair announced with a tally (engine.vote_announcements) and reports:

  - missing: an announced result no summary vote carries (same outcome and
    tally; each summary vote answers for one announcement);
  - contradicted: a summary vote with an announced tally but the opposite
    outcome ("Approved 5-6" where the chair said "Motion failed. 5 to six");
  - cut: the transcript was longer than what reached the model. Until
    Oct. 2026 only the first 90,000 characters did.

Then it prints the IDs to redo: run the pipeline workflow with
`resummarize` set to them (each is one Sonnet call). Nothing is changed here.

Usage:
    python -m engine.audit_votes            # report
    python -m engine.audit_votes --json     # the same, as JSON
    python -m engine.audit_votes --root ../other-checkout
"""

import argparse
import json
import re
import sys
from pathlib import Path

from .vote_announcements import announcements

OLD_LIMIT = 90000  # what reached the model before summaries recorded _transcript_chars
VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")
TALLY = re.compile(r"(\d+)\s*[-–]\s*(\d+)")


def outcome(vote: dict):
    """True for a pass, False for a failure, None for anything else (tabled, withdrawn)."""
    o = str(vote.get("outcome") or "").lower()
    if re.search(r"approv|pass|adopt|carri", o):
        return True
    if re.search(r"fail|defeat|reject|denied", o):
        return False
    return None


def tally(vote: dict):
    """(yes, no), "unanimous", or None."""
    t = str(vote.get("tally") or "")
    m = TALLY.search(t)
    if m:
        return int(m.group(1)), int(m.group(2))
    return "unanimous" if "unanim" in t.lower() else None


def compare(summary_votes: list, announced: list) -> dict:
    """{missing: [announcement], contradicted: [(summary vote, announcement)]}."""
    votes = [(v, outcome(v), tally(v)) for v in summary_votes or [] if isinstance(v, dict)]
    used = set()
    missing = []
    for a in announced:
        hit = next((i for i, (_, passed, t) in enumerate(votes) if i not in used and passed == a["passed"]
                    and (t == (a["yes"], a["no"]) or (t == "unanimous" and a["passed"] and a["no"] == 0))), None)
        if hit is None:
            missing.append(a)
        else:
            used.add(hit)
    contradicted = []
    for i, (v, passed, t) in enumerate(votes):
        if i in used or passed is None or not isinstance(t, tuple):
            continue
        a = next((a for a in missing if (a["yes"], a["no"]) == t and a["passed"] != passed), None)
        if a:
            contradicted.append((v, a))
    return {"missing": missing, "contradicted": contradicted}


def audit(root: Path) -> list[dict]:
    rows = []
    for path in sorted((root / "summaries").glob("*_summary.json")):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m = VIDEO_ID.search(s.get("url") or "")
        if s.get("_source") != "transcript" or not m:
            continue
        tpath = root / "transcripts" / f"{m.group(1)}.txt"
        if not tpath.exists():
            continue
        transcript = tpath.read_text(encoding="utf-8")
        sent = s.get("_transcript_chars", min(len(transcript), OLD_LIMIT))
        found = announcements(transcript)
        result = compare(s.get("votes"), [a for a in found if not a["check"]])
        row = {
            "id": m.group(1),
            "title": s.get("title") or path.stem,
            "chars": len(transcript),
            "sent": sent,
            "cut": sent < len(transcript),
            "announced": len(found),
            "missing": [f"[{a['at'] or '?'}] {a['said']}" for a in result["missing"]],
            "contradicted": [f"summary: {v.get('item', '?')} {v.get('outcome')} {v.get('tally')}; chair: {a['said']}"
                             for v, a in result["contradicted"]],
            "unclear": [f"[{a['at'] or '?'}] {a['said']}" for a in found if a["check"]],
        }
        if row["cut"] or row["missing"] or row["contradicted"] or row["unclear"]:
            rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not (args.root / "summaries").is_dir():
        sys.exit(f"No summaries/ under {args.root}")
    rows = audit(args.root)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        flags = ", ".join(filter(None, [
            r["cut"] and f"cut at {r['sent']:,} of {r['chars']:,} characters",
            r["missing"] and f"{len(r['missing'])} announced result(s) missing",
            r["contradicted"] and f"{len(r['contradicted'])} contradicted",
        ]))
        print(f"{r['id']}  {r['title'][:70]}" + (f"\n  {flags}" if flags else ""))
        for line in r["contradicted"]:
            print(f"  contradicted: {line}")
        for line in r["missing"]:
            print(f"  missing: {line}")
        for line in r["unclear"]:
            print(f"  unclear (check the video): {line}")
    redo = [r["id"] for r in rows if r["cut"] or r["missing"] or r["contradicted"]]
    print(f"\n{len(redo)} summaries to redo." + (f" Run the pipeline workflow with resummarize =\n{','.join(redo)}" if redo else ""))


if __name__ == "__main__":
    main()
