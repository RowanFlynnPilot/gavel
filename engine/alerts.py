"""The alert feed — what is new enough to tell a subscriber about.

    python -m engine.alerts                      # update data/alerts.json
    python -m engine.alerts --since 2027-03-22T11:00:00+00:00    # list what is new since then

Email alerts tell a subscriber when a board or an issue they follow is on an
agenda, and what was decided afterward. This module does the part that needs
no subscriber: each run it adds one entry for

  - "agenda": an upcoming meeting whose posted agenda has been previewed
    (engine.previews), the first time the tracker has that preview
  - "result": a displayed meeting summarized from a recording, minutes or the
    official vote record, the first time the tracker has that summary (so an
    agenda-only summary upgraded later gets its entry then)

    {id, kind, source, board, meeting, date "2027-03-09", time, issues: [key],
     text, items?, hearings?, publicComment?, contested?, url, first_seen}

`first_seen` is when the entry entered the feed, which is what a sender
compares with its last send (`new_since`). Sending is not here: who receives
what is the email service's business, and no subscriber's address is ever
read or written by the tracker, whose code and run logs are public.

An entry is made once. `seen` in the file remembers every id the feed has
considered, so a meeting put back on the displayed list, or a preview written
again for a revised agenda, is not announced twice. A meeting first met more
than RESULT_MAX_AGE_DAYS after it was held is recorded as seen and not
announced. Boards that are tracked but not published are left out; when one
is published, its upcoming meetings enter the feed then. Entries leave the
file after KEEP_DAYS; `seen` stays.

No network, no Claude calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR, MEETINGS_JSON, UPCOMING_JSON, InstanceConfig, load_instance, utf8_console
from .previews import preview_key
from .tipsheet import _held, meeting_url, vote_leads

ALERTS_JSON = DATA_DIR / "alerts.json"
KEEP_DAYS = 45                 # how long an entry stays in the feed
RESULT_MAX_AGE_DAYS = 90       # a summary first met later than this after its meeting is not news
TEXT_CHARS = 420


def _clip(text: str, limit: int = TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…"


def _issues(cfg: InstanceConfig, keys) -> list[str]:
    valid = {i["key"] for i in cfg.issues}
    return [k for k in keys or [] if isinstance(k, str) and k in valid]


def _agenda_entry(cfg: InstanceConfig, e: dict, stamp: str) -> dict:
    p = e["preview"]
    jur = cfg.by_key[e["source"]]
    return {
        "id": "a_" + preview_key(e), "kind": "agenda", "source": jur.key, "board": jur.name,
        "meeting": e.get("name", ""), "date": e["date"], "time": e.get("time", ""),
        "issues": _issues(cfg, p.get("issues")),
        "text": _clip(p.get("overview", "")),
        "items": [{"item": i.get("item", ""), "action": i.get("action", "discussion")}
                  for i in p.get("items") or [] if isinstance(i, dict) and i.get("item")],
        "hearings": [h for h in p.get("hearings") or [] if isinstance(h, str) and h.strip()],
        "publicComment": bool(p.get("publicComment")),
        "url": e.get("url") or cfg.tracker_page_url or cfg.newsroom_url,
        "first_seen": stamp,
    }


def _result_entry(cfg: InstanceConfig, m: dict, held: date, stamp: str) -> dict:
    jur = cfg.by_key[m["source"]]
    return {
        "id": "r_" + m["id"], "kind": "result", "source": jur.key, "board": jur.name,
        "meeting": m.get("title", ""), "date": held.isoformat(), "time": "",
        "issues": _issues(cfg, m.get("issues")),
        "text": _clip(m.get("overview", "")),
        "contested": [{"item": lead["item"], "why": lead["why"]} for lead in vote_leads(m)],
        "url": meeting_url(cfg, m["id"]),
        "first_seen": stamp,
    }


def build(cfg: InstanceConfig, feed: dict | None, *, now: datetime | None = None,
          meetings: list[dict] | None = None, upcoming: dict[str, list] | None = None) -> dict:
    """The feed after this run: {"entries": [...newest first...], "seen": {id: first_seen}}."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    today = now.date()
    feed = feed if isinstance(feed, dict) else {}
    seen: dict[str, str] = dict(feed.get("seen") or {})
    entries: list[dict] = [e for e in feed.get("entries") or [] if isinstance(e, dict)]
    hidden = cfg.hidden_keys

    for source, events in (upcoming or {}).items():
        if source in hidden or source not in cfg.by_key:
            continue
        for e in events:
            if not e.get("preview") or e.get("date", "") < today.isoformat():
                continue
            eid = "a_" + preview_key(e)
            if eid not in seen:
                seen[eid] = stamp
                entries.append(_agenda_entry(cfg, e, stamp))

    for m in meetings or []:
        if not isinstance(m, dict) or m.get("source") in hidden or m.get("source") not in cfg.by_key \
                or m.get("isAgendaOnly"):
            continue
        held = _held(m)
        eid = "r_" + str(m.get("id"))
        if held is None or held > today or eid in seen:
            continue
        seen[eid] = stamp
        if (today - held).days <= RESULT_MAX_AGE_DAYS:
            entries.append(_result_entry(cfg, m, held, stamp))

    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    entries = [e for e in entries if e.get("first_seen", "") >= cutoff]
    entries.sort(key=lambda e: (e["first_seen"], e["date"], e["id"]), reverse=True)
    return {"entries": entries, "seen": dict(sorted(seen.items()))}


def new_since(feed: dict, since: str) -> list[dict]:
    """Entries that entered the feed after `since` (an ISO time), oldest first:
    what a send covers when the last one went out at `since`."""
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    out = [e for e in feed.get("entries") or []
           if datetime.fromisoformat(e["first_seen"]) > start]
    return sorted(out, key=lambda e: (e["first_seen"], e["date"], e["id"]))


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="Update the alert feed, or list what is new in it.")
    ap.add_argument("--since", help="list entries newer than this ISO time; write nothing")
    ap.add_argument("--out", default=str(ALERTS_JSON))
    args = ap.parse_args()
    out = Path(args.out)
    if args.since:
        for e in new_since(_load(out, {}), args.since):
            print(f"{e['first_seen']}  {e['kind']:6s}  {e['date']}  {e['board']}: {e['meeting']}")
        return 0
    before = _load(out, {})
    feed = build(load_instance(), before, meetings=_load(MEETINGS_JSON, []),
                 upcoming=_load(UPCOMING_JSON, {}))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    had = {e.get("id") for e in before.get("entries") or []} if isinstance(before, dict) else set()
    added = [e for e in feed["entries"] if e["id"] not in had]
    print(f"[alerts]  {len(added)} new entr{'y' if len(added) == 1 else 'ies'} "
          f"({sum(1 for e in added if e['kind'] == 'agenda')} agenda, "
          f"{sum(1 for e in added if e['kind'] == 'result')} result); "
          f"{len(feed['entries'])} in the feed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
