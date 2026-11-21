"""diligent adapter — Diligent Community public portal (agenda, minutes, upcoming).

Config block: {"adapter": "diligent", "base": "https://<name>.community.highbond.com",
               "portal": ".../Portal/MeetingTypeList.aspx", "days_ahead": N,
               "bodies": [...], "fallback_rules": [...], "adopted_minutes_only": bool}
`base` is the portal's origin; `portal` the page readers are sent to when a
meeting has no page of its own. A meeting's doc_url is its public page,
<base>/Portal/MeetingInformation.aspx?Id=<meeting id>, and its id is that number.

The portal's pages are filled in by its own JSON service, which this adapter
reads the way those pages do:
  /Services/MeetingsService.svc/meetings?from=&to=&loadall=true   (the Calendar page's call)
  /Services/MeetingsService.svc/meetings/<id>/meetingDocuments    (the meeting page's call)
A meeting is listed once the clerk publishes it, which is when its agenda goes
up and can be days before it is held (Mosinee listed nothing 25 days ahead of
its next regular meeting), so `fallback_rules` carry the upcoming list until then.

Documents: the agenda is the open-session HTML agenda, which arrives inside
the documents response. Minutes are the documents the meeting page puts behind
its "Minutes" button: the adopted ("Signed") minutes when the clerk has filed
them, otherwise the minutes as published before adoption (the PDF is headed
"Draft"). `adopted_minutes_only` waits for the adopted ones. Closed-session
versions and documents with a public release date still ahead are never read.
The JSON's `IsPublic` flag is not what decides any of this: it is false on the
agenda packet and on every minutes document the public page links.

`bodies` (optional) keeps only the named meeting types, spelled as this adapter
names them: without the "Notice of" some clerks put in front.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

from .. import net
from ..errors import AdapterStructureError, UpstreamUnavailableError
from ..pdftext import fetch_pdf_text
from .rule_schedule import covered_by_posted

_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

# Document types, as the meeting page's own script names them (packageDocumentTypes).
_AGENDA_HTML = 1                # publishedAgendaOpenHtml
_MINUTES_ADOPTED = (55, 53)     # adoptedOpenMinutesHtml, adoptedOpenMinutesPdf
_MINUTES_PUBLISHED = (9, 10)    # publishedMinutesOpenHtml, publishedMinutesOpenPdf (before adoption)
_TYPE_AGENDA, _TYPE_MINUTES = 1, 2

_DATE_SUFFIX = re.compile(r"\s*-\s*[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}\s*$")   # " - Oct 27 2026", "- February 10, 2025"
_ITEM_MARKER = re.compile(r"^(\d{1,3}|[A-Za-z]{1,2})[.)]$")
_ADJOURN = re.compile(r"(?im)^\s*\d+\.\s*adjourn(?:ment|ed)?\b(?!\s+(?:in)?to\b)")
_PAGE_FOOTER = re.compile(r"(?m)^Page \d+ of \d+[ \t]*$")


def _base(agendas_cfg: dict) -> str:
    return agendas_cfg["base"].rstrip("/")


def meeting_url(agendas_cfg: dict, meeting_id) -> str:
    """A meeting's public page: the doc_url this adapter reads."""
    return f"{_base(agendas_cfg)}/Portal/MeetingInformation.aspx?Id={int(meeting_id)}"


def _meeting_id(doc_url: str) -> int | None:
    m = re.search(r"/MeetingInformation\.aspx\?(?:[^#]*&)?Id=(\d+)", doc_url or "", re.I)
    return int(m.group(1)) if m else None


def _clean(name: str) -> str:
    return re.sub(r"(?i)^notice of\s+", "", " ".join((name or "").split()))


def _body(row: dict) -> str:
    """The meeting type, Diligent's word for the body: "Personnel Committee Meeting"."""
    return _clean(row.get("MeetingTypeName") or "")


def _name(row: dict) -> str:
    """The meeting's own name without the date the portal appends. Usually the
    meeting type, but the clerk can retitle one meeting ("Annual Meeting")."""
    return _clean(_DATE_SUFFIX.sub("", row.get("Name") or "")) or _body(row)


