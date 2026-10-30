"""primegov adapter — PrimeGov public portal (agenda text, upcoming).

Config block: {"adapter": "primegov", "client": ..., "days_ahead": N}
`client` is the city's PrimeGov name, the subdomain of <client>.primegov.com
(cityofoviedo, santa-ana). A meeting's doc_url is its HTML agenda,
.../Portal/Meeting?compiledMeetingDocumentFileId=<document id>.

The public API lists meetings and their documents but not votes, so this
adapter gives agenda text only: meetings are summarized from the agenda.
Bilingual cities post each meeting twice; the "Versión en Español" copy is
skipped.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
_SPANISH = re.compile(r"versi[oó]n en espa[nñ]ol", re.I)


def _portal(agendas_cfg: dict) -> str:
    return f"https://{agendas_cfg['client']}.primegov.com"


def _doc_id(doc_url: str) -> int | None:
    m = re.search(r"[?&]compiledMeetingDocumentFileId=(\d+)", doc_url or "", re.I)
    return int(m.group(1)) if m else None


def agenda_url(agendas_cfg: dict, meeting: dict) -> str | None:
    """A listed meeting's HTML agenda, the doc_url this adapter reads, or None if it has none."""
    doc = next((d for d in meeting.get("documentList") or [] if d.get("compileOutputType") == 3
                and "agenda" in (d.get("templateName") or "").lower()), None)
    return f"{_portal(agendas_cfg)}/Portal/Meeting?compiledMeetingDocumentFileId={doc['id']}" if doc else None


def agenda_text(page: str) -> str | None:
    """The agenda in a PrimeGov meeting page: the MeetingContents block as lines of text, without the portal
    around it or the attachment links under each item (file names, which crowd out the agenda in the prompt)."""
    start = page.find('id="MeetingContents"')
    if start < 0:
        return None
    part = page[page.find(">", start) + 1:]
    part = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>|<a\b[^>]*>.*?</a>", " ", part)
    part = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|td)>", "\n", part)
    lines = (re.sub(r"[ \t\xa0]+", " ", x).strip() for x in html.unescape(re.sub(r"<[^>]+>", " ", part)).splitlines())
    text = "\n".join(line for line in lines if line)
    return text if len(text) > 100 else None


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The agenda's text from its HTML agenda page."""
    if not _doc_id(doc_url):
        return None
    try:
        r = requests.get(doc_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        return agenda_text(r.text) if r.status_code == 200 else None
    except Exception as e:
        print(f"       PrimeGov agenda fetch failed: {e}")
        return None


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future meetings from ListUpcomingMeetings."""
    end = (date.today() + timedelta(days=days_ahead)).isoformat()
    try:
        r = requests.get(f"{_portal(agendas_cfg)}/api/v2/PublicPortal/ListUpcomingMeetings", headers=_HEADERS, timeout=15)
        meetings = r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning("%s PrimeGov fetch failed: %s", jurisdiction_key, e)
        return []

    results = [{
        "date": m["dateTime"][:10],
        "time": (m.get("time") or "").lstrip("0"),
        "name": (m.get("title") or "").strip(),
        "url": agenda_url(agendas_cfg, m) or f"{_portal(agendas_cfg)}/public/portal",
        "source": jurisdiction_key,
    } for m in meetings
        if m.get("dateTime", "")[:10] <= end and not _SPANISH.search(m.get("title") or "") and "CANCEL" not in (m.get("title") or "").upper()]
    print(f"  [ok]  {jurisdiction_key}: {len(results)} upcoming events from PrimeGov")
    return results
