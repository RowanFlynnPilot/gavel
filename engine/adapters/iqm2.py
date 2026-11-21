"""iqm2 adapter — Granicus IQM2 (MinuteTraq) citizen portal.

Config block: {"adapter": "iqm2", "base": "https://<client>.iqm2.com",
               "portal": ".../Citizens/Default.aspx", "bodies": [...],
               "minutes_upgrade_window_days": N, "days_ahead": N,
               "fallback_rules": [...], "request_gap_seconds": N}
`request_gap_seconds` (optional) spaces this adapter's requests to the portal
that many seconds apart; set it to the Crawl-delay in the portal's robots.txt
(60 on Granicus-hosted portals as of Nov. 2026).
`bodies` (optional) keeps only the listed boards, by the portal's own name
for each ("Common Council"); without it every board on the portal is included.
A meeting's doc_url is its portal page,
<base>/Citizens/Detail_Meeting.aspx?ID=<meeting id>; the id is IQM2's own.

Listing: Calendar.aspx?From=&To= puts every meeting in a date range on one
page, with its status and its Agenda / Minutes links. One request serves a
whole run: list_recent asks for everything from the minutes window back to
days_ahead forward and keeps it, fetch_upcoming reads the same page, and the
two text functions use it to answer "nothing posted yet" without a request
(IQM2's robots.txt asks crawlers to wait 60 seconds between requests).

Agenda text: the outline on the meeting's page. The "Agenda" download is the
same outline as a PDF, and for a council meeting it carries the whole packet
(37 MB for Mosinee's Sept. 14, 2026 meeting), so it is never fetched.
Minutes text: the "Minutes" PDF (FileOpen.aspx?Type=12), where each item has
its RESULT / MOVER / SECONDER / AYES lines. Minutes posted as a Word document
(a FileView.aspx link) are not read.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

import requests

from .. import net
from ..errors import AdapterStructureError
from ..pdftext import fetch_pdf_text
from .rule_schedule import covered_by_posted, project

_HEADERS = {"User-Agent": "Mozilla/5.0"}
MINUTES_WINDOW_DAYS = 75   # engine.pipeline's default wait for minutes
DAYS_AHEAD = 60            # engine.upcoming's default look-ahead

# base -> {"start", "end", "rows"}: the calendar range read this run.
_CALENDARS: dict[str, dict] = {}

_ROW = re.compile(r'<div class="Row MeetingRow')
_ROW_LINK = re.compile(r'class="RowLink"><a href="[^"]*Detail_Meeting\.aspx\?ID=(\d+)"\s+title="([^"]*)"\s*>([^<]*)</a>')
_AGENDA_LINK = re.compile(r"File(?:Open|View)\.aspx\?Type=14&(?:amp;)?ID=\d+")
_MINUTES_LINK = re.compile(r"FileOpen\.aspx\?Type=12&(?:amp;)?ID=(\d+)")


def _base(agendas_cfg: dict) -> str:
    return agendas_cfg["base"].rstrip("/")


def _meeting_id(doc_url: str) -> str | None:
    m = re.search(r"/Detail_Meeting\.aspx\?(?:.*&)?ID=(\d+)", doc_url or "", re.I)
    return m.group(1) if m else None


def _one_line(text: str) -> str:
    return re.sub(r"[\s\xa0]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _minutes_pdf(base: str, link: re.Match | None) -> str | None:
    return f"{base}/Citizens/FileOpen.aspx?Type=12&ID={link.group(1)}&Inline=True" if link else None


def parse_calendar(page: str, base: str) -> list[dict]:
    """Every meeting on a Calendar.aspx list page, oldest first: {id, when,
    date, time, body, name, cancelled, agenda, minutes_url, url}. Raises
    AdapterStructureError when the page isn't the meeting list or a row can't
    be read; a list with no meetings in it is an empty list."""
    if 'id="ContentPlaceholder1_pnlMeetings"' not in page:
        raise AdapterStructureError(f"IQM2 {base}: calendar page fetched but it has no meeting list")
    meetings = []
    for row in _ROW.split(page)[1:]:
        row = row.split('class="RowBottom"')[0]   # the links and status sit in RowTop; the last row runs on into the footer
        link = _ROW_LINK.search(row)
        # The link's title is the hover card: date, then "Board:", "Type:" and "Status:" lines.
        card = dict(re.findall(r"(Board|Type|Status):\t([^\r\n]*)", html.unescape(link.group(2)))) if link else {}
        stamp = _one_line(link.group(3)) if link else ""
        when = None
        for fmt in ("%b %d, %Y %I:%M %p", "%b %d, %Y"):   # a meeting with no set time shows the date alone
            try:
                when = datetime.strptime(stamp, fmt)
                break
            except ValueError:
                pass
        body = _one_line(card.get("Board", ""))
        if not (link and when and body):
            raise AdapterStructureError(
                f"IQM2 {base}: a calendar row has no meeting link, date or board; upstream HTML structure changed")
        minutes = _MINUTES_LINK.search(row)
        meetings.append({
            "id": link.group(1),
            "when": when,
            "date": when.date().isoformat(),
            "time": when.strftime("%I:%M %p").lstrip("0") if ":" in stamp else "",
            "body": body,
            "name": " - ".join(filter(None, [body, _one_line(card.get("Type", ""))])),
            "cancelled": "cancel" in card.get("Status", "").lower() or "MeetingCancelled" in row,
            "agenda": bool(_AGENDA_LINK.search(row)),
            "minutes_url": _minutes_pdf(base, minutes),
            "url": f"{base}/Citizens/Detail_Meeting.aspx?ID={link.group(1)}",
        })
    return sorted(meetings, key=lambda m: m["when"])


def _calendar(agendas_cfg: dict, start: date, end: date) -> list[dict]:
    """The portal's meetings from start through end, oldest first. Reads the
    calendar once per run: a range already covered costs no request. Raises
    UpstreamUnavailableError when the portal doesn't answer."""
    base = _base(agendas_cfg)
    held = _CALENDARS.get(base)
    if held is None or start < held["start"] or end > held["end"]:
        if held:
            start, end = min(start, held["start"]), max(end, held["end"])
        _pace(agendas_cfg)
        r = net.get(f"{base}/Citizens/Calendar.aspx?From={start.month}/{start.day}/{start.year}"
                    f"&To={end.month}/{end.day}/{end.year}", headers=_HEADERS)
        held = _CALENDARS[base] = {"start": start, "end": end, "rows": parse_calendar(r.text, base)}
    return held["rows"]