def _wanted(agendas_cfg: dict, row: dict) -> bool:
    bodies = agendas_cfg.get("bodies")
    if "CANCEL" in f"{row.get('Name')} {row.get('MeetingTypeName')}".upper():
        return False
    if row.get("ExternalCalendar"):     # an entry from another calendar, not a meeting of this portal
        return False
    return not bodies or _body(row) in bodies


def _listing(agendas_cfg: dict, start: date, end: date) -> list[dict]:
    """The portal's meetings from start through end. Raises
    UpstreamUnavailableError when it doesn't answer and AdapterStructureError
    when the answer isn't the list of meetings this adapter knows. The service
    is asked for a day more on each side, because it answers a one-day range
    (from = to) with nothing; the rows are cut to the range here."""
    url = (f"{_base(agendas_cfg)}/Services/MeetingsService.svc/meetings"
           f"?from={start - timedelta(days=1)}&to={end + timedelta(days=1)}&loadall=true")
    rows = net.get_json(url, headers=_HEADERS)
    if not isinstance(rows, list) or not all(
            isinstance(r, dict) and str(r.get("Id", "")).isdigit() and r.get("Name")
            and re.match(r"\d{4}-\d{2}-\d{2}", str(r.get("MeetingDate") or "")) for r in rows):
        raise AdapterStructureError(
            f"Diligent {_base(agendas_cfg)}: the meetings service answered, but not with a list of "
            f"meetings carrying Id, Name and MeetingDate — the service's structure changed")
    return [r for r in rows if start.isoformat() <= r["MeetingDate"][:10] <= end.isoformat()]


def list_recent(agendas_cfg: dict, since: date) -> list[dict]:
    """Past meetings with a posted agenda, from `since` through today, newest
    first: {id, name, date, url}. A meeting is listed with its agenda once it
    is published; one that isn't yet is left out. One request."""
    today = date.today()
    results = [{
        "id": str(int(r["Id"])),
        "name": _name(r),
        "date": r["MeetingDate"][:10],
        "url": meeting_url(agendas_cfg, r["Id"]),
    } for r in _listing(agendas_cfg, since, today) if r.get("Published") and _wanted(agendas_cfg, r)]
    return sorted(results, key=lambda m: (m["date"], int(m["id"])), reverse=True)


def html_text(doc: str) -> str:
    """A Diligent HTML document (agenda or minutes) as lines of text, each item
    on one line with its number ("12. New Business", "a. Discussion: ...")."""
    start = doc.find("<body")
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", doc[max(start, 0):])
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|table)>", "\n", body)
    lines: list[str] = []
    for raw in html.unescape(re.sub(r"<[^>]+>", " ", body)).splitlines():
        line = re.sub(r"[ \t\xa0]+", " ", raw).strip()
        if not line:
            continue
        if lines and _ITEM_MARKER.match(lines[-1]):     # the number sits in its own table cell
            lines[-1] += " " + line
        else:
            lines.append(line)
    return "\n".join(lines)


def minutes_proper(text: str) -> str | None:
    """The minutes themselves out of a minutes PDF's text. The PDF is a packet:
    the minutes, then every attachment, among them the previous meeting's
    minutes with their own motions and votes. The minutes end on the page that
    carries the numbered adjournment item, or on the next one when that item's
    heading is the last line of its page. None when there is no such item,
    because the whole packet would put another meeting's votes in the summary."""
    adjourn = _ADJOURN.search(text or "")
    if not adjourn:
        return None
    heading_end = text.find("\n", adjourn.end())
    footer = _PAGE_FOOTER.search(text, adjourn.end())
    if footer and heading_end >= 0 and not text[heading_end:footer.start()].strip():
        footer = _PAGE_FOOTER.search(text, footer.end())
    kept = _PAGE_FOOTER.sub("", text[:footer.start()] if footer else text)
    lines = (x.strip() for x in kept.splitlines())
    return "\n".join(line for line in lines if line and line != "Page")     # "Page" heads the attachment-index column


