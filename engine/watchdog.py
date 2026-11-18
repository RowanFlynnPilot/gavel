"""Stuck-video watchdog — is the residential transcript fetcher still delivering?

    python -m engine.watchdog --print-count --markdown stale_body.md

CI cannot fetch YouTube captions (cloud IPs are blocked), so a video the
pipeline can't process is expected to arrive through the residential fetcher
within about 12 hours. The pipeline records when it first saw each such video
(skipped_videos.json, committed by CI); one still unprocessed after
STALE_VIDEO_HOURS means the fetcher has probably stopped, and its scheduled
task exits 0 even when the script fails internally. The pipeline writes the
stale list into the run report as `stale_unprocessed`; this CLI turns that
into the issue body the workflow posts.

A video leaves the watch list when it is processed, listed in
instance.skip_ids (the operator's lever for a video with no usable captions),
recorded in transcripts/unavailable.json (the fetcher saw YouTube report it
private or removed), or gone from its channel's listing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import RUN_REPORT, SKIPPED_FILE, STALE_VIDEO_HOURS, UNAVAILABLE_FILE

logger = logging.getLogger(__name__)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("%s is not valid JSON (%s) — starting it fresh", path, e)
        return {}


def _save_skipped(skipped: dict) -> None:
    SKIPPED_FILE.write_text(json.dumps(skipped, indent=2) + "\n", encoding="utf-8")


def note_skipped_video(video_id: str, title: str, source_key: str) -> None:
    """Record the first time the pipeline could not process a video."""
    skipped = _load(SKIPPED_FILE)
    if video_id not in skipped:
        skipped[video_id] = {
            "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": title,
            "source": source_key,
        }
        _save_skipped(skipped)


def stale_skipped_videos(processed: dict, skip_ids, listed_ids: dict[str, set],
                         now: datetime | None = None) -> list[dict]:
    """Watched videos still unprocessed after STALE_VIDEO_HOURS, as
    [{id, title, source, age_hours}]. Prunes the watch list first.

    listed_ids maps a jurisdiction key to every video ID its channel listing
    returned this run. A jurisdiction missing from it was not listed (or its
    listing failed), so nothing is concluded about its videos."""
    skipped = _load(SKIPPED_FILE)
    gone = set(_load(UNAVAILABLE_FILE))
    live = {}
    for vid, info in skipped.items():
        if vid in processed or vid in skip_ids or vid in gone:
            continue
        listed = listed_ids.get(info.get("source", ""))
        if listed is not None and vid not in listed:
            print(f"   [gone] {vid} ({info.get('title', '')}) is no longer on its channel "
                  f"(made private or deleted) — dismissing from the watchdog")
            continue
        live[vid] = info
    if len(live) != len(skipped):
        _save_skipped(live)

    now = now or datetime.now(timezone.utc)
    stale = []
    for vid, info in live.items():
        try:
            first = datetime.fromisoformat(info["first_seen"])
        except (KeyError, ValueError):
            continue
        age_h = (now - first).total_seconds() / 3600
        if age_h >= STALE_VIDEO_HOURS:
            stale.append({"id": vid, "title": info.get("title", vid),
                          "source": info.get("source", ""), "age_hours": round(age_h)})
    return stale


def render_markdown(stale: list[dict]) -> str:
    lines = [
        f"These YouTube videos have been unprocessed for over {STALE_VIDEO_HOURS} hours. "
        "CI cannot fetch captions (YouTube blocks cloud IPs), so they depend on the "
        "residential transcript fetcher: the scheduled task on the operator machine that "
        "runs `scripts/refresh-transcripts.ps1` (`python -m engine.fetch_transcripts --all`, "
        "then a push). It normally clears a video within about 12 hours; its per-run logs "
        "are in `logs/` on that machine.",
        "",
    ]
    lines += [f"- [{s['source']}] [{s['title']}](https://www.youtube.com/watch?v={s['id']}) "
              f"— unprocessed for {s['age_hours']}h" for s in stale]
    lines += [
        "",
        "**Check on the operator machine:** is the task running (Task Scheduler, last run "
        "result), and does `python -m engine.fetch_transcripts --all` succeed by hand? A "
        "silent break usually means a Python or yt-dlp environment change. If the fetcher "
        "is fine and a video simply has no captions, add its ID to `instance.skip_ids` in "
        "instance.json to dismiss it. This issue closes itself when nothing is stuck.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report videos stuck past the residential fetcher's normal lag.")
    parser.add_argument("--report", default=str(RUN_REPORT),
                        help="run report written by engine.pipeline")
    parser.add_argument("--markdown", help="write an issue body here when anything is stale")
    parser.add_argument("--print-count", action="store_true",
                        help="print only the stale count on stdout")
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"[err]  No run report at {report_path} — run engine.pipeline first.",
              file=sys.stderr)
        return 1
    stale = json.loads(report_path.read_text(encoding="utf-8")).get("stale_unprocessed", [])

    for s in stale:
        print(f"  stale: {s['age_hours']:4d}h  {s['source']:14s} {s['title'][:60]}",
              file=sys.stderr)
    print(f"{len(stale)} video(s) unprocessed for {STALE_VIDEO_HOURS}h+", file=sys.stderr)

    if args.markdown and stale:
        Path(args.markdown).write_text(render_markdown(stale), encoding="utf-8")
    if args.print_count:
        print(len(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
