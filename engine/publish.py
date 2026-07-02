"""Publish step — build data/meetings.json from state + summary sidecars.

Port of marathon-meetings inject_meetings.py with all jurisdiction-specific
tables (committee map, title-strip regexes, synthetic-ID fallback URLs)
driven by instance.json.

The published PAST-MEETING RECORD (one element of data/meetings.json):
    {id, source, title, date ("March 12, 2026"), shortDate ("MAR 12"),
     committee, duration ("1h 20m"|null), url, docUrl, isAgendaOnly,
     badge ("new"|null), overview, agenda: [{time, item}],
     discussions: [{item, body}], publicComment, actionItems: [str],
     civicItems?: [...] (CivicClerk jurisdictions only)}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import InstanceConfig, MEETINGS_JSON, STATE_FILE
from .state import load_injected, load_state, save_injected

logger = logging.getLogger(__name__)


# ── Committee / title helpers ─────────────────────────────────────────────────

def _boardbook_committee_from_title(title: str) -> str:
    """Meeting-type committee for a BoardBook district, from the title (the
    summary's `committee` field is unreliable after a transcript upgrade —
    Claude returns the board's name there instead of the meeting type)."""
    t = title.lower()
    if "committee of the whole" in t:          return "Committee of the Whole"
    if "workshop" in t:                        return "Board Workshop"
    if "annual" in t or "budget hearing" in t: return "Annual Meeting"
    if "special" in t:                         return "Special Meeting"
    if "audit" in t:                           return "Audit of the Bills"
    if "regular" in t:                         return "Regular Meeting"
    if "public hearing" in t:                  return "Public Hearing"
    c = re.sub(r"\s*-\s*\d.*$", "", title).strip()
    return c or "Board Meeting"


def infer_committee(cfg: InstanceConfig, title: str, source_key: str,
                    committee_from_json: str) -> str:
    """Clean up the display committee name via the jurisdiction's
    committee_map. source_key from state is authoritative and never changes."""
    jur = cfg.jurisdiction(source_key)

    if jur.agendas_adapter == "boardbook":
        return _boardbook_committee_from_title(title)

    def normalize(s):
        return s.lower().replace(" and ", " & ").replace("  ", " ")

    if committee_from_json and len(committee_from_json) > 3:
        lower = normalize(committee_from_json)
        for key, display in jur.committee_map.items():
            if key in lower:
                return display
        return committee_from_json

    lower = normalize(title)
    for key, display in jur.committee_map.items():
        if key in lower:
            return display

    clean = re.sub(r'\s*-\s*\d+.*$', '', title).strip()
    clean = re.sub(r'\s*-\s*20\d{2}-\d{2}-\d{2}$', '', clean).strip()
    return clean


def strip_title_prefix(cfg: InstanceConfig, title: str, source_key: str) -> str:
    """Strip the jurisdiction prefix — the source badge already tells the
    reader which jurisdiction a card belongs to."""
    jur = cfg.by_key.get(source_key)
    if not jur:
        return title
    for pattern in jur.title_strip_patterns:
        title = re.sub(pattern, '', title, flags=re.IGNORECASE).strip()
    return title


# ── Date helpers ──────────────────────────────────────────────────────────────

def parse_date_from_title(title: str):
    """Handles 'Board of Trustees - 3/23/2026' and 'Special Meeting - 2026-01-26'."""
    m = re.search(r'(20\d{2})-(\d{2})-(\d{2})', title)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', title)
    if m:
        mo, dy, yr = m.groups()
        yr = "20" + yr if len(yr) == 2 else yr
        try:
            return datetime(int(yr), int(mo), int(dy))
        except ValueError:
            pass
    m = re.search(r'(\d{1,2})-(\d{1,2})-(\d{2,4})(?!\d)', title)
    if m:
        mo, dy, yr = m.groups()
        yr = "20" + yr if len(yr) == 2 else yr
        try:
            dt = datetime(int(yr), int(mo), int(dy))
            if dt.year >= 2010:
                return dt
        except ValueError:
            pass
    return None


def fmt_date(dt: datetime) -> str:
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def fmt_short_date(dt: datetime) -> str:
    return f"{dt.strftime('%b')} {dt.day}".upper()


def clean_committee(s: str) -> str:
    """Strip HTML tags and placeholder tokens that occasionally bleed in."""
    if not s:
        return ""
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'\[+[A-Z_]+\]+', '', s)
    return s.strip()


