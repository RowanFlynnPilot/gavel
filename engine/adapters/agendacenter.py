"""agendacenter adapter — CivicPlus AgendaCenter HTML scrape (Weston-style).

Config block: {"adapter": "agendacenter", "base": "https://www.westonwi.gov",
               "days_ahead": N, "fallback_rules": [...]}

HTML-scraped and will drift: fetch_upcoming raises AdapterStructureError if
the page yields zero <h2> committee sections (structure change), while zero
*future* agendas is legitimate (the rules projection fills those months).
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import requests

from ..errors import AdapterStructureError
from ..pdftext import fetch_pdf_text

logger = logging.getLogger(__name__)

# Cache the page HTML for multiple lookups in one run, keyed by base URL.
_page_cache: dict[str, str] = {}


def _fetch_page(base: str) -> str:
    if base in _page_cache:
        return _page_cache[base]
    try:
        r = requests.get(f"{base}/agendacenter",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        _page_cache[base] = r.text
    except Exception as e:
        print(f"   [warn]  AgendaCenter fetch failed: {e}")
        _page_cache[base] = ""
    return _page_cache[base]


def _sections(html: str):
    """Yield (committee_heading, body_html) tuples for each <h2> section."""
    return re.findall(r"<h2[^>]*>([^<]+)</h2>(.*?)(?=<h2|$)", html, re.DOTALL)


def _normalize_committee(s: str) -> str:
    """Lowercased, &↔and unified, type-word stripped — for fuzzy-matching a
    meeting title against the AgendaCenter section headings."""
    s = s.lower()
    s = re.sub(r"\s*-\s*\d.*$", "", s)  # drop date suffix
    s = s.replace(" and ", " & ")
    s = re.sub(r"\b(committee|commission|board)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_doc_url(agendas_cfg: dict, title: str) -> str | None:
    """Given 'Board of Trustees - 3/23/2026', return the agenda PDF URL whose
    section heading matches the committee. No cross-committee fallback —
    multiple meetings can share a date and the wrong PDF would get attached."""
    base = agendas_cfg["base"]
    date_m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", title)
    if not date_m:
        return None
    mo, dy, yr = date_m.group(1).zfill(2), date_m.group(2).zfill(2), date_m.group(3)
    date_token = f"_{mo}{dy}{yr}"

    html = _fetch_page(base)
    if not html:
        return None
    want = _normalize_committee(title)
    if not want:
        return None

    best = None
    for heading, body in _sections(html):
        norm = _normalize_committee(heading)
        if not norm:
            continue
        if want not in norm and norm not in want:
            continue
        ids = re.findall(
            rf"/AgendaCenter/ViewFile/Agenda/({re.escape(date_token)}-(\d+))", body)
        if ids:
            ids.sort(key=lambda t: int(t[1]), reverse=True)
            best = ids[0][0]
            break

    if best:
        return f"{base}/AgendaCenter/ViewFile/Agenda/{best}"
    return None


def fetch_doc_url_by_date(agendas_cfg: dict, date_str_mmddyyyy: str) -> str | None:
    """Best agenda PDF URL for a date (highest ID across committees)."""
    base = agendas_cfg["base"]
    html = _fetch_page(base)
    if not html:
        return None
    matches = re.findall(
        rf'/AgendaCenter/ViewFile/Agenda/(_{re.escape(date_str_mmddyyyy)}-(\d+))',
        html)
    if not matches:
        return None
    unique = list(dict.fromkeys(m[0] for m in matches))
    unique.sort(key=lambda x: int(x.rsplit("-", 1)[1]), reverse=True)
    return f"{base}/AgendaCenter/ViewFile/Agenda/{unique[0]}"


def fetch_agenda_text(agendas_cfg: dict, doc_url: str | None,
                      title: str) -> str | None:
    """Agenda text for a meeting: try the doc_url PDF, then scrape the
    AgendaCenter for a committee+date match."""
    base = agendas_cfg["base"]
    host = base.split("//", 1)[-1].removeprefix("www.")
    if doc_url and host in doc_url:
        text = fetch_pdf_text(doc_url, timeout=15)
        if text:
            return text
    scraped_url = fetch_doc_url(agendas_cfg, title)
    if scraped_url:
        return fetch_pdf_text(scraped_url, timeout=15)
    return None


def _norm_committee_key(name: str) -> str:
    """Canonical key for de-duping a committee across scrape + rules
    ('Community, Life and Public Safety (CLPS) Committee' vs
    'Community Life & Public Safety Committee' are the same body)."""
    s = name.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(committee|commission)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future agendas from the AgendaCenter page, enriched/filled by
    the fallback_rules projection."""
    from .rule_schedule import nth_weekday, months_between

    base = agendas_cfg["base"]
    today = date.today()
    end_date = today + timedelta(days=days_ahead)
    results: dict = {}

    html = _fetch_page(base)
    if html:
        sections = _sections(html)
        if not sections:
            raise AdapterStructureError(
                f"AgendaCenter at {base}: page fetched but no <h2> sections "
                f"matched — upstream HTML structure changed")
        for committee_raw, body in sections:
            committee = re.sub(r'\s+', ' ', committee_raw).strip()
            if not committee or len(committee) < 3:
                continue
            links = re.findall(
                r'href="(/AgendaCenter/ViewFile/Agenda/(_(\d{2})(\d{2})(\d{4})-(\d+)))"',
                body)
            for full_path, _path_id, mo, dy, yr, _num in links:
                try:
                    d = date(int(yr), int(mo), int(dy))
                except ValueError:
                    continue
                if today <= d <= end_date:
                    key = (d.isoformat(), _norm_committee_key(committee))
                    results[key] = {
                        "date": d.isoformat(),
                        "time": "",
                        "name": committee,
                        "url": f"{base}{full_path}",
                        "source": jurisdiction_key,
                    }
        print(f"  [ok]  {jurisdiction_key} AgendaCenter: {len(results)} posted future agendas")
    else:
        logger.warning("%s AgendaCenter scrape failed", jurisdiction_key)

    rules = agendas_cfg.get("fallback_rules") or []
    rule_added = 0
    for yr, mo in months_between(today, end_date):
        for rule in rules:
            meeting_date = nth_weekday(yr, mo, rule["weekday"], rule["nth"])
            if meeting_date is None or not (today <= meeting_date <= end_date):
                continue
            key = (meeting_date.isoformat(), _norm_committee_key(rule["name"]))
            if key not in results:
                results[key] = {
                    "date": meeting_date.isoformat(),
                    "time": rule["time"],
                    "name": rule["name"],
                    "url": f"{base}/agendacenter",
                    "source": jurisdiction_key,
                }
                rule_added += 1
            else:
                # Same body already posted by the scrape — enrich rather than
                # duplicate: fill the standard time, prefer the clean name.
                ex = results[key]
                if not ex.get("time"):
                    ex["time"] = rule["time"]
                ex["name"] = rule["name"]

    print(f"  [ok]  {jurisdiction_key} rule-based: {rule_added} additional meetings projected")
    return sorted(results.values(), key=lambda x: (x["date"], x["time"]))
