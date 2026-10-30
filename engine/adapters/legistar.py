"""legistar adapter — Legistar Web API (agenda items, votes, upcoming).

Config block: {"adapter": "legistar", "client": ..., "days_ahead": N}
`client` is the city's Legistar name: webapi.legistar.com/v1/<client>, the
same name as <client>.legistar.com (sanjose, seattle, cityofcleveland).
A meeting's doc_url is its Legistar page (EventInSiteURL,
.../MeetingDetail.aspx?LEGID=<event id>&...).

Votes: an action with a pass/fail flag becomes one vote record, with the roll
call from /eventitems/<id>/votes. Actions with no flag ("postponed",
"ordered published") aren't votes, so they're left out rather than reported
as passed or failed. Many cities post actions only once minutes are final.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
_YES = re.compile(r"^(aye|yes|yea|in favor|affirmative)$", re.I)
_NO = re.compile(r"^(nay|no|opposed|against)$", re.I)
_ABSTAIN = re.compile(r"^(abstain\w*|recus\w*)$", re.I)


def _api(agendas_cfg: dict) -> str:
    return f"https://webapi.legistar.com/v1/{agendas_cfg['client']}"


def _event_id(doc_url: str) -> int | None:
    m = re.search(r"[?&]LEGID=(\d+)", doc_url or "", re.I)
    return int(m.group(1)) if m else None


def _title(item: dict) -> str:
    """An item's title on one line (titles can carry line breaks, like a clerk's signature block)."""
    return " ".join((item.get("EventItemTitle") or "").split())


def _get(url: str, timeout: int = 15):
    r = requests.get(url, headers=_HEADERS, timeout=timeout)
    return r.json() if r.status_code == 200 else None


def _roll_call(base: str, item_id: int) -> dict:
    """Names by vote: {'yes': [...], 'no': [...], 'abstain': [...]}; absent
    and excused members are in none of them."""
    out = {"yes": [], "no": [], "abstain": []}
    for v in _get(f"{base}/eventitems/{item_id}/votes") or []:
        value, name = (v.get("VoteValueName") or "").strip(), v.get("VotePersonName") or ""
        for key, pattern in (("yes", _YES), ("no", _NO), ("abstain", _ABSTAIN)):
            if pattern.match(value):
                out[key].append(name)
    return out


def _parse_item(base: str, item: dict) -> dict:
    votes = []
    if item.get("EventItemPassedFlag") is not None:
        votes.append({
            "motion": item.get("EventItemActionName") or "",
            "passed": item.get("EventItemPassedFlag") == 1,
            "initiator": item.get("EventItemMover") or "",
            "seconder": item.get("EventItemSeconder") or "",
            **_roll_call(base, item["EventItemId"]),
        })
    docs = [
        {
            "name": a.get("MatterAttachmentName", ""),
            "url": a.get("MatterAttachmentHyperlink"),
            "type": PurePosixPath(urlparse(a.get("MatterAttachmentHyperlink") or "").path).suffix.lstrip("."),
            "size": 0,
        }
        for a in item.get("EventItemMatterAttachments") or [] if a.get("MatterAttachmentName")
    ]
    kind = item.get("EventItemMatterType")
    return {
        "number": (item.get("EventItemAgendaNumber") or "").strip().rstrip("."),
        "name": _title(item),
        "desc": " ".join(filter(None, [item.get("EventItemMatterFile"), f"({kind})" if kind else None])),  # "CB 121297 (Council Bill (CB))"
        "votes": votes,
        "docs": docs,
        "children": [],
    }


def fetch_meeting_data(agendas_cfg: dict, doc_url: str) -> dict | None:
    """Given a Legistar meeting URL, fetch its agenda items with actions,
    roll calls, and attachments. Returns {'event_id', 'items': [...]} in the
    civicclerk adapter's shape, or None on failure."""
    event_id = _event_id(doc_url)
    if not event_id:
        return None
    base = _api(agendas_cfg)
    try:
        items = _get(f"{base}/events/{event_id}/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1")
        if items is None:
            return None
        parsed = [_parse_item(base, i) for i in items if _title(i)]
        return {"event_id": event_id, "items": parsed}
    except Exception as e:
        print(f"   [warn]  Legistar fetch failed: {e}")
        return None


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """The agenda as Legistar lists it: each item's number, title and file
    ("1. An ordinance relating to ... [CB 121297]"), section headings included.
    A third the size of the agenda PDF's text, which is mostly interpretation
    and access instructions, so a big city's agenda fits the prompt."""
    event_id = _event_id(doc_url)
    if not event_id:
        return None
    try:
        items = _get(f"{_api(agendas_cfg)}/events/{event_id}/eventitems") or []
        lines = [" ".join(filter(None, [(i.get("EventItemAgendaNumber") or "").strip(), _title(i),
                                        f"[{i['EventItemMatterFile']}]" if i.get("EventItemMatterFile") else None]))
                 for i in items if _title(i)]
        text = "\n".join(lines)
        return text if len(text) > 100 else None
    except Exception as e:
        print(f"       Legistar agenda fetch failed: {e}")
        return None


def _cancelled(e: dict) -> bool:
    return any("CANCEL" in (e.get(k) or "").upper() for k in
               ("EventBodyName", "EventComment", "EventAgendaStatusName", "EventMinutesStatusName"))


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future meetings from the Events endpoint."""
    today = date.today()
    end = today + timedelta(days=days_ahead)
    flt = quote(f"EventDate ge datetime'{today}' and EventDate le datetime'{end}'")
    try:
        events = _get(f"{_api(agendas_cfg)}/events?$filter={flt}&$orderby=EventDate&$top=30") or []
    except Exception as e:
        logger.warning("%s Legistar fetch failed: %s", jurisdiction_key, e)
        return []

    results = [{
        "date": e["EventDate"][:10],
        "time": e.get("EventTime") or "",
        "name": (e.get("EventBodyName") or "").strip(),
        "url": e.get("EventInSiteURL") or "",
        "source": jurisdiction_key,
    } for e in events if not _cancelled(e)]
    print(f"  [ok]  {jurisdiction_key}: {len(results)} upcoming events from Legistar")
    return results
