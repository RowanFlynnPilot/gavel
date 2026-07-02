"""municode adapter — Municode Meetings hub (Kronenwetter-style).

Config block: {"adapter": "municode", "base": ..., "ada_url": "...{guid}...",
               "blob_base": ..., "id_prefix": "kw",
               "minutes_upgrade_window_days": N, "days_ahead": N}

Meetings are created agenda-only with synthetic <id_prefix>_<guid> IDs
derived from the agenda PDF GUID. Agendas use the ADA HTML endpoint (clean text, no PDF
parsing); official minutes post days-to-weeks later and drive the minutes
upgrade pass.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import requests

from ..errors import AdapterStructureError

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def list_meetings(agendas_cfg: dict, max_pages: int = 2) -> list[dict]:
    """Scrape the hub table (newest first).
    Returns [{guid, date (YYYYMMDD), time, name, agenda_pdf, packet_pdf,
              minutes_pdf, detail_url, cancelled}].
    Meetings without a posted agenda yet have guid=None.
    Raises AdapterStructureError if page 0 fetched OK but zero rows parsed."""
    base = agendas_cfg["base"]
    meetings = []
    seen_keys = set()
    for page in range(max_pages):
        url = base + (f"/?page={page}" if page else "/")
        try:
            r = requests.get(url, headers=_HEADERS, timeout=20)
        except Exception as e:
            print(f"   [warn]  Municode hub page {page} fetch failed: {e}")
            break
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL)
        page_hits = 0
        for row in rows:
            text = re.sub(r"&[a-z]+;", " ", row)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            dm = re.search(
                r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{1,2}:\d{2})\s*([ap]m)\s+(.*?)(?:\s*View Details|$)",
                text)
            if not dm:
                continue
            page_hits += 1
            mo, dy, yr, clock, ampm, name = dm.groups()
            name = name.strip()
            guid_m = re.search(r"MEET-Agenda-([0-9a-f]{32})\.pdf", row)
            packet_m = re.search(r"MEET-Packet-([0-9a-f]{32})\.pdf", row)
            minutes_m = re.search(r"MEET-Minutes-([0-9a-f]{32})\.pdf", row)
            detail_m = re.search(r'href="(/bc-[^"]+)"', row)
            blob = agendas_cfg["blob_base"]
            entry = {
                "guid": guid_m.group(1) if guid_m else None,
                "date": f"{yr}{mo}{dy}",
                "time": f"{clock} {ampm.upper()}",
                "name": name,
                "agenda_pdf": f"{blob}/MEET-Agenda-{guid_m.group(1)}.pdf" if guid_m else None,
                "packet_pdf": f"{blob}/MEET-Packet-{packet_m.group(1)}.pdf" if packet_m else None,
                "minutes_pdf": f"{blob}/MEET-Minutes-{minutes_m.group(1)}.pdf" if minutes_m else None,
                "detail_url": base + detail_m.group(1) if detail_m else base,
                "cancelled": "CANCEL" in name.upper(),
            }
            key = entry["guid"] or (entry["date"], entry["name"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            meetings.append(entry)
        if page == 0 and r.status_code == 200 and page_hits == 0:
            raise AdapterStructureError(
                f"Municode hub at {base}: page fetched but no meeting rows "
                f"matched — upstream HTML structure changed")
    return meetings


def fetch_agenda_text(agendas_cfg: dict, guid: str) -> str | None:
    """Fetch the ADA HTML rendition of an agenda — clean text, no PDF."""
    try:
        r = requests.get(agendas_cfg["ada_url"].format(guid=guid),
                         headers=_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<br[^>]*>|</p>|</div>|</li>|</tr>", "\n", html,
                      flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text if len(text) > 100 else None
    except Exception as e:
        print(f"       Municode ADA agenda fetch failed: {e}")
        return None


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future meetings from the hub (the portal lists meetings weeks
    ahead on page 0)."""
    today = date.today()
    end_date = today + timedelta(days=days_ahead)
    results = {}
    try:
        meetings = list_meetings(agendas_cfg, max_pages=1)
    except AdapterStructureError:
        raise
    except Exception as e:
        logger.warning("%s Municode hub scrape failed: %s", jurisdiction_key, e)
        return []

    for m in meetings:
        if m["cancelled"]:
            continue
        try:
            d = date(int(m["date"][:4]), int(m["date"][4:6]), int(m["date"][6:8]))
        except ValueError:
            continue
        if not (today <= d <= end_date):
            continue
        key = (d.isoformat(), m["name"][:30])
        results[key] = {
            "date": d.isoformat(),
            "time": m["time"],
            "name": m["name"],
            "url": m["detail_url"],
            "source": jurisdiction_key,
        }
    print(f"  [ok]  {jurisdiction_key} Municode hub: {len(results)} posted future meetings")
    return sorted(results.values(), key=lambda x: (x["date"], x["time"]))