def _listed(agendas_cfg: dict, meeting_id: str) -> dict | None:
    """The meeting's calendar row, if the calendar read this run has it."""
    rows = _CALENDARS.get(_base(agendas_cfg), {}).get("rows", [])
    return next((m for m in rows if m["id"] == meeting_id), None)


def _wanted(agendas_cfg: dict, m: dict) -> bool:
    bodies = agendas_cfg.get("bodies")
    return not m["cancelled"] and (not bodies or m["body"] in bodies)


def _pace(agendas_cfg: dict) -> None:
    net.pace(_base(agendas_cfg), float(agendas_cfg.get("request_gap_seconds", 0)))


def _page(agendas_cfg: dict, url: str) -> str | None:
    _pace(agendas_cfg)
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        return r.text if r.status_code == 200 else None
    except requests.RequestException as e:
        print(f"       IQM2 meeting page fetch failed: {e}")
        return None


def agenda_text(page: str) -> str | None:
    """The agenda in a meeting's page: its heading (board, meeting type, date,
    place), then the outline's items, indented by level. Attachment rows are
    left out: file names, which also name licence and permit applicants. None
    for a cancelled meeting or a page with no agenda on it yet."""
    start = page.find("id='MeetingDetail'")
    if start < 0 or "class='MeetingCancelled'" in page:
        return None
    lines = []
    for row in re.findall(r"(?s)<tr>(.*?)</tr>", page[start:page.find("</table>", start)]):
        if "FileOpen.aspx" in row:
            continue
        cells = [_one_line(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)]
        depth = next((i for i, c in enumerate(cells) if c), 0)
        text = " ".join(c for c in cells if c)
        if text:
            lines.append("  " * depth + text)
    if not lines:
        return None
    heading = re.search(r'(?s)class="MeetingHeading">(.*?)<input', page)
    head = [_one_line(x) for x in re.split(r"(?i)<br\s*/?>|(?=<div)", heading.group(1))] if heading else []
    return "\n".join([x for x in head if x] + [""] + lines)


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The agenda's text from the meeting's portal page, or None when no
    agenda is posted."""
    meeting_id = _meeting_id(doc_url)
    if not meeting_id:
        return None
    listed = _listed(agendas_cfg, meeting_id)
    if listed and not listed["agenda"]:
        return None
    page = _page(agendas_cfg, doc_url)
    return agenda_text(page) if page else None


def fetch_minutes_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The posted minutes' text (the Minutes PDF), or None until minutes are
    posted. A meeting older than the calendar read this run is looked up on
    its own page."""
    meeting_id = _meeting_id(doc_url)
    if not meeting_id:
        return None
    listed = _listed(agendas_cfg, meeting_id)
    if listed:
        minutes_url = listed["minutes_url"]
    else:
        minutes_url = _minutes_pdf(_base(agendas_cfg),
                                   _MINUTES_LINK.search(_page(agendas_cfg, doc_url) or ""))
    if minutes_url:
        _pace(agendas_cfg)
    text = fetch_pdf_text(minutes_url, timeout=60) if minutes_url else None
    # Each PDF page ends with a "Page N" line, sometimes in the middle of an item's vote.
    return re.sub(r"(?m)^Page \d+\n?", "", text).strip() if text else None


