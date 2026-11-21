"""revize adapter — municipal websites built on Revize (agenda text, minutes, upcoming).

Config block: {"adapter": "revize", "portal": ..., "pages": [{"body": ..., "url": ...}],
               "bodies": [...], "days_ahead": N, "fallback_rules": [...]}
`portal` is the site's agendas-and-minutes index (the link a card falls back
to). Each `pages` entry is one body's page built with Revize's
"agendas_minutes" module: a row per meeting with the date, an optional meeting
type ("Regular Meeting", "Special Budget Hearing") and links labelled Agenda /
Packet / Minutes, mostly PDFs. `bodies` is an optional allow-list of body
names; without it every page is read.

There is no API. What identifies a meeting is the row's record number, which
Revize prints in the page as <a name="agendas_minutes_rz967"> and keeps for
the life of the row (the date and the file names can be edited; two bodies'
pages can link one agenda file for a joint meeting; one body can meet twice in
a day). So:
  - a meeting's doc_url is its agenda file plus that number as the fragment,
    ".../01 261117 VB Agenda.pdf#rz967": readers open the agenda, and
    fetch_minutes_text finds the row again by the number;
  - its id is three digits for the site (from the portal's host name, so two
    Revize villages in one instance can't collide) followed by the number.

Each page is fetched once per run and shared by list_recent, fetch_upcoming
and fetch_minutes_text. Rows marked cancelled, and "Quorum No Action" notices
(a quorum may be present somewhere, nothing is decided), are not meetings. A
joint meeting listed on two bodies' pages with the same agenda file is listed
once, under the body whose page comes first in `pages`.

The page gives no meeting times: a posted meeting takes the time of the
fallback rule that covers it.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
import zlib
from datetime import date, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests

from .. import net
from ..errors import AdapterStructureError
from ..pdftext import fetch_pdf_text
from .rule_schedule import name_tokens, project

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_RECORD = re.compile(r'name="agendas_minutes_rz(\d+)"')
_CELL = re.compile(r"(?is)<td\b[^>]*>(.*?)</td>")
_LINK = re.compile(r'(?is)<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>')
_BASE = re.compile(r'(?i)<base\b[^>]*\bhref="([^"]+)"')
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b\s*(.*)$", re.S)
_NOT_HELD = re.compile(r"cancel|no action", re.I)
# "Regular Meeting *Amended Agenda*", "Regular Meeting - REVISED": a note about the file, not part of the name.
_FILE_NOTE = re.compile(r"(?i)[\s\-–]*\b(?:agenda\s+)?(?:amended|revised)\b(?:[\s&]*(?:agenda|packet))*\s*$")
_DOC_REF = re.compile(r"#rz(\d+)$")
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Page URL -> its rows, for this run (one request per page however many functions ask).
_pages: dict[str, list[dict]] = {}


def _text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script\b.*?</script>", " ", fragment)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _file_url(base_url: str, href: str) -> str:
    """A linked file's address without Revize's upload stamp (?t=...), spaces encoded."""
    u = urlsplit(urljoin(base_url, html.unescape(href).strip()))
    return urlunsplit((u.scheme, u.netloc, quote(unquote(u.path), safe="/&(),"), "", ""))


def _name(body: str, title: str) -> str:
    title = _FILE_NOTE.sub("", title.replace("*", " ")).strip(" -–")
    title = " ".join(title.split())
    if not title:
        return body
    return title if body.lower() in title.lower() else f"{body} {title}"


def parse_page(page: str, page_url: str, body: str) -> list[dict]:
    """One body's rows: {record, date "YYYY-MM-DD", name, held, agenda, minutes, url}.
    `agenda` and `minutes` are file addresses or None; `url` is the doc_url (the
    agenda when there is one, else the row's own anchor on the page)."""
    low = page.lower()
    base = _BASE.search(page)
    base_url = base.group(1) if base else page_url
    rows = []
    for anchor in _RECORD.finditer(page):
        start = low.rfind("<table", 0, anchor.start())
        if start < 0 or "</table" in low[start:anchor.start()]:
            continue        # a year heading's own record, not a meeting row
        row = page[start:anchor.start()]
        cells = _CELL.findall(row)
        m = _DATE.match(_text(cells[0])) if cells else None
        if not m:
            continue
        year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        try:
            day = date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        files: dict[str, str] = {}
        for href, label in _LINK.findall(row):
            label = _text(label).lower()
            kind = "minutes" if "minutes" in label else "agenda" if "agenda" in label and "packet" not in label else None
            if kind:
                files.setdefault(kind, _file_url(base_url, href))
        record = anchor.group(1)
        agenda = files.get("agenda")
        rows.append({
            "record": record,
            "date": day.isoformat(),
            "name": _name(body, m.group(4)),
            "held": not _NOT_HELD.search(m.group(4)),
            "agenda": agenda,
            "minutes": files.get("minutes"),
            "url": f"{agenda}#rz{record}" if agenda else f"{page_url}#agendas_minutes_rz{record}",
        })
    return rows


def _rows(agendas_cfg: dict):
    """Every configured body's rows, page by page in `pages` order. Raises
    UpstreamUnavailableError when a page doesn't answer and
    AdapterStructureError when one answers without a single meeting row."""
    wanted = agendas_cfg.get("bodies")
    for page in agendas_cfg["pages"]:
        if wanted and page["body"] not in wanted:
            continue
        url = page["url"]
        if url not in _pages:
            rows = parse_page(net.get(url, headers=_HEADERS).text, url, page["body"])
            if not rows:
                raise AdapterStructureError(
                    f"Revize page {url}: fetched but no meeting rows matched — "
                    f"upstream HTML structure changed")
            _pages[url] = rows
        yield from _pages[url]


def _meetings(agendas_cfg: dict) -> list[dict]:
    """Rows that are meetings, a joint meeting (same date, same agenda file) once."""
    seen, out = set(), []
    for r in _rows(agendas_cfg):
        key = (r["date"], r["agenda"])
        if not r["held"] or (r["agenda"] and key in seen):
            continue
        seen.add(key)
        out.append(r)
    return out


def _site_number(agendas_cfg: dict) -> str:
    host = (urlsplit(agendas_cfg["portal"]).netloc or agendas_cfg["portal"]).lower().removeprefix("www.")
    return f"{zlib.crc32(host.encode()) % 1000:03d}"


def _docx_text(url: str) -> str | None:
    """A .docx file's paragraphs as lines of text."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        if r.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
        lines = ("".join("\t" if n.tag == _WORD + "tab" else n.text or ""
                         for n in p.iter() if n.tag in (_WORD + "t", _WORD + "tab"))
                 for p in root.iter(_WORD + "p"))
        text = "\n".join(line.strip() for line in lines if line.strip())
        return text if len(text) > 100 else None
    except Exception as e:
        print(f"       Revize .docx text extraction failed: {e}")
        return None


def _document_text(url: str) -> str | None:
    """A posted file's text: a PDF or a .docx. None for a scan with no text
    layer and for a legacy .doc, which can't be read here."""
    kind = PurePosixPath(urlsplit(url).path).suffix.lower()
    text = fetch_pdf_text(url) if kind == ".pdf" else _docx_text(url) if kind == ".docx" else None
    # Bullets set in a symbol font come out as private-use characters.
    return re.sub(r"[-]", "", text).strip() if text else None


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The agenda's text from the file a doc_url names. None when the URL
    isn't one of this adapter's (a row with no agenda posted links its page)."""
    if not _DOC_REF.search(doc_url or ""):
        return None
    return _document_text(doc_url.split("#")[0])


def fetch_minutes_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The posted minutes' text for the meeting a doc_url names: its row is
    found again by record number and the Minutes link read. None until
    minutes are posted. Raises UpstreamUnavailableError when the page
    doesn't answer."""
    ref = _DOC_REF.search(doc_url or "")
    if not ref:
        return None
    row = next((r for r in _rows(agendas_cfg) if r["record"] == ref.group(1)), None)
    return _document_text(row["minutes"]) if row and row["minutes"] else None


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Listed future meetings, plus the fallback_rules projection for dates the
    site hasn't listed yet. A projection is dropped when a row on that date
    covers it (a cancelled row included, so a cancelled meeting isn't projected
    back in), and a listed meeting it covers takes its time. Raises
    UpstreamUnavailableError when the site doesn't answer."""
    today = date.today().isoformat()
    end = (date.today() + timedelta(days=days_ahead)).isoformat()
    listed = [r for r in _rows(agendas_cfg) if today <= r["date"] <= end]
    posted = {r["record"]: {"date": r["date"], "time": "", "name": r["name"], "url": r["url"],
                            "source": jurisdiction_key}
              for r in _meetings(agendas_cfg) if today <= r["date"] <= end}
    print(f"  [ok]  {jurisdiction_key} Revize: {len(posted)} listed future meetings")

    rules = agendas_cfg.get("fallback_rules") or []
    projected = []
    for entry in project(rules, jurisdiction_key, days_ahead, default_url=agendas_cfg["portal"]):
        want = name_tokens(entry["name"])
        covering = [r for r in listed if r["date"] == entry["date"] and want <= name_tokens(r["name"])]
        if not covering:
            projected.append(entry)
        for r in covering:
            if r["record"] in posted and not posted[r["record"]]["time"]:
                posted[r["record"]]["time"] = entry["time"]
    if rules:
        print(f"  [ok]  {jurisdiction_key} rule-based: {len(projected)} additional meetings projected")
    return sorted([*posted.values(), *projected], key=lambda x: (x["date"], x["time"]))


def list_recent(agendas_cfg: dict, since: date) -> list[dict]:
    """Past meetings with a posted agenda, from `since` through today, newest
    first: {id, name, date, url}. Raises UpstreamUnavailableError when a page
    doesn't answer and AdapterStructureError when one has no meeting rows."""
    today, site = date.today().isoformat(), _site_number(agendas_cfg)
    rows = [r for r in _meetings(agendas_cfg) if since.isoformat() <= r["date"] <= today and r["agenda"]]
    rows.sort(key=lambda r: (r["date"], int(r["record"])), reverse=True)
    return [{"id": site + r["record"], "name": r["name"], "date": r["date"], "url": r["url"]} for r in rows]
