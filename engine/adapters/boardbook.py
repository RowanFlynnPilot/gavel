"""boardbook adapter — BoardBook Premier org pages (multi-org).

Config block: {"adapter": "boardbook", "base": ..., "org_id": N,
               "label": "...", "days_ahead": N, "fallback_rules": [...]}

Meetings are created agenda-only with synthetic bb_<meeting_id> IDs
(BoardBook meeting IDs are globally unique across orgs; the stored `source`
disambiguates districts). The video-upgrade pass in the pipeline later
matches recordings from the district's YouTube channel.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

import requests

from ..errors import AdapterStructureError
from .youtube import parse_date_from_title

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def list_meetings(agendas_cfg: dict) -> list[dict]:
    """Scrape the organization page for all meetings (newest first).
    Returns [{meeting_id, date (ISO), time, name, url, packet_url}].
    Raises AdapterStructureError if the page parses to zero agenda rows —
    that's HTML drift, not an empty schedule."""
    base = agendas_cfg["base"]
    org = agendas_cfg["org_id"]
    r = requests.get(f"{base}/Public/Organization/{org}", headers=_HEADERS,
                     timeout=15)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL)
    agenda_rows = [row for row in rows
                   if re.search(r"/Public/Agenda/\d+\?meeting=(\d+)", row)]
    if r.status_code == 200 and not agenda_rows:
        raise AdapterStructureError(
            f"BoardBook org {org}: page fetched but no agenda rows matched — "
            f"upstream HTML structure changed")

    meetings = []
    for row in agenda_rows:
        id_m = re.search(r"/Public/Agenda/\d+\?meeting=(\d+)", row)
        text = re.sub(r"&nbsp;", " ", row)
        text = re.sub(r"&[a-z]+;", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        dm = re.search(
            r"(\w+ \d+, \d{4}) at (\d+:\d+ [AP]M) - (.+?)(?:Meeting Type|$)", text)
        if not dm:
            continue
        date_str, time_str, name = dm.groups()
        name = re.sub(r"\s+Meeting Type.*$", "", name).strip().rstrip(" -")
        try:
            dt = datetime.strptime(date_str, "%B %d, %Y").date()
        except ValueError:
            continue
        mid = id_m.group(1)
        meetings.append({
            "meeting_id": mid,
            "date": dt.isoformat(),
            "time": time_str,
            "name": name.strip(),
            "url": f"{base}/Public/Agenda/{org}?meeting={mid}",
            "packet_url": f"{base}/Public/DownloadAgenda/{org}?meeting={mid}",
        })
    return sorted(meetings, key=lambda x: x["date"], reverse=True)


def fetch_agenda(agendas_cfg: dict, meeting_id: str) -> dict:
    """Scrape agenda items, descriptions, presenter info, and attachment
    names from a meeting page for richer agenda-only summaries.
    Returns {page_title, items, meeting_id, packet_url, agenda_url}."""
    base = agendas_cfg["base"]
    org = agendas_cfg["org_id"]
    r = requests.get(f"{base}/Public/Agenda/{org}?meeting={meeting_id}",
                     headers=_HEADERS, timeout=15)

    title_m = re.search(r"<title>([^<]+)</title>", r.text)
    page_title = (title_m.group(1) if title_m else "").replace(
        " - BoardBook Premier", "").strip()

    html = r.text
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&eacute;", "e")
    html = re.sub(r"&[a-z]+;", " ", html)

    items = []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        row_text_parts = []
        for cell in cells:
            attachments = re.findall(r'title="([^"]+)"', cell)
            clean = re.sub(r"<[^>]+>", " ", cell)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean and len(clean) > 3:
                row_text_parts.append(clean)
            for att in attachments:
                if len(att) > 5 and att not in row_text_parts:
                    row_text_parts.append(f"[Attachment: {att}]")
        row_text = " | ".join(row_text_parts)
        if len(row_text) > 5 and row_text.lower() != "agenda":
            items.append(row_text)

    desc_blocks = re.findall(
        r'class="[^"]*(?:description|detail|presenter|estimate)[^"]*"[^>]*>(.*?)</(?:div|span|p)>',
        r.text, re.DOTALL | re.IGNORECASE,
    )
    for desc in desc_blocks:
        clean = re.sub(r"<[^>]+>", " ", desc)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) > 10:
            items.append(f"[Detail: {clean}]")

    return {
        "page_title": page_title,
        "items": items,
        "meeting_id": meeting_id,
        "packet_url": f"{base}/Public/DownloadAgenda/{org}?meeting={meeting_id}",
        "agenda_url": f"{base}/Public/Agenda/{org}?meeting={meeting_id}",
    }


def match_recording(info: dict, videos: list[dict]) -> dict | None:
    """Match a BoardBook meeting record to a district YouTube video by
    meeting date + meeting type. Returns None unless exactly one video
    matches — better to stay agenda-only than attach the wrong recording."""
    mdate = info.get("meeting_date") or parse_date_from_title(info.get("title", ""))
    if not mdate:
        return None
    same_day = [v for v in videos
                if (v.get("meeting_date") or parse_date_from_title(v.get("title", ""))) == mdate]
    if not same_day:
        return None

    def _mtype(t: str) -> str:
        t = t.lower()
        if "special" in t:
            return "special"
        if "workshop" in t:
            return "workshop"
        if "annual" in t or "budget hearing" in t:
            return "annual"
        # BoardBook says "Committee of the Whole"; the channel titles it
        # "Ed/Op Committee Meeting" — both are the committee meeting.
        if "committee" in t or "ed/op" in t:
            return "committee"
        # "Public Meeting" entries are hearings/quorum notices — without this
        # they'd classify as "regular" and steal the Regular Meeting's
        # recording on shared dates.
        if "public meeting" in t:
            return "public"
        return "regular"

    want = _mtype(info.get("title", ""))
    matches = [v for v in same_day if _mtype(v.get("title", "")) == want]
    return matches[0] if len(matches) == 1 else None


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future meetings, plus the fallback_rules projection for months
    the org hasn't posted yet."""
    from .rule_schedule import project

    base = agendas_cfg["base"]
    org = agendas_cfg["org_id"]
    today = date.today()
    end_date = today + timedelta(days=days_ahead)
    results = {}

    try:
        meetings = list_meetings(agendas_cfg)
        for m in meetings:
            dt = date.fromisoformat(m["date"])
            if today <= dt <= end_date:
                key = (m["date"], m["name"][:30])
                results[key] = {
                    "date": m["date"],
                    "time": m["time"],
                    "name": m["name"],
                    "url": m["url"],
                    "source": jurisdiction_key,
                }
        print(f"  [ok]  {jurisdiction_key} BoardBook: {len(results)} posted future meetings")
    except Exception as e:
        logger.warning("BoardBook scrape failed for %s: %s", jurisdiction_key, e)

    rules = agendas_cfg.get("fallback_rules") or []
    rule_added = 0
    for entry in project(rules, jurisdiction_key, days_ahead,
                         default_url=f"{base}/Public/Organization/{org}"):
        key = (entry["date"], entry["name"][:30])
        if key not in results:
            results[key] = entry
            rule_added += 1
    if rules:
        print(f"  [ok]  {jurisdiction_key} rule-based: {rule_added} additional meetings projected")
    return sorted(results.values(), key=lambda x: (x["date"], x["time"]))
