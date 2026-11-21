"""townweb adapter — Town Web's meeting repository on a WordPress site (agendas, minutes, upcoming).

Config block: {"adapter": "townweb", "base": "https://villageofmaine.org",
               "portal": "https://villageofmaine.org/agendas-minutes/",
               "bodies": ["Board of Trustees", ...] (optional), "days_ahead": N,
               "fallback_rules": [...] (optional)}
`base` is the site; `portal` is its Agendas & Minutes page, the link a
rule-projected meeting gets. `bodies` keeps only the repository categories
named (exact names, as the page's headings show them); without it every
category is read.

The Agendas & Minutes page is drawn in the browser from the site's own REST
API, and this adapter sends the same request the page does:
/wp-json/wp/v2/twd_repository, one record per meeting, with its meeting date,
its category (the body) and, with _embed=1, its agenda and minutes files. The
plugin also puts each meeting on the site calendar (The Events Calendar,
/wp-json/tribe/events/v1/events). That calendar is the only place a meeting's
start time is given and the place a cancellation is marked ("Cancelled –
Finance Committee Meeting"): the repository record of a cancelled meeting
keeps its name and can keep its agenda.

A meeting's id is its repository record's WordPress post id. Its doc_url is
that record's own address, <base>/?post_type=twd_repository&p=<id>, which the
site redirects to the Agendas & Minutes page.

Also here, for the heygov adapter (Town Web's other meetings product): reading
a posted document, naming a meeting after its body, and merging the
fallback_rules projection into the posted meetings.
"""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from .. import net
from ..errors import AdapterStructureError, UpstreamUnavailableError
from ..pdftext import fetch_pdf_text
from .rule_schedule import covered_by_posted, project

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_CANCELLED = re.compile(r"cancel", re.I)          # "Cancelled", "Canceled", "Cancellation"
_PER_PAGE = 100                                   # the most WordPress gives in one page
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MAX_DOCX_XML = 20_000_000                        # bytes of word/document.xml; minutes run to a few hundred KB


# ── Shared with the heygov adapter ──────────────────────────────────────────

def _squash(s: str) -> str:
    """A name reduced to its words, for comparing: lowercase, "&" as "and"."""
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower().replace("&", " and ")))


def meeting_name(title: str, body: str) -> str:
    """What a meeting is called: its title, with its body in front when the
    title alone doesn't say whose meeting it is ("Public Hearing" filed under
    Plan Commission -> "Plan Commission: Public Hearing")."""
    title, body = " ".join((title or "").split()), " ".join((body or "").split())
    if not body or _squash(body) in _squash(title):
        return title or body
    return f"{body}: {title}" if title else body


def _docx_paragraph(p) -> str:
    parts = []
    for el in p.iter():
        if el.tag == _W + "t":
            parts.append(el.text or "")
        elif el.tag == _W + "tab":
            parts.append("\t")
        elif el.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _docx_lines(parent):
    """Paragraphs in order; a table row is one line, its cells tab-separated
    (roll calls and vote tables are tables)."""
    for el in parent:
        if el.tag == _W + "p":
            yield _docx_paragraph(el)
        elif el.tag == _W + "tbl":
            for row in el.iter(_W + "tr"):
                yield "\t".join(" ".join(filter(None, (_docx_paragraph(p) for p in cell.iter(_W + "p"))))
                                for cell in row.findall(_W + "tc")).strip()
        else:
            yield from _docx_lines(el)


