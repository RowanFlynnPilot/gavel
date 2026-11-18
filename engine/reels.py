"""Which meetings just became reel-ready, for a companion repo's reel drafter.

Optional: only instances with a `reels` block in instance.json use it
({repo: "owner/name", bodies: regex}). The reference instance hands newly
transcribed city council and county board meetings to wpr-reels, which drafts
a short video from each.

A meeting is reel-ready once its summary is built from the full transcript
(`_source: "transcript"`) and the transcript file is in transcripts/. The
workflow snapshots the reel-ready set at the start of a run and compares at
the end; only meetings that flipped during the run are new. (The injected
ledger can't say this: ingest clears a video from it on every run so publish
rebuilds it.)

Only meetings whose title matches `reels.bodies` get reels. Committees don't,
though some are titled "City Council ... Committee Meeting", and neither do
educational sessions. A meeting split into parts ("Pt.1", "Pt.2") isn't
auto-drafted either: each part alone would miss the other's votes, so a person
drafts it and picks the part that carries them.

  python -m engine.reels --snapshot /tmp/reel_ready.json
  python -m engine.reels --since /tmp/reel_ready.json   # prints new IDs, one per line
  python -m engine.reels --repo                         # the configured repo, or nothing
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .config import SUMMARIES_DIR, load_instance

VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")
NOT_A_MEETING = re.compile(
    r"committee|commission|educational|workshop|training|orientation|listening session", re.I)
SPLIT = re.compile(r"\bpt\.?\s*\d|\bpart\s*\d", re.I)


def reel_ready(summaries: Path | None = None, transcripts: Path | None = None) -> dict[str, str]:
    """{video_id: title} for every meeting with a transcript-built summary and its transcript file."""
    summaries = summaries or SUMMARIES_DIR
    transcripts = transcripts or Path("transcripts")
    ready = {}
    for path in sorted(summaries.glob("*_summary.json")):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        m = VIDEO_ID.search(summary.get("url") or "")
        if summary.get("_source") == "transcript" and m and (transcripts / f"{m.group(1)}.txt").exists():
            ready[m.group(1)] = summary.get("title") or ""
    return ready


def wants_reel(title: str, bodies: str) -> bool:
    return bool(bodies) and bool(re.search(bodies, title, re.I)) \
        and not NOT_A_MEETING.search(title) and not SPLIT.search(title)


def new_candidates(before: set[str], now: dict[str, str], bodies: str) -> list[str]:
    return sorted(vid for vid, title in now.items() if vid not in before and wants_reel(title, bodies))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", type=Path, help="write the current reel-ready IDs here")
    group.add_argument("--since", type=Path, help="print reel candidates that weren't in this snapshot")
    group.add_argument("--repo", action="store_true", help="print the configured companion repo")
    args = ap.parse_args()

    reels = load_instance().reels
    if args.repo:
        if reels.get("repo"):
            print(reels["repo"])
        return
    if not reels.get("repo"):
        return                      # no hand-off configured: nothing to snapshot or send
    if args.snapshot:
        args.snapshot.write_text(json.dumps(sorted(reel_ready())), encoding="utf-8")
        return
    # No usable snapshot means no baseline: never treat the whole archive as new.
    try:
        before = json.loads(args.since.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(before, list):
        return
    for vid in new_candidates(set(before), reel_ready(), reels.get("bodies", "")):
        print(vid)


if __name__ == "__main__":
    main()