def list_recent(agendas_cfg: dict, since: date) -> list[dict]:
    """Past meetings with a posted agenda, from `since` through today, newest
    first: {id, name, date, url}. Cancelled meetings are left out. Raises
    UpstreamUnavailableError when the portal doesn't answer and
    AdapterStructureError when its calendar can't be read."""
    today = date.today()
    back = today - timedelta(days=int(agendas_cfg.get("minutes_upgrade_window_days", MINUTES_WINDOW_DAYS)))
    ahead = today + timedelta(days=int(agendas_cfg.get("days_ahead", DAYS_AHEAD)))
    rows = _calendar(agendas_cfg, min(since, back), ahead)
    return [{"id": m["id"], "name": m["name"], "date": m["date"], "url": m["url"]}
            for m in reversed(rows)
            if since.isoformat() <= m["date"] <= today.isoformat() and m["agenda"] and _wanted(agendas_cfg, m)]


def _clock(time: str) -> datetime:
    return datetime.strptime(time, "%I:%M %p") if time else datetime.min


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Meetings on the portal's calendar from today through days_ahead,
    agenda posted or not, plus the fallback_rules projection for months the
    portal hasn't scheduled yet. Raises UpstreamUnavailableError when the
    portal doesn't answer."""
    today = date.today()
    end = today + timedelta(days=days_ahead)
    listed = [m for m in _calendar(agendas_cfg, today, end)
              if today.isoformat() <= m["date"] <= end.isoformat()]
    results = [{"date": m["date"], "time": m["time"], "name": m["name"], "url": m["url"],
                "source": jurisdiction_key} for m in listed if _wanted(agendas_cfg, m)]
    print(f"  [ok]  {jurisdiction_key}: {len(results)} upcoming meetings from IQM2")

    rules = agendas_cfg.get("fallback_rules") or []
    # Checked against everything the portal lists, so a meeting it shows as
    # cancelled isn't projected back onto the calendar.
    projected = [e for e in project(rules, jurisdiction_key, days_ahead, default_url=agendas_cfg["portal"])
                 if not covered_by_posted(e, listed)]
    if rules:
        print(f"  [ok]  {jurisdiction_key} rule-based: {len(projected)} additional meetings projected")
    return sorted(results + projected, key=lambda x: (x["date"], _clock(x["time"])))