def docx_text(data: bytes) -> str | None:
    """The text of a .docx file, or None if it isn't one or has under 100
    characters. Typed numbering is kept; Word's automatic list numbers are
    not in the text and are lost."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.getinfo("word/document.xml").file_size > _MAX_DOCX_XML:
                return None
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        print(f"       .docx text extraction failed: {e}")
        return None
    body = root.find(_W + "body")
    text = "\n".join(line for line in _docx_lines(body if body is not None else root) if line)
    return text if len(text) > 100 else None


def document_text(url: str) -> str | None:
    """The text of a posted agenda or minutes file: a PDF or a .docx. A legacy
    .doc (Word 97-2003) can't be read with what the engine has, so it gives
    None, like a scanned PDF with no text layer."""
    ext = PurePosixPath(urlparse(url or "").path).suffix.lower()
    if ext == ".pdf":
        return fetch_pdf_text(url)
    if ext == ".docx":
        try:
            r = requests.get(url, headers=_HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"       .docx fetch failed: {e}")
            return None
        return docx_text(r.content) if r.status_code == 200 else None
    print(f"       Can't read this file type ({ext or 'no extension'}): {url}")
    return None


def merge_rules(posted: list[dict], cancelled: list[dict], agendas_cfg: dict,
                jurisdiction_key: str, days_ahead: int) -> list[dict]:
    """Posted future meetings plus the fallback_rules projection for dates the
    board hasn't posted yet, sorted by date then time (the BoardBook merge).
    A projection is dropped when a posted meeting on its date covers it, and
    also when a cancelled one does: a cancelled regular meeting must not come
    back as its own projection."""
    results = {(m["date"], m["name"][:30]): m for m in posted}
    rules = agendas_cfg.get("fallback_rules") or []
    known = posted + cancelled
    added = 0
    for entry in project(rules, jurisdiction_key, days_ahead, default_url=agendas_cfg["portal"]):
        key = (entry["date"], entry["name"][:30])
        if key not in results and not covered_by_posted(entry, known):
            results[key] = entry
            added += 1
    if rules:
        print(f"  [ok]  {jurisdiction_key} rule-based: {added} additional meetings projected")
    return sorted(results.values(), key=lambda x: (x["date"], x["time"]))


# ── The repository ──────────────────────────────────────────────────────────

def _base(agendas_cfg: dict) -> str:
    return agendas_cfg["base"].rstrip("/")


def record_url(agendas_cfg: dict, record_id) -> str:
    """A meeting's doc_url: its repository record's address on the site."""
    return f"{_base(agendas_cfg)}/?post_type=twd_repository&p={record_id}"


def _record_id(doc_url: str) -> str | None:
    if "post_type=twd_repository" not in (doc_url or ""):
        return None
    m = re.search(r"[?&]p=(\d+)", doc_url)
    return m.group(1) if m else None


def _file(record: dict, field: str) -> dict | None:
    """A record's attached file ({guid: its address, post_title, ...}), or None
    when there is none (the API gives "")."""
    value = record.get(field)
    if not value:
        return None
    if not isinstance(value, dict) or not value.get("guid"):
        raise AdapterStructureError(
            f"Town Web record {record.get('id')}: '{field}' is {value!r}, not a file with an address — "
            f"the repository API no longer expands attachments")
    return value


def _bodies(record: dict) -> list[str]:
    """The record's repository categories, by name."""
    names = [html.unescape(t.get("name") or "").strip()
             for group in (record.get("_embedded") or {}).get("wp:term") or []
             for t in group if isinstance(t, dict) and t.get("taxonomy") == "twd_repository_cat"]
    if record.get("twd_repository_cat") and not names:
        raise AdapterStructureError(
            f"Town Web record {record.get('id')}: has categories but the API didn't name them")
    return names


def _records(agendas_cfg: dict, start: date, end: date) -> list[dict]:
    """Repository records whose meeting date is in start..end, as
    {id, date, title, bodies, agenda, minutes, notes}. The date filter is the
    one the page's own date search sends. Raises UpstreamUnavailableError when
    the site doesn't answer and AdapterStructureError when the answer isn't a
    list of meeting records."""
    url = f"{_base(agendas_cfg)}/wp-json/wp/v2/twd_repository"
    params = {
        "_embed": 1, "per_page": _PER_PAGE,
        "filter[orderby]": "meta_value", "filter[meta_key]": "meeting_date",
        "filter[meta_query][0][key]": "meeting_date", "filter[meta_query][0][value]": start.isoformat(),
        "filter[meta_query][0][compare]": ">=", "filter[meta_query][0][type]": "date",
        "filter[meta_query][1][key]": "meeting_date", "filter[meta_query][1][value]": end.isoformat(),
        "filter[meta_query][1][compare]": "<=", "filter[meta_query][1][type]": "date",
    }
    raw, page = [], 1
    while True:
        r = net.get(url, params={**params, "page": page}, headers=_HEADERS)
        try:
            batch = r.json()
        except ValueError as e:
            raise UpstreamUnavailableError(f"{url}: response was not JSON: {e}") from e
        if not isinstance(batch, list):
            raise AdapterStructureError(f"Town Web repository at {url}: expected a list of records, got {type(batch).__name__}")
        raw += batch
        if page >= int(r.headers.get("X-WP-TotalPages") or 1):
            break
        page += 1

    records = []
    for rec in raw:
        if not isinstance(rec, dict) or not rec.get("id") or "meeting_date" not in rec or "title" not in rec:
            raise AdapterStructureError(f"Town Web repository at {url}: a record has no id, title or meeting_date")
        try:
            d = date.fromisoformat(rec["meeting_date"] or "")
        except ValueError:
            continue            # a record the clerk gave no meeting date: it isn't on the page's date search either
        if not start <= d <= end:
            continue            # the site ignored the date filter; the dates are still right
        records.append({
            "id": str(rec["id"]),
            "date": d.isoformat(),
            "title": " ".join(html.unescape((rec["title"] or {}).get("rendered") or "").split()),
            "bodies": _bodies(rec),
            "agenda": _file(rec, "agenda"),
            "minutes": _file(rec, "meeting_minutes"),
            "notes": rec.get("notes") or "",
        })
    if raw and not records and all(not r.get("meeting_date") for r in raw):
        raise AdapterStructureError(f"Town Web repository at {url}: {len(raw)} records and none has a meeting date")
    return records


