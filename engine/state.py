"""Scraper state — processed_meetings.json and injected_meetings.json.

Semantics carry over unchanged from marathon-meetings (RESHAPE Phase 1 rule):
`processed` maps a meeting ID (YouTube video ID, bb_<id>, kw_<guid>) to its
metadata + summary file path. `injected` tracks which processed IDs have
already been built into data/meetings.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_FILE, INJECTED_FILE, SUMMARIES_DIR


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"processed": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def mark_processed(state: dict, video_id: str, title: str, source: str,
                   summary_path: str, doc_url=None, upload_date=None,
                   meeting_date=None, duration=None, video_url=None) -> None:
    """Record a processed meeting.

    upload_date   = YYYYMMDD from YouTube (often a day after the meeting).
    meeting_date  = YYYYMMDD canonical meeting date (title suffix / portal);
                    publish.py uses this to render the displayed date.
    duration      = video length in seconds; omitted for agenda-only entries.
    video_url     = watch URL for synthetic (bb_/kw_) entries upgraded from a
                    recording — their IDs can't be turned into a URL.
    """
    prior = state["processed"].get(video_id, {})
    state["processed"][video_id] = {
        "title":        title,
        "source":       source,
        "doc_url":      doc_url,
        "upload_date":  upload_date or prior.get("upload_date"),
        "meeting_date": meeting_date or prior.get("meeting_date"),
        "duration":     duration or prior.get("duration"),
        "video_url":    video_url or prior.get("video_url"),
        "processed_at": prior.get("processed_at") or datetime.now(timezone.utc).isoformat(),
        "summary_file": summary_path,
    }


def load_injected() -> set[str]:
    if INJECTED_FILE.exists():
        try:
            return set(json.loads(INJECTED_FILE.read_text(encoding="utf-8")).get("injected", []))
        except json.JSONDecodeError:
            return set()
    return set()


def save_injected(ids: set[str]) -> None:
    INJECTED_FILE.write_text(json.dumps({"injected": sorted(ids)}, indent=2),
                             encoding="utf-8")


def clear_injected(ids: list[str]) -> None:
    """Remove IDs from the injected tracker so publish rebuilds them (used
    after an upgrade pass re-summarizes an entry)."""
    if not ids:
        return
    injected = load_injected()
    injected -= set(ids)
    save_injected(injected)


def prune_orphan_summaries(state: dict) -> None:
    """Delete summary files in SUMMARIES_DIR not referenced from state.

    Summary slugs derive from titles, so a retitle creates a new file and
    orphans the old one; this sweep keeps the directory in sync with state.
    """
    if not SUMMARIES_DIR.exists():
        return
    referenced: set[Path] = set()
    for rec in state.get("processed", {}).values():
        path = rec.get("summary_file")
        if not path:
            continue
        p = Path(path).resolve()
        referenced.add(p)
        stem = p.with_suffix("")
        referenced.add(Path(f"{stem}_summary.json").resolve())
        referenced.add(Path(f"{stem}_votes.json").resolve())
    removed = 0
    for f in SUMMARIES_DIR.iterdir():
        if not f.is_file() or f.suffix not in (".md", ".json"):
            continue
        if f.resolve() not in referenced:
            print(f"  [prune] removing orphan: {f.name}")
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                print(f"  [prune] failed to remove {f.name}: {e}")
    if removed:
        print(f"  [prune] removed {removed} orphan summary file(s).")