def _fmt_duration_secs(secs):
    if not secs:
        return None
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    h = secs // 3600
    m = (secs % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _synthetic_fallback_url(cfg: InstanceConfig, video_id: str,
                            source_key: str) -> str:
    """Card link for a synthetic (bb_/kw_) entry with no matched recording:
    the jurisdiction's agenda portal."""
    jur = cfg.jurisdiction(source_key)
    a = jur.agendas
    if jur.agendas_adapter == "boardbook":
        return f"{a['base']}/Public/Organization/{a['org_id']}"
    return a.get("base", "")


def _is_synthetic(video_id: str) -> bool:
    # bb_ = BoardBook; anything with a <prefix>_<32-hex-guid> shape = municode.
    return video_id.startswith("bb_") or bool(
        re.match(r"^[a-z]{2,8}_[0-9a-f]{32}$", video_id))


# ── Build one record ──────────────────────────────────────────────────────────

def build_meeting(cfg: InstanceConfig, video_id: str, info: dict,
                  summary: dict, civic_data: dict | None,
                  existing_date: str | None = None) -> dict | None:
    """existing_date = the date currently published for this meeting, used as
    a tie-breaker so re-publishing never drifts a date to the CI run day."""
    title = info["title"]
    source_key = info["source"]
    doc_url = info.get("doc_url")
    jur = cfg.jurisdiction(source_key)
    today = date.today()

    def _yyyymmdd_to_dt(s):
        if s and len(str(s)) == 8 and str(s).isdigit():
            try:
                return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))
            except ValueError:
                return None
        return None

    # 1. meeting_date is authoritative (actual meeting date, not upload date).
    dt = _yyyymmdd_to_dt(info.get("meeting_date"))
    # 2. Title may carry the date as a suffix.
    if dt is None:
        dt = parse_date_from_title(title)
    # 3. upload_date (close enough; off by ~1 day for some sources).
    if dt is None:
        dt = _yyyymmdd_to_dt(info.get("upload_date"))
    # 4. Known-good published date.
    if dt is None and existing_date:
        try:
            dt = datetime.strptime(existing_date, "%B %d, %Y")
        except ValueError:
            dt = None
    # 5. Last resort: processed_at, clamped so a past meeting never shows a
    #    today-or-future date.
    if dt is None:
        processed_at = info.get("processed_at", "")
        if processed_at:
            try:
                dt = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now()
        else:
            dt = datetime.now()
        if dt.date() >= today:
            logger.warning(
                "%s: no upload_date and processed_at resolves to %s; "
                "clamping to yesterday to avoid a future-dated past meeting",
                video_id, dt.date().isoformat())
            dt = datetime.combine(today - timedelta(days=1), datetime.min.time())

    committee = clean_committee(
        infer_committee(cfg, title, source_key, summary.get("committee", "")))

    is_synthetic = _is_synthetic(video_id)
    if is_synthetic:
        yt_url = info.get("video_url") or doc_url \
            or _synthetic_fallback_url(cfg, video_id, source_key)
    else:
        yt_url = f"https://www.youtube.com/watch?v={video_id}"

    clean_title = title
    clean_title = re.sub(r'\s*-\s*\d{4}-\d{2}-\d{2}$', '', clean_title).strip()
    clean_title = re.sub(r'\s*-\s*\d{1,2}/\d{1,2}/\d{2,4}$', '', clean_title).strip()
    clean_title = re.sub(r'\s*-\s*\d{1,2}-\d{1,2}-\d{2,4}$', '', clean_title).strip()
    clean_title = strip_title_prefix(cfg, clean_title, source_key)

    agenda_items = summary.get("agenda") or [{"time": "0:00", "item": "Meeting convened"}]
    # Synthetic entries are agenda-only by default, but the upgrade passes
    # can re-summarize from real outcomes: a recording transcript (boardbook)
    # or official minutes (municode).
    is_agenda_only = (summary.get("_source") == "agenda") or \
                     (is_synthetic and summary.get("_source") not in ("transcript", "minutes"))

    duration_str = None if is_agenda_only else _fmt_duration_secs(info.get("duration"))

    meeting = {
        "id": video_id,
        "source": source_key,
        "title": clean_title,
        "date": fmt_date(dt),
        "shortDate": fmt_short_date(dt),
        "committee": committee,
        "duration": duration_str,
        "url": yt_url,
        "docUrl": doc_url,
        "isAgendaOnly": bool(is_agenda_only),
        "badge": "new",
        "overview": summary.get("overview", ""),
        "agenda": agenda_items,
        "discussions": summary.get("discussions", []),
        "publicComment": summary.get("publicComment", "No public comment was offered."),
        "actionItems": summary.get("actionItems", []),
    }

    if civic_data and jur.agendas_adapter == "civicclerk":
        items = civic_data.get("items", [])
        if items:
            meeting["civicItems"] = items

    return meeting


