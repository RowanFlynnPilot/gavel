"""Portal documents fetched from a residential connection.

Some municipal sites refuse cloud servers outright (the Village of Maine's
answers HTTP 403 to GitHub Actions and 200 to a home connection, Nov. 2026),
the same wall YouTube puts up for captions. A jurisdiction whose `agendas`
block says `"fetch_from": "residential"` is therefore read in two halves:

  the operator's machine   python -m engine.fetch_portals
      runs the real adapter and writes what the pipeline will need to
      portal_cache/<key>.json, which the refresh script commits and pushes;
  CI                        reads that file through CachedPortal, which
      answers the same four calls the adapter would, with no network.

The cache holds only what is pending: the listings, the agenda or minutes
text of meetings not yet summarized from minutes, and agenda text for
upcoming meetings due a preview. A text is dropped once CI has used it.

portal_cache/<key>.json:
    {"fetched_at": iso, "recent": [list_recent rows], "upcoming": [records],
     "agenda": {doc_url: text}, "minutes": {doc_url: text}}
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import MINUTES_ADAPTERS
from .errors import UpstreamUnavailableError

CACHE_DIR = Path(os.environ.get("PORTAL_CACHE_DIR", "./portal_cache"))
# The fetcher rewrites an unchanged cache once a day (a heartbeat); a cache
# older than this means the operator's machine has stopped delivering.
MAX_AGE_HOURS = int(os.environ.get("PORTAL_CACHE_MAX_AGE_HOURS", "72"))
MINUTES_WINDOW_DAYS = 75   # default wait for minutes; engine.pipeline uses the same number


def is_residential(jur) -> bool:
    return jur.agendas.get("fetch_from") == "residential"


def cache_file(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def look_back(jur, days: int, date_cutoff: str = "", today: date | None = None) -> date:
    """The first meeting date a portal-first board is listed from. Shared by
    the pipeline and the residential fetcher so both ask for the same range.

    Normally `days` back. A board that posts minutes and has a start_date is
    looked back the whole minutes window instead (a meeting whose agenda can't
    be read is created when its minutes appear), floored at the start_date so
    its history is never backfilled."""
    today = today or date.today()
    since = today - timedelta(days=days)
    if jur.start_date and jur.agendas_adapter in MINUTES_ADAPTERS:
        window = int(jur.agendas.get("minutes_upgrade_window_days", MINUTES_WINDOW_DAYS))
        since = min(since, today - timedelta(days=window))
    if jur.start_date:
        since = max(since, jur.start_date)
    if date_cutoff:
        since = max(since, date(int(date_cutoff[:4]), int(date_cutoff[4:6]), int(date_cutoff[6:8])))
    return since


class CachedPortal:
    """Stands in for a portal adapter in CI, answering from the cache file."""

    def __init__(self, key: str, now: datetime | None = None):
        self.key = key
        self.now = now or datetime.now(timezone.utc)
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            path = cache_file(self.key)
            if not path.exists():
                raise UpstreamUnavailableError(
                    f"{self.key}: no documents from the residential fetcher yet ({path} is missing)")
            data = json.loads(path.read_text(encoding="utf-8"))
            try:
                age = self.now - datetime.fromisoformat(data["fetched_at"])
            except (KeyError, ValueError):
                raise UpstreamUnavailableError(f"{self.key}: {path} has no usable fetched_at") from None
            if age > timedelta(hours=MAX_AGE_HOURS):
                raise UpstreamUnavailableError(
                    f"{self.key}: no documents from the residential fetcher for "
                    f"{int(age.total_seconds() // 3600)} hours ({path})")
            self._data = data
        return self._data

    def list_recent(self, agendas_cfg: dict, since: date) -> list[dict]:
        return [m for m in self._load().get("recent", []) if m.get("date", "") >= since.isoformat()]

    def fetch_upcoming(self, agendas_cfg: dict, jurisdiction_key: str, days_ahead: int) -> list[dict]:
        today = date.today()
        end = today + timedelta(days=days_ahead)
        rows = [e for e in self._load().get("upcoming", [])
                if today.isoformat() <= e.get("date", "") <= end.isoformat()]
        print(f"  [ok]  {jurisdiction_key}: {len(rows)} upcoming meetings from the residential cache")
        return rows

    # A text that isn't in the cache is "not posted yet" as far as CI can
    # tell; a missing or stale cache file was already raised by the listing.
    def fetch_agenda_text(self, agendas_cfg: dict, doc_url: str) -> str | None:
        try:
            return self._load().get("agenda", {}).get(doc_url)
        except UpstreamUnavailableError:
            return None

    def fetch_minutes_text(self, agendas_cfg: dict, doc_url: str) -> str | None:
        try:
            return self._load().get("minutes", {}).get(doc_url)
        except UpstreamUnavailableError:
            return None