def _documents(agendas_cfg: dict, doc_url: str, kind: int) -> dict[int, dict]:
    """The meeting's agenda or minutes documents the public page shows, by
    document type. Empty when the URL isn't a meeting page or the portal
    doesn't answer (the next run asks again)."""
    meeting_id = _meeting_id(doc_url)
    if not meeting_id:
        return {}
    url = f"{_base(agendas_cfg)}/Services/MeetingsService.svc/meetings/{meeting_id}/meetingDocuments"
    try:
        data = net.get_json(url, headers=_HEADERS, attempts=1)
    except UpstreamUnavailableError as e:
        print(f"       Diligent documents fetch failed: {e}")
        return {}
    docs = data.get("Documents") if isinstance(data, dict) else None
    return {d.get("DocumentType"): d for d in docs or []
            if isinstance(d, dict) and d.get("Type") == kind and not d.get("PublicReleaseDelayDateString")}


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The agenda's text from the meeting's HTML agenda, or None when none is posted. One request."""
    doc = _documents(agendas_cfg, doc_url, _TYPE_AGENDA).get(_AGENDA_HTML)
    text = html_text(doc.get("Html") or "") if doc else ""
    return text if len(text) > 100 else None


def fetch_minutes_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The minutes' text, motions and votes included, or None until minutes are
    posted. Adopted minutes come before published ones, HTML before PDF. One
    request, two when the minutes are a PDF."""
    docs = _documents(agendas_cfg, doc_url, _TYPE_MINUTES)
    kinds = _MINUTES_ADOPTED if agendas_cfg.get("adopted_minutes_only") else _MINUTES_ADOPTED + _MINUTES_PUBLISHED
    doc = next((docs[k] for k in kinds if k in docs), None)
    if not doc:
        return None
    if doc.get("Html"):
        text = html_text(doc["Html"])
    elif (doc.get("Format") or "").lower() == "pdf":
        packet = fetch_pdf_text(f"{_base(agendas_cfg)}/document/{int(doc['Id'])}", timeout=60)
        text = minutes_proper(packet) if packet else None
        if packet and not text:
            print(f"       Diligent minutes PDF for meeting {_meeting_id(doc_url)} has no numbered "
                  f"adjournment item to end the minutes at; not read")
    else:
        text = None
    return text if text and len(text) > 100 else None


def _clock(time_str: str) -> int:
    """Minutes past midnight of "6:00 PM", for sorting; 0 when there is no time."""
    try:
        t = datetime.strptime(time_str.strip(), "%I:%M %p")
    except ValueError:
        return 0
    return t.hour * 60 + t.minute


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future meetings, plus the fallback_rules projection for the ones
    the clerk hasn't published yet. One request. Raises
    UpstreamUnavailableError when the portal doesn't answer, rather than
    falling back to rules alone: the last published list beats a projection."""
    from .rule_schedule import project

    today = date.today()
    end = today + timedelta(days=days_ahead)
    posted = [{
        "date": r["MeetingDate"][:10],
        "time": (r.get("MeetingTime") or "").strip().lstrip("0"),
        "name": _name(r),
        "url": meeting_url(agendas_cfg, r["Id"]),
        "source": jurisdiction_key,
    } for r in _listing(agendas_cfg, today, end) if _wanted(agendas_cfg, r)]
    print(f"  [ok]  {jurisdiction_key} Diligent: {len(posted)} posted future meetings")

    results = list(posted)
    rules = agendas_cfg.get("fallback_rules") or []
    taken = {(m["date"], m["name"]) for m in posted}
    for entry in project(rules, jurisdiction_key, days_ahead, default_url=agendas_cfg["portal"]):
        if (entry["date"], entry["name"]) not in taken and not covered_by_posted(entry, posted):
            results.append(entry)
    if rules:
        print(f"  [ok]  {jurisdiction_key} rule-based: {len(results) - len(posted)} additional meetings projected")
    return sorted(results, key=lambda m: (m["date"], _clock(m["time"]), m["name"]))