def _event_key(day: str, title: str) -> tuple[str, str]:
    """A calendar event and its repository record share a date and a name;
    the calendar's copy can carry a "Cancelled –" in front."""
    return day, _squash(re.sub(r"cancel\w*", " ", html.unescape(title or ""), flags=re.I))


def _events(agendas_cfg: dict, start: date, end: date) -> dict:
    """The site calendar for start..end: {(date, name): {time, cancelled}}."""
    url = f"{_base(agendas_cfg)}/wp-json/tribe/events/v1/events"
    events, page = {}, 1
    while True:
        data = net.get_json(url, params={"start_date": start.isoformat(), "end_date": end.isoformat(),
                                         "per_page": 50, "page": page}, headers=_HEADERS)
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise AdapterStructureError(f"Town Web calendar at {url}: expected {{events: [...]}}")
        for e in data["events"]:
            try:
                when = datetime.strptime(e["start_date"], "%Y-%m-%d %H:%M:%S")
            except (KeyError, TypeError, ValueError) as err:
                raise AdapterStructureError(f"Town Web calendar at {url}: an event has no readable start_date") from err
            title = e.get("title") or ""
            events[_event_key(when.date().isoformat(), title)] = {
                "time": "" if e.get("all_day") else when.strftime("%I:%M %p").lstrip("0"),
                "cancelled": bool(_CANCELLED.search(html.unescape(title))),
            }
        if page >= int(data.get("total_pages") or 1):
            break
        page += 1
    return events


def _listing(agendas_cfg: dict, start: date, end: date) -> list[dict]:
    """The configured bodies' meetings in start..end, each with its name, its
    start time from the calendar ("" when it isn't there) and whether it was
    cancelled: on the calendar, or in the record's own name, notes or agenda
    file name ("...Agenda_cancelled.pdf")."""
    wanted = set(agendas_cfg.get("bodies") or [])
    records = [r for r in _records(agendas_cfg, start, end) if not wanted or wanted & set(r["bodies"])]
    events = _events(agendas_cfg, start, end) if records else {}
    for r in records:
        event = events.get(_event_key(r["date"], r["title"]), {})
        body = next((b for b in r["bodies"] if not wanted or b in wanted), "")
        r["name"] = meeting_name(r["title"], body)
        r["time"] = event.get("time", "")
        r["cancelled"] = bool(event.get("cancelled") or _CANCELLED.search(r["title"]) or _CANCELLED.search(r["notes"])
                              or _CANCELLED.search((r["agenda"] or {}).get("post_title") or ""))
    return records


def list_recent(agendas_cfg: dict, since: date) -> list[dict]:
    """Past meetings with a posted agenda, from `since` through today, newest
    first: {id, name, date, url}. Cancelled meetings are left out. Two
    requests: the repository and the calendar."""
    meetings = [m for m in _listing(agendas_cfg, since, date.today()) if m["agenda"] and not m["cancelled"]]
    return [{"id": m["id"], "name": m["name"], "date": m["date"], "url": record_url(agendas_cfg, m["id"])}
            for m in sorted(meetings, key=lambda m: (m["date"], int(m["id"])), reverse=True)]


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str, days_ahead: int) -> list[dict]:
    """Posted future meetings (the clerk adds a meeting to the repository a
    week or so ahead), plus the fallback_rules projection for dates not posted
    yet. Raises UpstreamUnavailableError when the site doesn't answer."""
    today = date.today()
    listing = _listing(agendas_cfg, today, today + timedelta(days=days_ahead))

    def entry(m):
        return {"date": m["date"], "time": m["time"], "name": m["name"],
                "url": record_url(agendas_cfg, m["id"]), "source": jurisdiction_key}
    posted = [entry(m) for m in listing if not m["cancelled"]]
    print(f"  [ok]  {jurisdiction_key}: {len(posted)} posted future meetings from Town Web")
    return merge_rules(posted, [entry(m) for m in listing if m["cancelled"]],
                       agendas_cfg, jurisdiction_key, days_ahead)


def _record(agendas_cfg: dict, doc_url: str) -> dict | None:
    """The repository record a doc_url names, or None when the URL isn't a
    record's (a rule-projected meeting links to the portal) or the clerk has
    removed the record. Raises when the site doesn't answer."""
    record_id = _record_id(doc_url)
    if not record_id:
        return None
    r = requests.get(f"{_base(agendas_cfg)}/wp-json/wp/v2/twd_repository/{record_id}",
                     params={"_embed": 1}, headers=_HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The text of the meeting's agenda file, or None if none is posted."""
    record = _record(agendas_cfg, doc_url)
    agenda = _file(record, "agenda") if record else None
    return document_text(agenda["guid"]) if agenda else None


def fetch_minutes_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The text of the meeting's minutes file, or None until minutes are posted."""
    record = _record(agendas_cfg, doc_url)
    minutes = _file(record, "meeting_minutes") if record else None
    return document_text(minutes["guid"]) if minutes else None
