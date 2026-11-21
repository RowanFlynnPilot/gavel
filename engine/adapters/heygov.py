"""heygov adapter — HeyGov Meetings, the meetings product behind Town Web's newer sites (agendas, minutes, upcoming).

Config block: {"adapter": "heygov", "site": "townofwestonwi.com",
               "portal": "https://townofwestonwi.com/agendas-minutes",
               "bodies": ["Town Board", ...] (optional), "days_ahead": N,
               "fallback_rules": [...] (optional)}
`site` is the municipality's HeyGov name (the data-heygov-jurisdiction value in
its pages, usually its domain); `portal` is its Agendas & Minutes page, the
link a rule-projected meeting gets. `bodies` keeps only the meeting categories
named (exact names, the headings on that page); without it every category is
read.

The Agendas & Minutes page is drawn in the browser from api.heygov.com, and
this adapter sends the same requests: /<site>/meetings?api=1&order=desc (every
meeting, newest first; the API ignores date filters) and
/<site>/meetings/categories (the bodies). One meeting, with the agenda HeyGov
parsed out of the uploaded file, comes from /<site>/meetings/<id>?expand=agenda_items,
the request HeyGov's own public meeting page makes. Files are on
files.heygov.com.

A meeting's doc_url is that public page, app.heygov.com/<site>/meetings/<id>.
HeyGov's public meeting id is a ULID ("me_01m4v446q0..."); the engine's id is
the same value written as a decimal number, so it never changes and no two
meetings share one. HeyGov has no meeting times here (every meeting starts at
midnight), so an upcoming meeting's time is "" unless one is set.

Agendas and minutes are whatever the clerk uploads: PDF, .docx or legacy .doc.
A .doc can't be read. For an agenda HeyGov's parsed item list stands in; for
minutes there is nothing to stand in, and the meeting keeps its agenda summary.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import requests

from .. import net
from ..errors import AdapterStructureError
from .townweb import document_text, meeting_name, merge_rules

logger = logging.getLogger(__name__)

API = "https://api.heygov.com"
FILES = "https://files.heygov.com/"
APP = "https://app.heygov.com"
_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
_PAGE_SIZE = 25
_ULID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"     # Crockford base 32
_PID = re.compile(r"^me_[0-9a-hjkmnp-tv-z]{26}$")
_CANCELLED = re.compile(r"cancel", re.I)


def numeric_id(pid: str) -> str:
    """HeyGov's public meeting id ("me_" + a ULID) as digits. Raises
    AdapterStructureError when the id isn't that shape: a different id scheme
    must not quietly give every meeting a new id."""
    pid = (pid or "").lower()
    if not _PID.match(pid):
        raise AdapterStructureError(f"HeyGov meeting id {pid!r} is not 'me_' and a 26-character ULID")
    n = 0
    for ch in pid[3:]:
        n = n * 32 + _ULID_ALPHABET.index(ch)
    return str(n)


def meeting_url(agendas_cfg: dict, pid: str) -> str:
    """A meeting's doc_url: its public page on HeyGov."""
    return f"{APP}/{agendas_cfg['site']}/meetings/{pid}"


def _pid(doc_url: str) -> str | None:
    m = re.search(r"/meetings/(me_[0-9a-z]{26})(?:[/?#]|$)", doc_url or "")
    return m.group(1) if m else None


def _meetings(agendas_cfg: dict, start: date) -> list[dict]:
    """Published and cancelled meetings dated `start` or later, newest first,
    as {pid, date, time, title, categories, has_agenda, cancelled}. The list is
    read a page at a time until it reaches meetings older than `start`. HeyGov
    doesn't keep same-day meetings in a fixed order, so they are put in one
    here (and a meeting that turns up on two pages is taken once).
    Raises UpstreamUnavailableError when HeyGov doesn't answer and
    AdapterStructureError when the answer isn't a list of meetings."""
    url = f"{API}/{agendas_cfg['site']}/meetings"
    meetings, page = {}, 1
    while True:
        batch = net.get_json(url, params={"api": 1, "order": "desc", "limit": _PAGE_SIZE, "page": page},
                             headers=_HEADERS)
        if not isinstance(batch, list) or (page == 1 and not batch):
            # Every site this adapter is pointed at has meetings; none at all is a changed API, not a quiet month.
            raise AdapterStructureError(f"HeyGov meetings at {url}: expected a list of meetings, got "
                                        f"{'an empty list' if isinstance(batch, list) else type(batch).__name__}")
        oldest = None
        for m in batch:
            local = m.get("starts_at_local") if isinstance(m, dict) else None
            if not local or not m.get("id") or not re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", local):
                raise AdapterStructureError(f"HeyGov meetings at {url}: a meeting has no id or starts_at_local")
            oldest = date.fromisoformat(local[:10])
            status = m.get("status") or ""
            if oldest < start or status not in ("publish", "canceled"):
                continue
            hour, minute = int(local[11:13]), int(local[14:16])
            meetings[m["id"]] = {
                "pid": m["id"],
                "date": local[:10],
                "time": "" if (hour, minute) == (0, 0) else f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}",
                "title": " ".join((m.get("title") or "").split()),
                "categories": m.get("categories") or [],
                "has_agenda": m.get("agenda_status") == "public" and bool(m.get("agenda_file_url")),
                "cancelled": status == "canceled" or bool(_CANCELLED.search(m.get("title") or "")),
            }
        if len(batch) < _PAGE_SIZE or oldest < start:
            break
        page += 1
    return sorted(meetings.values(), key=lambda m: (m["date"], m["pid"]), reverse=True)


