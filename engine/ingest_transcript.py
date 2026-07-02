"""Manually inject a transcript for an agenda-only meeting.

    python -m engine.ingest_transcript --video-id rQcjCEY36e4 --transcript t.txt
    cat t.txt | python -m engine.ingest_transcript --video-id rQcjCEY36e4 --stdin

Re-summarizes the meeting from the real transcript (Sonnet tier), updates
state, and re-publishes data/meetings.json. A sha256 hash of the transcript
is stored in the summary sidecar so unchanged files are skipped on
subsequent runs (no wasted API calls).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import publish as publish_mod
from . import summarize
from .adapters import boardbook as bb_adapter
from .adapters import youtube
from .config import (MEETINGS_JSON, STATE_FILE, SUMMARIES_DIR, load_instance,
                     setup_logging)
from .output import save_summary
from .state import clear_injected, prune_orphan_summaries


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Re-summarize an agenda-only meeting from a pasted transcript.")
    parser.add_argument("--video-id", required=True,
                        help="YouTube video ID or synthetic ID (bb_719203, kw_<guid>)")
    parser.add_argument("--transcript", default=None,
                        help="Path to a text file containing the transcript")
    parser.add_argument("--stdin", action="store_true",
                        help="Read transcript from stdin instead of a file")
    parser.add_argument("--source", default=None,
                        help="Jurisdiction key (auto-detected from state if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Summarize but don't update files")
    parser.add_argument("--force", action="store_true",
                        help="Re-summarize even if a transcript-based summary exists")
    parser.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                        help="Force the meeting date (overrides yt-dlp upload_date)")
    args = parser.parse_args()

    cfg = load_instance()
    vid_id = args.video_id

    # ── Skip-guard: resolve existing summary via state, compare hashes ───────
    transcript_hash = None
    if args.transcript and Path(args.transcript).exists():
        try:
            transcript_hash = hashlib.sha256(
                Path(args.transcript).read_bytes()).hexdigest()[:16]
        except OSError:
            pass

    existing_summaries: list[Path] = []
    if STATE_FILE.exists():
        try:
            _info = json.loads(STATE_FILE.read_text(encoding="utf-8")) \
                        .get("processed", {}).get(vid_id, {})
            sf = _info.get("summary_file", "")
            if sf:
                sj = Path(sf.replace(".md", "_summary.json"))
                if sj.exists():
                    existing_summaries.append(sj)
        except (json.JSONDecodeError, OSError):
            pass
    if not existing_summaries and SUMMARIES_DIR.exists():
        existing_summaries = list(SUMMARIES_DIR.glob(f"*{vid_id.lower()}*_summary.json"))

    if existing_summaries and not args.force:
        for sp in existing_summaries:
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            stored_hash = s.get("_transcript_hash")
            # Anything not explicitly agenda-based counts as transcript-based
            # (older summaries predate the _source tag) — EXCEPT bb_ entries,
            # whose untagged BoardBook agenda summaries are agenda-based until
            # explicitly upgraded to "transcript".
            if vid_id.startswith("bb_"):
                is_agenda = s.get("_source") != "transcript"
            else:
                is_agenda = s.get("_source") in ("agenda", "agenda_with_votes")
            if not is_agenda:
                if transcript_hash and stored_hash and transcript_hash == stored_hash:
                    print(f"[skip] {vid_id} already summarized (transcript unchanged, saves API call)")
                    return
                if transcript_hash and stored_hash and transcript_hash != stored_hash:
                    print(f"[update] {vid_id} transcript changed — re-summarizing")
                    break
                print(f"[skip] {vid_id} already has a transcript-based summary ({sp.name})")
                print("       Use --force to re-summarize.")
                return

    # ── Read transcript ──────────────────────────────────────────────────────
    if args.stdin:
        print("[read] Reading transcript from stdin...")
        transcript = sys.stdin.read()
    elif args.transcript:
        print(f"[read] Reading transcript from {args.transcript}...")
        transcript = Path(args.transcript).read_text(encoding="utf-8")
    else:
        print("Error: Provide --transcript FILE or --stdin")
        sys.exit(1)

    transcript = transcript.strip()
    if len(transcript) < 100:
        print(f"Error: Transcript too short ({len(transcript)} chars). Need at least 100.")
        sys.exit(1)
    print(f"[ok]  Transcript: {len(transcript):,} characters")

    # ── Look up meeting metadata ─────────────────────────────────────────────
    title = vid_id
    source_key = args.source
    doc_url = None
    url = f"https://www.youtube.com/watch?v={vid_id}"

    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        info = state.get("processed", {}).get(vid_id)
        if info:
            title = info.get("title", title)
            title = re.sub(r'(\s*-\s*\d{1,2}/\d{1,2}/\d{2,4})+$', '', title).strip()
            source_key = source_key or info.get("source")
            doc_url = info.get("doc_url")
            # Synthetic municode IDs aren't YouTube videos — use the stored
            # meeting page URL instead of a bogus watch?v= link.
            if not vid_id.startswith("bb_") and "_" in vid_id and info.get("video_url"):
                url = info["video_url"]
            print(f"[ok]  Found in state: [{source_key}] {title}")
        else:
            print(f"[warn] Video {vid_id} not in {STATE_FILE.name}")

    meeting_date_from_json = None
    if not source_key or title == vid_id:
        if MEETINGS_JSON.exists():
            try:
                meetings_list = json.loads(MEETINGS_JSON.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meetings_list = []
            entry = next((m for m in meetings_list
                          if isinstance(m, dict) and m.get("id") == vid_id), None)
            if entry:
                if not source_key and entry.get("source"):
                    source_key = entry["source"]
                    print(f"[ok]  Detected source from meetings.json: {source_key}")
                if title == vid_id and entry.get("title"):
                    title = re.sub(r'(\s*-\s*\d{1,2}/\d{1,2}/\d{2,4})+$', '',
                                   entry["title"]).strip()
                    print(f"[ok]  Detected title from meetings.json: {title}")
                if entry.get("date"):
                    meeting_date_from_json = entry["date"]
                if not doc_url and entry.get("docUrl"):
                    doc_url = entry["docUrl"]

    # ── Resolve upload_date and meeting_date ────────────────────────────────
    upload_date = None
    meeting_date = None
    if args.date:
        try:
            meeting_date = datetime.strptime(args.date, "%Y-%m-%d").strftime("%Y%m%d")
            print(f"[ok]  meeting_date from --date flag: {meeting_date}")
        except ValueError:
            print(f"[error] --date must be YYYY-MM-DD, got: {args.date}")
            sys.exit(2)
    if STATE_FILE.exists():
        try:
            _existing = json.loads(STATE_FILE.read_text(encoding="utf-8")) \
                            .get("processed", {}).get(vid_id, {})
            upload_date = _existing.get("upload_date")
            if not meeting_date:
                meeting_date = _existing.get("meeting_date")
        except json.JSONDecodeError:
            pass

    is_synthetic = "_" in vid_id and not re.match(r"^[A-Za-z0-9_-]{11}$", vid_id)
    if not is_synthetic:
        print("[fetch] Querying YouTube for authoritative title and date...")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--no-check-certificate",
                 "--dump-json", "--skip-download", url],
                capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                meta = json.loads(r.stdout)
                yt_title = meta.get("title", "")
                yt_upload_date = meta.get("upload_date", "")
                if yt_upload_date and len(yt_upload_date) == 8 and yt_upload_date.isdigit():
                    upload_date = yt_upload_date
                if not args.date and yt_title:
                    tm = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", yt_title)
                    if tm:
                        mo, dy, yr = tm.groups()
                        yr = "20" + yr if len(yr) == 2 else yr
                        try:
                            meeting_date = datetime(int(yr), int(mo), int(dy)).strftime("%Y%m%d")
                        except ValueError:
                            pass
                    if not meeting_date:
                        tm = re.search(r"(20\d{2})-(\d{2})-(\d{2})", yt_title)
                        if tm:
                            meeting_date = "".join(tm.groups())
                if yt_title and title == vid_id:
                    cleaned = re.sub(r'\s*-\s*\d{1,2}/\d{1,2}/\d{2,4}$', '', yt_title).strip()
                    cleaned = re.sub(r'\s*-\s*\d{4}-\d{2}-\d{2}$', '', cleaned).strip()
                    cleaned = re.sub(r'\s+Pt\.\d+$', '', cleaned).strip()
                    title = cleaned
                    print(f"[ok]  Title from YouTube: {title}")
        except Exception as e:
            print(f"[warn] Could not fetch from YouTube: {e}")

    if not meeting_date and meeting_date_from_json:
        try:
            d = datetime.strptime(meeting_date_from_json, "%B %d, %Y")
            if d.date() != datetime.now().date():
                meeting_date = d.strftime("%Y%m%d")
        except (ValueError, TypeError):
            pass

    # For bb_ meetings, look up the district recording so the card links to
    # the actual video. Channel LISTING works from CI IPs (only caption
    # downloads are bot-blocked).
    video_url = None
    video_duration = None
    if vid_id.startswith("bb_") and source_key:
        try:
            jur = cfg.jurisdiction(source_key)
            if jur.video:
                _minfo = {"title": title, "meeting_date": meeting_date or ""}
                v = bb_adapter.match_recording(
                    _minfo, youtube.list_videos(jur.video, source_key, jur.name))
                if v:
                    video_url = v["url"]
                    video_duration = v.get("duration")
                    url = video_url
                    print(f"[ok]  Matched district recording: {v['id']} ({v['title'][:60]})")
        except Exception as e:
            print(f"[warn] Recording match failed: {str(e)[:120]}")

    if not source_key:
        print(f"Error: Cannot detect source. Use --source with one of: "
              f"{', '.join(sorted(cfg.by_key))}")
        sys.exit(1)
    jur = cfg.jurisdiction(source_key)

    # ── Summarize ────────────────────────────────────────────────────────────
    print(f"\n[claude] Summarizing from transcript ({len(transcript):,} chars)...")
    summary = summarize.summarize_meeting(
        transcript, title, url, org_label=jur.name, region=cfg.region,
        jurisdiction=source_key, meeting_id=vid_id)
    if not summary:
        print("Error: Claude returned no summary.")
        sys.exit(1)
    print("[ok]  Summary generated:")
    print(f"       Overview: {summary.get('overview', '')[:120]}...")
    print(f"       Discussions: {len(summary.get('discussions', []))} items")
    print(f"       Action items: {len(summary.get('actionItems', []))}")

    civic_data = None
    if jur.agendas_adapter == "civicclerk" and doc_url:
        from .adapters import civicclerk
        print("[fetch] Getting CivicClerk vote data...")
        civic_data = civicclerk.fetch_meeting_data(jur.agendas, doc_url)
        if civic_data:
            print(f"[ok]  Got {len(civic_data.get('items', []))} agenda items with votes")

    if args.dry_run:
        print("\n[DRY RUN] Would save summary and re-publish. Exiting.")
        print(json.dumps(summary, indent=2)[:2000])
        return

    # ── Save + update state ──────────────────────────────────────────────────
    if transcript_hash:
        summary["_transcript_hash"] = transcript_hash
    path = save_summary(title, url, source_key, jur.name, summary,
                        doc_url=doc_url, civic_data=civic_data, video_id=vid_id)
    print(f"[save] Saved: {path}")

    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {"processed": {}}

    prior = state["processed"].get(vid_id, {})
    state["processed"][vid_id] = {
        "title": title,
        "source": source_key,
        "doc_url": doc_url,
        "upload_date": upload_date or prior.get("upload_date"),
        "meeting_date": meeting_date or prior.get("meeting_date"),
        "duration": video_duration or prior.get("duration"),
        "video_url": video_url or prior.get("video_url"),
        # Preserve the original processed_at so re-running over the same
        # transcript doesn't drift the meeting date to "now".
        "processed_at": prior.get("processed_at") or datetime.now(timezone.utc).isoformat(),
        "summary_file": str(path),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[save] Updated {STATE_FILE}")

    prune_orphan_summaries(state)
    clear_injected([vid_id])

    print("\n[publish] Rebuilding data/meetings.json...")
    publish_mod.publish(cfg)
    print(f"\n[ok]  Done! Meeting {vid_id} re-summarized from transcript.")


if __name__ == "__main__":
    main()
