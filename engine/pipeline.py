"""Pipeline orchestrator — config in → data/*.json out.

    python -m engine.pipeline                    # new meetings (cron mode)
    python -m engine.pipeline --source wausau    # one jurisdiction
    python -m engine.pipeline --url URL          # one specific video
    python -m engine.pipeline --backfill         # all historical videos
    python -m engine.pipeline --days N           # last N days only
    python -m engine.pipeline --dry-run          # preview without processing
    python -m engine.pipeline --publish-only     # rebuild data/*.json from
                                                 # existing state, no scraping

Steps: validate instance.json → scrape per jurisdiction → summarize new
meetings → upgrade passes (caption retry, BoardBook recordings, Municode
minutes) → publish data/meetings.json → refresh data/upcoming.json.
Costs are recorded per Anthropic call in data/costs.json (engine.claude).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import publish as publish_mod
from . import summarize, upcoming
from .adapters import boardbook as bb_adapter
from .adapters import get_adapter, youtube
from .config import (AGENDA_RETRY_DAYS, InstanceConfig, load_instance,
                     setup_logging)
from .errors import AdapterStructureError, TranscriptAuthError
from .output import save_summary
from .state import (clear_injected, load_state, mark_processed,
                    prune_orphan_summaries, save_state)
from .transcripts import NoCaptionsError, build_whisper_hint, fetch_transcript

logger = logging.getLogger(__name__)


# ── Agenda-text fallback dispatch ─────────────────────────────────────────────

def _fetch_agenda_text(cfg: InstanceConfig, source_key: str,
                       doc_url: str | None, title: str,
                       description: str = "") -> str | None:
    """Agenda text for a video with no transcript, via the jurisdiction's
    agendas adapter. rule_schedule jurisdictions have no agenda source —
    fall back to the video description as context."""
    jur = cfg.jurisdiction(source_key)
    adapter_name = jur.agendas_adapter
    print("  [agenda]  Trying agenda document fallback...")

    if adapter_name == "civicclerk" and doc_url:
        from .adapters import civicclerk
        text = civicclerk.fetch_agenda_text(jur.agendas, doc_url)
        if text:
            print(f"       Agenda text from CivicClerk ({len(text)} chars)")
            return text

    if adapter_name == "agendacenter":
        from .adapters import agendacenter
        text = agendacenter.fetch_agenda_text(jur.agendas, doc_url, title)
        if text:
            print(f"       Agenda text from AgendaCenter ({len(text)} chars)")
            return text

    if adapter_name == "rule_schedule":
        if description and len(description) > 20:
            stub = f"Meeting: {title}\nOrganization: {jur.name}\n\n{description}"
            print(f"       No agenda source - using title + description ({len(stub)} chars)")
            return stub
        print("       No agenda source and no description available")

    return None


# ── Video processing (YouTube-first jurisdictions) ────────────────────────────

def process_video(cfg: InstanceConfig, video: dict,
                  auth_failures: list) -> str | None:
    vid_id, title, url, source_key = (
        video["id"], video["title"], video["url"], video["source"])
    doc_url = video.get("doc_url")
    description = video.get("description", "")
    upload_date = video.get("upload_date", "")
    jur = cfg.jurisdiction(source_key)

    print(f"\n{'-'*60}")
    print(f"[{jur.name}] {title}")
    print(f"  {url}")
    if doc_url:
        print(f"   {doc_url}")

    summary = None
    try:
        print("  [dl]  Fetching transcript...")
        transcript = fetch_transcript(url, source_key=source_key,
                                      upload_date=upload_date,
                                      whisper_hint=build_whisper_hint(jur.name, jur.officials))
        print(f"  [ok]  {len(transcript):,} characters")
        print("  [claude]  Summarizing from transcript...")
        summary = summarize.summarize_meeting(
            transcript, title, url, org_label=jur.name, region=cfg.region,
            jurisdiction=source_key, meeting_id=vid_id, officials=jur.officials)
    except TranscriptAuthError as e:
        print(f"  [AUTH]  {e}")
        auth_failures.append(e)
    except NoCaptionsError as e:
        print(f"  [warn]  No transcript: {e}")
    except Exception as e:
        print(f"  [warn]  Transcript error: {e}")

    # Pre-fetch enrichment before agenda fallback.
    if jur.agendas_adapter == "agendacenter" and not doc_url:
        from .adapters import agendacenter
        print("    Looking up agenda document...")
        doc_url = agendacenter.fetch_doc_url(jur.agendas, title)
        if doc_url:
            print(f"    Found: {doc_url}")

    civic_data = None
    if jur.agendas_adapter == "civicclerk" and doc_url:
        from .adapters import civicclerk
        print("    Fetching CivicClerk vote data...")
        civic_data = civicclerk.fetch_meeting_data(jur.agendas, doc_url)
        if civic_data:
            print(f"  [ok]  Got {len(civic_data.get('items', []))} agenda items with votes")

    if summary is None:
        agenda_text = _fetch_agenda_text(cfg, source_key, doc_url, title, description)
        if agenda_text:
            if civic_data and civic_data.get("items"):
                print("  [claude]  Summarizing from agenda + CivicClerk vote data...")
                summary = summarize.summarize_from_agenda_with_votes(
                    agenda_text, title, url, civic_data, org_label=jur.name,
                    region=cfg.region, jurisdiction=source_key, meeting_id=vid_id)
            else:
                print("  [claude]  Summarizing from agenda document...")
                summary = summarize.summarize_from_agenda(
                    agenda_text, title, url, org_label=jur.name,
                    region=cfg.region, jurisdiction=source_key, meeting_id=vid_id)
        else:
            print("  [err]  No transcript or agenda available - skipping.")
            return None

    path = save_summary(title, url, source_key, jur.name, summary,
                        doc_url=doc_url, civic_data=civic_data, video_id=vid_id)
    print(f"  [save]  Saved  {path}")
    # doc_url may have been discovered above — propagate to the caller's record.
    video["doc_url"] = doc_url
    return path


# ── BoardBook (meeting-creating) processing ───────────────────────────────────

def process_boardbook_meeting(cfg: InstanceConfig, source_key: str,
                              bb_meeting: dict, state: dict) -> bool:
    jur = cfg.jurisdiction(source_key)
    meeting_id = bb_meeting["meeting_id"]
    video_id = f"bb_{meeting_id}"
    if video_id in state.get("processed", {}):
        return False

    title = f"{bb_meeting['name']} - {bb_meeting['date']}"
    print(f"  [agenda]  Scraping BoardBook agenda for: {title}")
    agenda = bb_adapter.fetch_agenda(jur.agendas, meeting_id)

    # Ceremonial quorum notices (graduations etc.) aren't meetings — skip
    # before summarizing; don't mark processed so a retitled/expanded agenda
    # would still get picked up.
    _agenda_blob = " ".join(agenda.get("items", []))[:2000].lower()
    if re.search(r"graduation", f"{bb_meeting['name']} {_agenda_blob}", re.IGNORECASE) \
            and "no board action" in _agenda_blob:
        print(f"  [skip]  Ceremonial event (graduation, no Board action): {title}")
        return False

    print(f"  [claude]  Summarizing {len(agenda['items'])} agenda items...")
    summary = summarize.summarize_from_boardbook(
        agenda, title, district_label=jur.agendas["label"],
        newsroom=cfg.newsroom, jurisdiction=source_key, meeting_id=video_id)

    doc_url = bb_meeting.get("url", bb_meeting.get("agenda_url", ""))
    path = save_summary(title, doc_url, source_key, jur.name, summary,
                        doc_url=doc_url, video_id=video_id)
    bb_date = (bb_meeting.get("date") or "").replace("-", "") or None
    mark_processed(state, video_id, title, source_key, path,
                   doc_url=doc_url, upload_date=bb_date, meeting_date=bb_date)
    print(f"  [ok]  Saved: {path}")
    return True


def fetch_boardbook_new(cfg: InstanceConfig, source_key: str, state: dict,
                        dry_run: bool = False) -> int:
    jur = cfg.jurisdiction(source_key)
    today = date.today()
    print(f"[fetch]  Checking BoardBook for new {jur.name} meetings...")
    meetings = bb_adapter.list_meetings(jur.agendas)
    count = 0

    cutoff_date = None
    if cfg.date_cutoff:
        try:
            cutoff_date = date(int(cfg.date_cutoff[:4]),
                               int(cfg.date_cutoff[4:6]),
                               int(cfg.date_cutoff[6:8]))
        except ValueError:
            pass

    for m in meetings[:20]:
        try:
            meeting_date = date.fromisoformat(m.get("date", ""))
            if meeting_date > today:
                print(f"  [skip] Future meeting skipped: {m['name']} on {m['date']}")
                continue
            if cutoff_date and meeting_date < cutoff_date:
                continue
        except ValueError:
            pass

        video_id = f"bb_{m['meeting_id']}"
        if video_id in state.get("processed", {}) or video_id in cfg.skip_ids:
            continue
        print(f"  [new]  New meeting: {m['name']} on {m.get('date','')}")
        if not dry_run:
            if process_boardbook_meeting(cfg, source_key, m, state):
                count += 1
    return count


# ── Municode (meeting-creating) processing ────────────────────────────────────

def process_municode_meeting(cfg: InstanceConfig, source_key: str, m: dict,
                             state: dict) -> bool:
    """Minutes if posted (actual outcomes, Sonnet), else ADA agenda text
    (scheduled language, Haiku)."""
    from .adapters import municode
    from .pdftext import fetch_pdf_text

    jur = cfg.jurisdiction(source_key)
    if not m.get("guid"):
        return False
    prefix = jur.agendas["id_prefix"]
    video_id = f"{prefix}_{m['guid']}"
    if video_id in state.get("processed", {}) or video_id in cfg.skip_ids:
        return False

    date_iso = f"{m['date'][:4]}-{m['date'][4:6]}-{m['date'][6:8]}"
    title = f"{m['name']} - {date_iso}"

    summary = None
    if m.get("minutes_pdf"):
        print(f"  [minutes]  Fetching minutes PDF for: {title}")
        minutes_text = fetch_pdf_text(m["minutes_pdf"])
        if minutes_text:
            print(f"  [claude]  Summarizing from minutes ({len(minutes_text):,} chars)...")
            summary = summarize.summarize_from_minutes(
                minutes_text, title, m["detail_url"], org_label=jur.name,
                region=cfg.region, jurisdiction=source_key, meeting_id=video_id,
                officials=jur.officials)
    if summary is None:
        print(f"  [agenda]  Fetching ADA agenda text for: {title}")
        agenda_text = municode.fetch_agenda_text(jur.agendas, m["guid"])
        if not agenda_text:
            print(f"  [warn]  No agenda text available — skipping {title}")
            return False
        print(f"  [claude]  Summarizing from agenda ({len(agenda_text):,} chars)...")
        summary = summarize.summarize_from_agenda(
            agenda_text, title, m["detail_url"], org_label=jur.name,
            region=cfg.region, jurisdiction=source_key, meeting_id=video_id)

    path = save_summary(title, m["detail_url"], source_key, jur.name, summary,
                        doc_url=m.get("agenda_pdf"), video_id=video_id)
    mark_processed(state, video_id, title, source_key, path,
                   doc_url=m.get("agenda_pdf"),
                   upload_date=m["date"], meeting_date=m["date"],
                   video_url=m["detail_url"])
    print(f"  [ok]  Saved: {path}")
    return True


def fetch_municode_new(cfg: InstanceConfig, source_key: str, state: dict,
                       dry_run: bool = False) -> int:
    from .adapters import municode

    jur = cfg.jurisdiction(source_key)
    today_str = date.today().strftime("%Y%m%d")
    print(f"[fetch]  Checking Municode hub for new {jur.name} meetings...")
    meetings = municode.list_meetings(jur.agendas)
    prefix = jur.agendas["id_prefix"]
    count = 0
    for m in meetings:
        if m["cancelled"] or m["date"] > today_str or not m.get("guid"):
            continue
        if cfg.date_cutoff and m["date"] < cfg.date_cutoff:
            continue
        video_id = f"{prefix}_{m['guid']}"
        if video_id in state.get("processed", {}) or video_id in cfg.skip_ids:
            continue
        print(f"  [new]  New meeting: {m['name']} on {m['date']}")
        if not dry_run:
            if process_municode_meeting(cfg, source_key, m, state):
                count += 1
    return count


# ── Upgrade passes ────────────────────────────────────────────────────────────

def _summary_source(info: dict) -> tuple[str | None, Path | None]:
    """Return (existing summary's _source tag, sidecar path) or (None, None)."""
    sf = info.get("summary_file", "")
    sjson = Path(sf.replace(".md", "_summary.json")) if sf else None
    if not sjson or not sjson.exists():
        return None, None
    try:
        existing = json.loads(sjson.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, None
    return existing.get("_source"), sjson


def retry_agenda_only_captions(cfg: InstanceConfig, state: dict,
                               sources: list[str], auth_failures: list) -> list[str]:
    """Re-try transcript fetch for recently-uploaded agenda-only YouTube
    entries — auto-captions can take hours/days to appear."""
    synthetic_prefixes = tuple(
        {"bb_"} | {f"{j.agendas['id_prefix']}_" for j in cfg.jurisdictions
                   if j.agendas_adapter == "municode"})
    candidates = []
    for vid_id, info in list(state.get("processed", {}).items()):
        if vid_id.startswith(synthetic_prefixes):
            continue  # synthetic IDs — no YouTube video to retry
        if info.get("source") not in sources:
            continue
        upload = info.get("upload_date") or ""
        if not upload or len(upload) != 8 or not upload.isdigit():
            continue
        try:
            upload_dt = datetime.strptime(upload, "%Y%m%d")
        except ValueError:
            continue
        age_days = (datetime.now() - upload_dt).days
        if age_days < 0 or age_days > AGENDA_RETRY_DAYS:
            continue
        src_tag, _ = _summary_source(info)
        if src_tag not in ("agenda", "agenda_with_votes"):
            continue
        candidates.append({
            "id": vid_id,
            "title": info["title"],
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "source": info["source"],
            "doc_url": info.get("doc_url"),
            "upload_date": upload,
            "meeting_date": info.get("meeting_date"),
            "duration": info.get("duration"),
        })

    if candidates:
        print(f"\n[retry]  {len(candidates)} agenda-only entry/entries within "
              f"last {AGENDA_RETRY_DAYS} days — re-trying transcript fetch:")
        for v in candidates:
            print(f"   {v['source']}: {v['title']}")

    upgraded = []
    for v in candidates:
        jur = cfg.jurisdiction(v["source"])
        try:
            transcript = fetch_transcript(v["url"], source_key=v["source"],
                                          upload_date=v.get("upload_date", ""),
                                          whisper_hint=build_whisper_hint(jur.name, jur.officials))
        except TranscriptAuthError as e:
            auth_failures.append(e)
            continue
        except NoCaptionsError:
            continue
        except Exception as e:
            logger.warning("retry: transcript fetch crashed for %s: %s", v["id"], e)
            continue
        if not transcript or len(transcript) < 200:
            continue
        print(f"   [retry-ok] transcript found for {v['id']} "
              f"({len(transcript):,} chars) — re-summarizing")
        try:
            summary = summarize.summarize_meeting(
                transcript, v["title"], v["url"], org_label=jur.name,
                region=cfg.region, jurisdiction=v["source"], meeting_id=v["id"],
                officials=jur.officials)
        except Exception as e:
            logger.warning("retry: summarization crashed for %s: %s", v["id"], e)
            continue
        new_path = save_summary(v["title"], v["url"], v["source"], jur.name,
                                summary, doc_url=v.get("doc_url"), video_id=v["id"])
        mark_processed(state, v["id"], v["title"], v["source"], new_path,
                       doc_url=v.get("doc_url"), upload_date=v.get("upload_date"),
                       meeting_date=v.get("meeting_date"), duration=v.get("duration"))
        save_state(state)
        upgraded.append(v["id"])

    if candidates:
        print(f"[retry]  Upgraded {len(upgraded)}/{len(candidates)} "
              f"entry/entries to transcript-based summaries.")
    return upgraded


def upgrade_boardbook_from_recordings(cfg: InstanceConfig, source_key: str,
                                      state: dict, auth_failures: list) -> list[str]:
    """Match recent agenda-only BoardBook entries to recordings on the
    district's YouTube channel and re-summarize from the transcript."""
    jur = cfg.jurisdiction(source_key)
    if not jur.video:
        return []
    window = jur.video.get("upgrade_window_days", 45)

    candidates = []
    for vid_id, info in list(state.get("processed", {}).items()):
        if not vid_id.startswith("bb_") or info.get("source") != source_key:
            continue
        mdate = info.get("meeting_date") or youtube.parse_date_from_title(info.get("title", ""))
        if not (len(mdate) == 8 and mdate.isdigit()):
            continue
        try:
            age_days = (datetime.now() - datetime.strptime(mdate, "%Y%m%d")).days
        except ValueError:
            continue
        if age_days < 0 or age_days > window:
            continue
        src_tag, sjson = _summary_source(info)
        if sjson is None or src_tag == "transcript":
            continue
        candidates.append((vid_id, info))

    if not candidates:
        return []
    print(f"\n[bb-video]  {len(candidates)} {source_key} agenda-only "
          f"entry/entries within {window} days — checking YouTube for recordings...")
    try:
        district_videos = youtube.list_videos(jur.video, source_key, jur.name)
    except Exception as e:
        logger.warning("%s channel fetch failed: %s", source_key, e)
        district_videos = []

    upgraded = []
    for vid_id, info in candidates:
        video = bb_adapter.match_recording(info, district_videos)
        if not video:
            continue
        print(f"   [match] {info['title']} -> {video['id']} ({video['title'][:60]})")
        try:
            transcript = fetch_transcript(video["url"], source_key=source_key,
                                          upload_date=video.get("upload_date", ""),
                                          whisper_hint=build_whisper_hint(jur.name, jur.officials))
        except TranscriptAuthError as e:
            auth_failures.append(e)
            continue
        except Exception as e:
            print(f"   [warn]  transcript fetch failed: {str(e)[:120]}")
            continue
        if not transcript or len(transcript) < 200:
            continue
        try:
            summary = summarize.summarize_meeting(
                transcript, info["title"], video["url"], org_label=jur.name,
                region=cfg.region, jurisdiction=source_key, meeting_id=vid_id,
                officials=jur.officials)
        except Exception as e:
            logger.warning("bb-video: summarization failed for %s: %s", vid_id, e)
            continue
        new_path = save_summary(info["title"], video["url"], source_key,
                                jur.name, summary, doc_url=info.get("doc_url"),
                                video_id=vid_id)
        mark_processed(state, vid_id, info["title"], source_key, new_path,
                       doc_url=info.get("doc_url"),
                       upload_date=info.get("upload_date"),
                       meeting_date=info.get("meeting_date"),
                       duration=video.get("duration"), video_url=video["url"])
        save_state(state)
        upgraded.append(vid_id)
        print(f"   [ok]  upgraded {vid_id} from recording ({len(transcript):,} chars)")
    return upgraded


def upgrade_municode_from_minutes(cfg: InstanceConfig, source_key: str,
                                  state: dict) -> list[str]:
    """Re-summarize recent agenda-only Municode entries once official
    minutes post — they record actual motions and votes."""
    from .adapters import municode
    from .pdftext import fetch_pdf_text

    jur = cfg.jurisdiction(source_key)
    window = jur.agendas.get("minutes_upgrade_window_days", 45)
    prefix = jur.agendas["id_prefix"]

    candidates = []
    for vid_id, info in list(state.get("processed", {}).items()):
        if not vid_id.startswith(f"{prefix}_") or info.get("source") != source_key:
            continue
        mdate = info.get("meeting_date") or ""
        if not (len(mdate) == 8 and mdate.isdigit()):
            continue
        try:
            age_days = (datetime.now() - datetime.strptime(mdate, "%Y%m%d")).days
        except ValueError:
            continue
        if age_days < 0 or age_days > window:
            continue
        src_tag, sjson = _summary_source(info)
        if sjson is None or src_tag in ("minutes", "transcript"):
            continue
        candidates.append((vid_id, info))

    if not candidates:
        return []
    print(f"\n[minutes]  {len(candidates)} {source_key} agenda-only "
          f"entry/entries within {window} days — checking for posted minutes...")
    try:
        hub = {f"{prefix}_{m['guid']}": m
               for m in municode.list_meetings(jur.agendas) if m.get("guid")}
    except Exception as e:
        logger.warning("%s hub fetch failed: %s", source_key, e)
        hub = {}

    upgraded = []
    for vid_id, info in candidates:
        m = hub.get(vid_id)
        if not m or not m.get("minutes_pdf"):
            continue
        print(f"   [minutes] posted for: {info['title']}")
        minutes_text = fetch_pdf_text(m["minutes_pdf"])
        if not minutes_text:
            continue
        try:
            summary = summarize.summarize_from_minutes(
                minutes_text, info["title"], m["detail_url"], org_label=jur.name,
                region=cfg.region, jurisdiction=source_key, meeting_id=vid_id,
                officials=jur.officials)
        except Exception as e:
            logger.warning("minutes: summarization failed for %s: %s", vid_id, e)
            continue
        new_path = save_summary(info["title"], m["detail_url"], source_key,
                                jur.name, summary, doc_url=info.get("doc_url"),
                                video_id=vid_id)
        mark_processed(state, vid_id, info["title"], source_key, new_path,
                       doc_url=info.get("doc_url"),
                       upload_date=info.get("upload_date"),
                       meeting_date=info.get("meeting_date"),
                       video_url=m["detail_url"])
        save_state(state)
        upgraded.append(vid_id)
        print(f"   [ok]  upgraded {vid_id} from minutes ({len(minutes_text):,} chars)")
    return upgraded


# ── Agenda-only report (CI creates a GitHub Issue from this) ──────────────────

def write_agenda_only_report(cfg: InstanceConfig, state: dict, ok: int) -> None:
    agenda_only, transcript_based = [], []
    for vid, info in state.get("processed", {}).items():
        src_tag, sjson = _summary_source(info)
        if sjson is None:
            continue
        entry = {"id": vid, "title": info["title"], "source": info["source"],
                 "url": info.get("doc_url") or f"https://www.youtube.com/watch?v={vid}",
                 "_source": src_tag or "transcript"}
        if src_tag in ("agenda", "agenda_with_votes"):
            agenda_only.append(entry)
        else:
            transcript_based.append(entry)

    if not agenda_only:
        print(f"\n[ok]  All {ok} meetings have transcripts — no agenda-only fallbacks.")
        return

    print(f"\n{'='*60}")
    print(f"[warn]  AGENDA-ONLY MEETINGS ({len(agenda_only)}):")
    print("   These meetings lack transcripts — summaries are based on")
    print("   agenda documents only. Paste a transcript to improve them.")
    for m in agenda_only:
        label = cfg.by_key.get(m["source"])
        label = label.name if label else m["source"]
        vote_note = " (has vote data)" if m["_source"] == "agenda_with_votes" else ""
        print(f"   [{label}] {m['title']}{vote_note}")
        print(f"            {m['url']}")

    report = {
        "agenda_only": agenda_only,
        "transcript_based": [{"id": m["id"], "title": m["title"], "source": m["source"]}
                             for m in transcript_based],
        "total_processed": ok,
    }
    report_path = Path("./agenda_only_report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n   Report saved: {report_path.resolve()}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Gavel pipeline — civic meeting scrape + summarize + publish")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--url", metavar="URL", help="Process a single video URL")
    group.add_argument("--backfill", action="store_true",
                       help="Process all historical videos")
    group.add_argument("--days", type=int, metavar="N",
                       help="Process videos from the last N days only")
    group.add_argument("--publish-only", action="store_true",
                       help="Rebuild data/*.json from existing state; no scraping")
    parser.add_argument("--source", default="all",
                        help="Jurisdiction key to process (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without processing")
    parser.add_argument("--skip-upcoming", action="store_true",
                        help="Skip the data/upcoming.json refresh")
    args = parser.parse_args()

    cfg = load_instance()

    if args.source != "all" and args.source not in cfg.by_key:
        print(f"[err]  Unknown jurisdiction '{args.source}' "
              f"(known: {', '.join(sorted(cfg.by_key))})")
        sys.exit(1)
    sources = [j.key for j in cfg.jurisdictions] if args.source == "all" else [args.source]

    if args.publish_only:
        publish_mod.publish(cfg)
        if not args.skip_upcoming:
            upcoming.update_upcoming(cfg, sources=sources)
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[err]  ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    state = load_state()
    save_state(state)  # ensure the file exists even if nothing is processed

    auth_failures: list[TranscriptAuthError] = []
    structure_failures: list[Exception] = []

    # Which jurisdictions are YouTube-first vs meeting-creating?
    youtube_first = [k for k in sources
                     if cfg.by_key[k].video is not None
                     and cfg.by_key[k].agendas_adapter not in ("boardbook", "municode")]
    meeting_creating = [k for k in sources
                        if cfg.by_key[k].agendas_adapter in ("boardbook", "municode")]

    # ── Single URL mode ────────────────────────────────────────────────────
    if args.url:
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", args.url)
        if not m:
            print(f"[err]  Could not parse video ID from: {args.url}")
            sys.exit(1)
        vid_id = m.group(1)
        if vid_id in state["processed"] and not args.dry_run:
            info = state["processed"][vid_id]
            print(f"  Already processed: {info['title']}\n   {info['summary_file']}")
            return
        # Find the video in a jurisdiction's channel listing.
        video = None
        for key in youtube_first:
            jur = cfg.by_key[key]
            videos = youtube.list_videos(jur.video, key, jur.name)
            video = next((v for v in videos if v["id"] == vid_id), None)
            if video:
                break
        if video is None:
            if args.source == "all":
                print("[err]  Video not found in any configured channel — "
                      "pass --source KEY to force a jurisdiction.")
                sys.exit(1)
            video = {"id": vid_id, "title": f"Meeting {vid_id}", "url": args.url,
                     "source": args.source, "doc_url": None}
        if args.dry_run:
            print(f"[DRY RUN] Would process: {video['title']}")
            return
        path = process_video(cfg, video, auth_failures)
        if path:
            mark_processed(state, vid_id, video["title"], video["source"], path,
                           video.get("doc_url"),
                           upload_date=video.get("upload_date"),
                           meeting_date=video.get("meeting_date"),
                           duration=video.get("duration"))
            save_state(state)
        return

    # ── Channel mode ───────────────────────────────────────────────────────
    from datetime import timedelta
    cutoff_date = None
    if args.days:
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y%m%d")
        print(f"[date]  Limiting to videos uploaded on or after {cutoff_date} "
              f"(last {args.days} days)")

    all_pending = []
    for src in youtube_first:
        jur = cfg.by_key[src]
        videos = youtube.list_videos(jur.video, src, jur.name,
                                     dateafter=cutoff_date or "")
        if args.backfill:
            pending = [v for v in videos if v["id"] not in state["processed"]]
        elif cutoff_date:
            pending = [
                v for v in videos
                if v["id"] not in state["processed"]
                and v["id"] not in cfg.skip_ids
                and v.get("upload_date", "99999999") >= cfg.date_cutoff
            ]
            print(f"   {src}: {len(videos)} in window, {len(pending)} on or after {cfg.date_cutoff}")
        else:
            # Incremental: newest-first until the first already-processed ID.
            pending = []
            for v in videos:
                if v["id"] in state["processed"]:
                    break
                pending.append(v)
        all_pending.extend(pending)

    # Meeting-creating scrapers. Isolate failures so one upstream outage
    # doesn't abort other sources.
    for src in meeting_creating:
        adapter_name = cfg.by_key[src].agendas_adapter
        try:
            if adapter_name == "boardbook":
                n = fetch_boardbook_new(cfg, src, state, dry_run=args.dry_run)
            else:
                n = fetch_municode_new(cfg, src, state, dry_run=args.dry_run)
            print(f"   {src}: {n} new meeting(s) processed")
        except AdapterStructureError as e:
            logger.error("%s structure drift: %s", src, e)
            structure_failures.append(e)
        except Exception as e:
            logger.error("%s processing failed: %s", src, e)
        finally:
            save_state(state)

    ok = 0
    failed = 0
    if all_pending:
        print(f"\n[agenda]  {len(all_pending)} meeting(s) to process:")
        for v in all_pending:
            print(f"   [{cfg.by_key[v['source']].name}] {v['title']}")
        if args.dry_run:
            print("\n[DRY RUN] No files written.")
            return
        for v in all_pending:
            # Isolate per-video failures — one bad video or a mid-run API
            # outage shouldn't abort the rest of the run.
            try:
                path = process_video(cfg, v, auth_failures)
            except Exception as e:
                logger.error("process_video failed for %s (%s): %s",
                             v["id"], v.get("title", "")[:60], e)
                failed += 1
                continue
            if path:
                mark_processed(state, v["id"], v["title"], v["source"], path,
                               v.get("doc_url"),
                               upload_date=v.get("upload_date"),
                               meeting_date=v.get("meeting_date"),
                               duration=v.get("duration"))
                save_state(state)
                ok += 1
        if failed:
            print(f"[warn]  {failed} video(s) failed — they remain unprocessed "
                  f"and will be retried next run.")
    else:
        print("[ok]  No new YouTube meetings to process.")

    if args.dry_run:
        print("\n[DRY RUN] Skipping upgrade passes, publish, and upcoming — "
              "no files written.")
        return

    # ── Upgrade passes ─────────────────────────────────────────────────────
    upgraded_ids: list[str] = []
    upgraded_ids += retry_agenda_only_captions(cfg, state, youtube_first,
                                               auth_failures)
    for src in meeting_creating:
        jur = cfg.by_key[src]
        if jur.agendas_adapter == "boardbook":
            upgraded_ids += upgrade_boardbook_from_recordings(
                cfg, src, state, auth_failures)
        else:
            upgraded_ids += upgrade_municode_from_minutes(cfg, src, state)

    if upgraded_ids:
        clear_injected(upgraded_ids)
        print(f"[retry]  Cleared {len(upgraded_ids)} ID(s) from the injected "
              f"tracker for re-publication.")

    print(f"\n{'='*60}")
    print(f"[ok]  Done. {ok}/{len(all_pending)} processed.")

    write_agenda_only_report(cfg, state, ok)
    prune_orphan_summaries(state)

    # ── Publish + upcoming ─────────────────────────────────────────────────
    publish_mod.publish(cfg)
    if not args.skip_upcoming:
        try:
            upcoming.update_upcoming(cfg, sources=sources)
        except AdapterStructureError as e:
            structure_failures.append(e)

    # ── Fail loud at the end (after outputs are written) ───────────────────
    if auth_failures:
        print(f"\n{'!'*60}")
        print(f"[AUTH FAILURE]  {len(auth_failures)} transcript fetch(es) hit "
              f"YouTube bot-blocking — cookie refresh needed for:")
        for e in auth_failures:
            print(f"   - {cfg.name} / {e.jurisdiction}: {e.detail[:160]}")
    if structure_failures:
        print(f"\n{'!'*60}")
        print(f"[STRUCTURE DRIFT]  {len(structure_failures)} adapter(s) hit "
              f"unrecognized upstream HTML:")
        for e in structure_failures:
            print(f"   - {e}")
    if auth_failures or structure_failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