def _category_names(agendas_cfg: dict) -> dict:
    """{category id: name} — the bodies, as the Agendas & Minutes page heads them."""
    url = f"{API}/{agendas_cfg['site']}/meetings/categories"
    cats = net.get_json(url, headers=_HEADERS)
    if not isinstance(cats, list) or not all(isinstance(c, dict) and "id" in c and c.get("name") for c in cats):
        raise AdapterStructureError(f"HeyGov categories at {url}: expected a list of {{id, name}}")
    return {c["id"]: " ".join(c["name"].split()) for c in cats}


def _listing(agendas_cfg: dict, start: date, end: date) -> list[dict]:
    """The configured bodies' meetings in start..end, each with its name.
    The categories are only asked for when there is a meeting to name."""
    meetings = [m for m in _meetings(agendas_cfg, start) if m["date"] <= end.isoformat()]
    if not meetings:
        return []
    names = _category_names(agendas_cfg)
    wanted = set(agendas_cfg.get("bodies") or [])
    out = []
    for m in meetings:
        bodies = [names[c] for c in m["categories"] if c in names]
        if m["categories"] and not bodies:
            raise AdapterStructureError(f"HeyGov meeting {m['pid']}: its categories {m['categories']} aren't in the category list")
        if wanted and not wanted & set(bodies):
            continue
        body = next((b for b in bodies if not wanted or b in wanted), "")
        out.append({**m, "name": meeting_name(m["title"], body)})
    return out


def list_recent(agendas_cfg: dict, since: date) -> list[dict]:
    """Past meetings with a posted agenda, from `since` through today, newest
    first: {id, name, date, url}. Cancelled meetings are left out. Two
    requests (the list and the categories) for the usual window."""
    return [{"id": numeric_id(m["pid"]), "name": m["name"], "date": m["date"], "url": meeting_url(agendas_cfg, m["pid"])}
            for m in _listing(agendas_cfg, since, date.today()) if m["has_agenda"] and not m["cancelled"]]


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str, days_ahead: int) -> list[dict]:
    """Posted future meetings (the clerk posts a meeting about a week ahead),
    plus the fallback_rules projection for dates not posted yet. Raises
    UpstreamUnavailableError when HeyGov doesn't answer."""
    today = date.today()
    listing = _listing(agendas_cfg, today, today + timedelta(days=days_ahead))

    def entry(m):
        return {"date": m["date"], "time": m["time"], "name": m["name"],
                "url": meeting_url(agendas_cfg, m["pid"]), "source": jurisdiction_key}
    posted = [entry(m) for m in listing if not m["cancelled"]]
    print(f"  [ok]  {jurisdiction_key}: {len(posted)} posted future meetings from HeyGov")
    return merge_rules(posted, [entry(m) for m in listing if m["cancelled"]],
                       agendas_cfg, jurisdiction_key, days_ahead)


def _meeting(agendas_cfg: dict, doc_url: str) -> dict | None:
    """The meeting a doc_url names, with its parsed agenda items, or None when
    the URL isn't a meeting's (a rule-projected meeting links to the portal)
    or the meeting is gone. Raises when HeyGov doesn't answer."""
    pid = _pid(doc_url)
    if not pid:
        return None
    r = requests.get(f"{API}/{agendas_cfg['site']}/meetings/{pid}", params={"expand": "agenda_items"},
                     headers=_HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def items_text(items: list[dict]) -> str | None:
    """HeyGov's parsed agenda as an outline: "3. Unfinished Business" with
    "3.1 ..." under it, each item's note below its title when it has one."""
    children: dict = {}
    for i in items:
        children.setdefault(i.get("parent_id") or 0, []).append(i)

    def outline(parent, prefix, depth):
        for n, i in enumerate(sorted(children.get(parent, []), key=lambda x: (x.get("order") or 0, x.get("id") or 0)), 1):
            number = f"{prefix}{n}"
            yield f"{'  ' * depth}{number}{'.' if not depth else ''} {' '.join((i.get('title') or '').split())}"
            if (i.get("text") or "").strip():
                yield f"{'  ' * (depth + 1)}{' '.join(i['text'].split())}"
            yield from outline(i.get("id"), f"{number}.", depth + 1)

    text = "\n".join(outline(0, "", 0))
    return text if len(text) > 100 else None


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The agenda's text: the uploaded file when it's a PDF or .docx,
    otherwise (a .doc, a scan) the item list HeyGov parsed from it. None when
    no agenda is public or neither can be read."""
    m = _meeting(agendas_cfg, doc_url)
    if not m or m.get("agenda_status") != "public":
        return None
    path = m.get("agenda_file_path")
    return (document_text(FILES + path) if path else None) or items_text(m.get("agenda_items") or [])


def fetch_minutes_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The posted minutes' text, or None until minutes are public — and None
    for minutes uploaded as a legacy .doc, which can't be read."""
    m = _meeting(agendas_cfg, doc_url)
    if not m or m.get("minutes_status") != "public" or not m.get("meeting_minutes_file_path"):
        return None
    return document_text(FILES + m["meeting_minutes_file_path"])
