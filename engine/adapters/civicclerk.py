"""civicclerk adapter — CivicClerk OData API (agendas, votes, upcoming).

Config block: {"adapter": "civicclerk", "api_base": ..., "portal": ...,
               "days_ahead": N}
Note: CivicClerk stores local times with a Z suffix (not true UTC) — the
Z is stripped, never converted.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def fetch_meeting_data(agendas_cfg: dict, doc_url: str) -> dict | None:
    """Given a portal event URL, extract the event ID, look up its agendaId,
    then fetch agenda items, motions, votes, and attachments.
    Returns {'event_id', 'agenda_id', 'items': [...]}, or None on failure."""
    base = agendas_cfg["api_base"]
    event_id_m = re.search(r"/event/(\d+)", doc_url)
    if not event_id_m:
        return None
    event_id = int(event_id_m.group(1))

    try:
        r = requests.get(f"{base}/Events/{event_id}", headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        agenda_id = r.json().get("agendaId")
        if not agenda_id:
            return None

        r2 = requests.get(f"{base}/Meetings/{agenda_id}", headers=_HEADERS, timeout=10)
        if r2.status_code != 200:
            return None
        meeting = r2.json()

        def parse_item(item):
            votes = []
            for v in item.get("minutesItemVotes", []):
                votes.append({
                    "motion": v.get("motionName", ""),
                    "passed": v.get("passFail") == 1,
                    "initiator": v.get("initiatedBy", ""),
                    "seconder": v.get("secondedBy", ""),
                    "yes": v.get("yesVotes", []),
                    "no": v.get("noVotes", []),
                    "abstain": v.get("abstainVotes", []),
                })
            docs = [
                {
                    "name": a.get("fileName", ""),
                    "url": f"{base}/Meetings/GetAttachmentFile(fileId={a['id']})"
                           if a.get("id") else None,
                    "type": a.get("contentType", ""),
                    "size": a.get("fileSize", 0),
                }
                for a in item.get("attachmentsList", []) if a.get("fileName")
            ]
            children = [parse_item(c) for c in item.get("childItems", [])]
            return {
                "number": item.get("agendaObjectItemOutlineNumber", ""),
                "name": item.get("agendaObjectItemName", ""),
                "desc": item.get("agendaObjectItemDescription", "") or "",
                "votes": votes,
                "docs": docs,
                "children": children,
            }

        items = [parse_item(i) for i in meeting.get("items", [])]
        return {"event_id": event_id, "agenda_id": agenda_id, "items": items}
    except Exception as e:
        print(f"   [warn]  CivicClerk fetch failed: {e}")
        return None


def fetch_agenda_text(agendas_cfg: dict, doc_url: str) -> str | None:
    """Fetch the agenda's plain text via the published-files blob URL —
    no PDF parsing needed."""
    base = agendas_cfg["api_base"]
    event_id_m = re.search(r"/event/(\d+)", doc_url)
    if not event_id_m:
        return None
    event_id = int(event_id_m.group(1))
    try:
        r = requests.get(f"{base}/Events/{event_id}", headers=_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        agenda_id = r.json().get("agendaId")
        if not agenda_id:
            return None
        r2 = requests.get(f"{base}/Meetings/{agenda_id}", headers=_HEADERS, timeout=10)
        if r2.status_code != 200:
            return None
        pub_files = r2.json().get("publishedFiles", [])
        agenda_file = next(
            (f for f in pub_files if f.get("type", "").lower() == "agenda"),
            pub_files[0] if pub_files else None,
        )
        if not agenda_file:
            return None
        file_id = agenda_file.get("fileId")
        if not file_id:
            return None
        r3 = requests.get(
            f"{base}/Meetings/GetMeetingFile(fileId={file_id},plainText=true)",
            headers=_HEADERS, timeout=10,
        )
        if r3.status_code != 200:
            return None
        blob_url = r3.json().get("blobUri", "")
        if not blob_url:
            return None
        r4 = requests.get(blob_url, timeout=15)
        if r4.status_code == 200 and len(r4.text) > 100:
            return r4.text.strip()
    except Exception as e:
        print(f"       CivicClerk agenda fetch failed: {e}")
    return None


def fetch_upcoming(agendas_cfg: dict, jurisdiction_key: str,
                   days_ahead: int) -> list[dict]:
    """Posted future events from the OData Events endpoint."""
    base = agendas_cfg["api_base"]
    portal = agendas_cfg["portal"].rstrip("/")
    today = date.today()
    end = today + timedelta(days=days_ahead)

    url = (
        f"{base}/Events"
        f"?%24filter=eventDate%20ge%20{today}T00%3A00%3A00Z"
        f"%20and%20eventDate%20le%20{end}T23%3A59%3A59Z"
        "%20and%20isDeleted%20eq%20false"
        "&%24orderby=eventDate&%24top=30"
    )
    try:
        r = requests.get(url, headers=_HEADERS, timeout=10)
        events = r.json().get("value", [])
    except Exception as e:
        logger.warning("%s CivicClerk fetch failed: %s", jurisdiction_key, e)
        return []

    results = []
    for e in events:
        name = e.get("eventName", "")
        if "CANCEL" in name.upper() or "POSSIBLE QUORUM" in name.upper():
            continue
        raw = e.get("eventDate", "")
        date_part = raw[:10]
        time_part = ""
        if "T" in raw:
            h, m = int(raw[11:13]), int(raw[14:16])
            ap = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            time_part = f"{h12}:{m:02d} {ap}"
        results.append({
            "date": date_part,
            "time": time_part,
            "name": name,
            "url": f"{portal}/event/{e['id']}/overview",
            "source": jurisdiction_key,
        })
    print(f"  [ok]  {jurisdiction_key}: {len(results)} upcoming events from CivicClerk")
    return results