# ── Whole-file publish ────────────────────────────────────────────────────────

def _load_meetings_json(path: Path) -> list:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [m for m in data if isinstance(m, dict)]
        except json.JSONDecodeError:
            logger.warning("%s is malformed; starting fresh.", path)
            return []
    return []


def _is_future(meeting: dict) -> bool:
    date_str = meeting.get("date", "")
    if not date_str:
        return False
    try:
        return datetime.strptime(date_str, "%B %d, %Y").date() > date.today()
    except ValueError:
        return False


def _is_stub(meeting: dict) -> bool:
    return not meeting.get("agenda") and not meeting.get("discussions")


def _title_is_future(info: dict) -> bool:
    title = info.get("title", "")
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", title)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) > date.today()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", title)
    if m:
        try:
            mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            yr = 2000 + yr if yr < 100 else yr
            return date(yr, mo, dy) > date.today()
        except ValueError:
            pass
    return False


def publish(cfg: InstanceConfig, data_path: Path | None = None) -> int:
    """Rebuild data/meetings.json from state + sidecars. Returns the number
    of newly built entries."""
    data_path = data_path or MEETINGS_JSON
    print(f"\n[publish]  target: {data_path}")

    if not STATE_FILE.exists():
        print("   No state file found — nothing processed yet.")
        return 0

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    processed = state.get("processed", {})
    injected = load_injected()

    data_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_meetings_json(data_path)
    existing_by_id = {m["id"]: m for m in existing}
    stub_ids = {m["id"] for m in existing if _is_stub(m)}

    print(f"   Processed IDs in state:        {len(processed)}")
    print(f"   Already published:             {len(injected)}")
    print(f"   Existing entries (loaded):     {len(existing)}")
    print(f"   Stubs awaiting full summary:   {len(stub_ids)}")

    pending = {
        vid: info for vid, info in processed.items()
        if (vid not in injected or vid in stub_ids) and not _title_is_future(info)
    }

    if not pending:
        print("   No new meetings to publish (rewriting for canonical order).")

    new_entries = []
    newly_injected = set()

    for video_id, info in sorted(pending.items(),
                                 key=lambda x: x[1].get("processed_at", ""),
                                 reverse=True):
        summary_path = info.get("summary_file", "")
        summary_json_path = summary_path.replace(".md", "_summary.json") if summary_path else ""
        if not summary_json_path or not Path(summary_json_path).exists():
            logger.warning("No summary JSON for %s, skipping", info["title"])
            continue
        summary = json.loads(Path(summary_json_path).read_text(encoding="utf-8"))

        votes_path = summary_path.replace(".md", "_votes.json") if summary_path else ""
        civic_data = None
        if votes_path and Path(votes_path).exists():
            try:
                civic_data = json.loads(Path(votes_path).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                civic_data = None

        prior = existing_by_id.get(video_id)
        prior_date = prior.get("date") if prior else None
        entry = build_meeting(cfg, video_id, info, summary, civic_data,
                              existing_date=prior_date)
        if entry is None:
            continue
        new_entries.append(entry)
        newly_injected.add(video_id)
        print(f"   [ok]  Built entry for: {info['title']}")

    # Carry over existing entries not rebuilt this run; drop ones removed
    # from state (e.g. a force-reprocess cleared a wrong summary).
    state_ids = set(processed.keys())
    kept_existing = [m for m in existing
                     if m["id"] not in newly_injected and m["id"] in state_ids]
    dropped = len(existing) - len(kept_existing) \
        - len([m for m in existing if m["id"] in newly_injected])
    if dropped > 0:
        print(f"   [prune] dropped {dropped} entry/entries no longer in state")

    # Carried entries don't pass through build_meeting (their sidecars may be
    # pruned) — apply title/committee/duration normalization here so they pick
    # up later improvements without a re-summarization.
    for m in kept_existing:
        m["title"] = strip_title_prefix(cfg, m.get("title", ""), m.get("source", ""))
        jur = cfg.by_key.get(m.get("source", ""))
        if jur and jur.agendas_adapter == "boardbook":
            m["committee"] = _boardbook_committee_from_title(m.get("title", ""))
        if m.get("duration") in (None, "", "~1h"):
            info = processed.get(m["id"], {})
            new_dur = None
            if not m.get("isAgendaOnly"):
                new_dur = _fmt_duration_secs(info.get("duration"))
            m["duration"] = new_dur

    combined = new_entries + kept_existing

    before = len(combined)
    combined = [m for m in combined if not _is_future(m)]
    if before - len(combined):
        print(f"   [prune] removed {before - len(combined)} future-dated entry/entries")

    # Sort newest-first BEFORE pruning so the cut can't drop a recent meeting
    # while keeping an older one.
    def _sort_key(m):
        try:
            return datetime.strptime(m.get("date", ""), "%B %d, %Y")
        except ValueError:
            return datetime.min
    combined.sort(key=_sort_key, reverse=True)

    if len(combined) > cfg.max_meetings:
        n = len(combined) - cfg.max_meetings
        combined = combined[:cfg.max_meetings]
        print(f"   [prune] pruned {n} old meeting(s) (kept {cfg.max_meetings})")

    # Badge sweep: only this run's new entries keep badge=new. (badge=null
    # instead of a missing key keeps the JSON diff minimal between runs.)
    for m in combined:
        m["badge"] = "new" if m["id"] in newly_injected else None

    # Backfill missing doc URLs by date for agendacenter jurisdictions.
    from .adapters import agendacenter
    backfilled = 0
    for m in combined:
        jur = cfg.by_key.get(m.get("source", ""))
        if not jur or jur.agendas_adapter != "agendacenter" or m.get("docUrl"):
            continue
        try:
            d = datetime.strptime(m["date"], "%B %d, %Y")
            url = agendacenter.fetch_doc_url_by_date(jur.agendas, d.strftime("%m%d%Y"))
            if url:
                m["docUrl"] = url
                backfilled += 1
        except (ValueError, KeyError):
            pass
    if backfilled:
        print(f"   [ok]  Backfilled {backfilled} AgendaCenter doc URL(s)")

    data_path.write_text(
        json.dumps(combined, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"   Wrote {len(combined)} meeting(s) to {data_path} ({len(new_entries)} new)")

    injected.update(newly_injected)
    save_injected(injected)
    return len(new_entries)
