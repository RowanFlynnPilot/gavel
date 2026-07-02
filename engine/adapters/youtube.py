"""youtube adapter — channel video listing via yt-dlp flat-playlist.

Given a jurisdiction's video config block, returns normalized video records:
    {id, title, url, source, doc_url, upload_date (YYYYMMDD),
     meeting_date (YYYYMMDD from title), duration (secs), description}

Transcript acquisition itself lives in engine.transcripts (it's shared by
the ingest tool and the upgrade passes, not adapter-specific).
"""

from __future__ import annotations

import json
import re
import subprocess

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date_from_title(title: str) -> str:
    """Parse YYYYMMDD from a video title.
    Handles: 3/19/26, 3-19-26, 3/19/2026, 2026-01-26, and month-name forms
    like 'May 20, 2026' (DC Everest titles). Empty string if none found."""
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", title)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", title)
    if m:
        mo = m.group(1).zfill(2)
        dy = m.group(2).zfill(2)
        yr = m.group(3)
        yr = "20" + yr if len(yr) == 2 else yr
        if int(mo) > 12 or int(dy) > 31:
            return ""
        return yr + mo + dy
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", title)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon and int(m.group(2)) <= 31:
            return f"{m.group(3)}{mon:02d}{int(m.group(2)):02d}"
    return ""


def list_videos(video_cfg: dict, jurisdiction_key: str, label: str,
                dateafter: str = "") -> list[dict]:
    """Fetch the channel's video list using flat-playlist.
    Dates are parsed from titles since flat-playlist omits upload_date;
    dateafter (YYYYMMDD) filters client-side by that parsed date."""
    channel_url = video_cfg["channel_url"]
    print(f"\U0001f4e1  Fetching {label} video list...")
    cmd = ["yt-dlp", "--no-check-certificate", "--flat-playlist",
           "--dump-json", channel_url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr[-300:]}")

    pattern = video_cfg.get("doc_pattern")
    videos = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid_id = d.get("id", "")
        title = d.get("title", "Untitled")
        desc = d.get("description") or ""
        if not vid_id:
            continue
        doc_m = re.search(pattern, desc) if pattern else None
        yt_upload = d.get("upload_date") or ""
        title_date = parse_date_from_title(title)
        videos.append({
            "id": vid_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "source": jurisdiction_key,
            "doc_url": doc_m.group(0) if doc_m else None,
            "upload_date": yt_upload or title_date,
            "meeting_date": title_date,
            "duration": d.get("duration"),
            "description": desc,
        })

    if dateafter:
        total = len(videos)
        videos = [v for v in videos
                  if v["upload_date"] and v["upload_date"] >= dateafter]
        print(f"   {total} total, {len(videos)} on or after {dateafter} (by title date)")

    print(f"   Returning {len(videos)} videos for {label}")
    return videos


def scan_descriptions_for_upcoming(video_cfg: dict, jurisdiction_key: str,
                                   days_ahead: int) -> list[dict]:
    """Scan recent video descriptions for explicit 'next meeting M/D/YY'
    mentions (Marathon County posts these). Returns upcoming records."""
    from datetime import date, timedelta

    today = date.today()
    end_date = today + timedelta(days=days_ahead)
    results = {}
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-check-certificate", "--flat-playlist",
             "--dump-json", video_cfg["channel_url"]],
            capture_output=True, text=True, timeout=30,
        )
        for line in out.stdout.strip().splitlines()[:20]:
            d = json.loads(line)
            title = d.get("title", "")
            desc = d.get("description", "") or ""
            date_mentions = re.findall(
                r'(?:next meeting|upcoming)[^\n]{0,60}(\d{1,2}/\d{1,2}/\d{2,4})',
                desc, re.IGNORECASE,
            )
            for dm in date_mentions:
                parts = dm.split("/")
                if len(parts) != 3:
                    continue
                mo, dy, yr = parts
                yr = "20" + yr if len(yr) == 2 else yr
                try:
                    md = date(int(yr), int(mo), int(dy))
                except ValueError:
                    continue
                if today <= md <= end_date:
                    comm = re.sub(r'\s*-\s*\d+.*$', '', title).strip()
                    key = (md.isoformat(), comm)
                    results[key] = {
                        "date": md.isoformat(),
                        "time": "",
                        "name": comm,
                        "url": f"https://www.youtube.com/watch?v={d['id']}",
                        "source": jurisdiction_key,
                    }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "%s YouTube description scan failed: %s", jurisdiction_key, e)
    return list(results.values())
