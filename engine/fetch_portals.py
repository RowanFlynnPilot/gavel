"""Residential fetcher for portals that refuse cloud servers.

    python -m engine.fetch_portals [--source KEY] [--dry-run]

Runs on the operator's machine (the same scheduled task that fetches
transcripts) for every jurisdiction whose `agendas` block says
`"fetch_from": "residential"`. It calls the real adapter from this
connection and writes portal_cache/<key>.json with what CI will need; the
refresh script commits and pushes it. See engine/portal_cache.py for the file
and for how CI reads it. No API key is needed and Claude is never called.

What is fetched, to keep both the requests and the committed text small:
  - the listing of recent meetings and the upcoming calendar (always);
  - for a meeting CI hasn't created yet: its minutes if posted, else its agenda;
  - for a meeting still summarized from its agenda: its minutes, once posted;
  - for an upcoming meeting within the preview window: its agenda.
Nothing is fetched for a meeting already summarized from minutes.

The file is rewritten when its contents change, and otherwise once a day so
CI can tell a quiet board from a fetcher that has stopped.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import portal_cache
from .adapters import get_adapter
from .config import MINUTES_ADAPTERS, PORTAL_PREFIX, InstanceConfig, load_instance, utf8_console
from .state import load_state

PREVIEW_DAYS = 8            # engine.previews' window
PORTAL_DAYS = 30            # engine.pipeline's default look-back
HEARTBEAT_HOURS = 20


def _summary_source(info: dict) -> str | None:
    sf = info.get("summary_file") or ""
    path = Path(sf.replace(".md", "_summary.json")) if sf else None
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("_source")
    except json.JSONDecodeError:
        return None


def collect(cfg: InstanceConfig, key: str, state: dict, today: date | None = None) -> dict:
    """Fetch one jurisdiction's pending documents with its real adapter.
    Raises whatever the adapter raises when the site doesn't answer."""
    today = today or date.today()
    jur = cfg.jurisdiction(key)
    adapter = get_adapter(jur.agendas_adapter)
    has_minutes = jur.agendas_adapter in MINUTES_ADAPTERS
    prefix = PORTAL_PREFIX[jur.agendas_adapter]
    processed = state.get("processed", {})

    since = portal_cache.look_back(jur, PORTAL_DAYS, cfg.date_cutoff, today)
    recent = adapter.list_recent(jur.agendas, since)
    agenda: dict[str, str] = {}
    minutes: dict[str, str] = {}
    for m in recent:
        meeting_id = f"{prefix}_{m['id']}"
        if meeting_id in cfg.skip_ids:
            continue
        info = processed.get(meeting_id)
        if info is not None and _summary_source(info) != "agenda":
            continue                                    # final: nothing more to fetch
        text = adapter.fetch_minutes_text(jur.agendas, m["url"]) if has_minutes else None
        if text:
            minutes[m["url"]] = text
        elif info is None:
            text = adapter.fetch_agenda_text(jur.agendas, m["url"])
            if text:
                agenda[m["url"]] = text

    upcoming = adapter.fetch_upcoming(jur.agendas, key, int(jur.agendas.get("days_ahead", 60)))
    horizon = (today + timedelta(days=PREVIEW_DAYS)).isoformat()
    for e in upcoming:
        url = e.get("url") or ""
        if url and url not in agenda and today.isoformat() <= e.get("date", "") <= horizon:
            text = adapter.fetch_agenda_text(jur.agendas, url)
            if text:
                agenda[url] = text

    return {"recent": recent, "upcoming": upcoming, "agenda": agenda, "minutes": minutes}


def write_if_changed(key: str, payload: dict, now: datetime | None = None) -> bool:
    """Write portal_cache/<key>.json when the documents changed or the file
    is due its daily heartbeat. Returns whether it was written."""
    now = now or datetime.now(timezone.utc)
    path = portal_cache.cache_file(key)
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            age = now - datetime.fromisoformat(old.pop("fetched_at"))
            if old == payload and age < timedelta(hours=HEARTBEAT_HOURS):
                return False
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": now.isoformat(timespec="seconds"), **payload},
                               indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return True


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="Fetch portal documents for residential-only jurisdictions.")
    ap.add_argument("--source", help="only this jurisdiction key")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    args = ap.parse_args()

    cfg = load_instance()
    keys = [j.key for j in cfg.jurisdictions if portal_cache.is_residential(j)
            and (not args.source or j.key == args.source)]
    if not keys:
        print("[portals]  No jurisdiction is set to fetch_from residential. Nothing to do.")
        return 0
    state = load_state()
    failed = 0
    for key in keys:
        try:
            payload = collect(cfg, key, state)
        except Exception as e:          # one site down must not stop the others
            print(f"[portals]  {key}: FAILED: {e}")
            failed += 1
            continue
        counts = (f"{len(payload['recent'])} recent, {len(payload['upcoming'])} upcoming, "
                  f"{len(payload['agenda'])} agenda text(s), {len(payload['minutes'])} minutes text(s)")
        if args.dry_run:
            print(f"[portals]  {key}: {counts} (dry run, nothing written)")
        elif write_if_changed(key, payload):
            print(f"[portals]  {key}: {counts} -> {portal_cache.cache_file(key)}")
        else:
            print(f"[portals]  {key}: {counts}; unchanged")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
