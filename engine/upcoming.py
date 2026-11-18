"""Upcoming step — build data/upcoming.json, keyed by jurisdiction key.

Each jurisdiction's upcoming list is the union of:
  - its agendas adapter's posted future meetings (civicclerk / legistar / primegov API, boardbook /
    agendacenter / municode scrape), which handle their own fallback_rules
  - the rule_schedule projection, for rule_schedule jurisdictions
  - an optional YouTube description scan (video.scan_descriptions_for_upcoming)

Adapters fetch through engine.net (3 attempts). When an upstream still doesn't
answer, that jurisdiction keeps its previously published future meetings
(last-known-good) instead of going blank.

UPCOMING RECORD: {date ("2026-07-08"), time ("5:00 PM"|""), name, url, source}
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .adapters import get_adapter
from .config import InstanceConfig, UPCOMING_JSON
from .errors import AdapterStructureError, UpstreamUnavailableError

logger = logging.getLogger(__name__)


def fetch_for_jurisdiction(cfg: InstanceConfig, key: str) -> list[dict] | None:
    """A jurisdiction's upcoming meetings, or None when its upstream didn't
    answer (an empty list means it answered with no meetings)."""
    jur = cfg.jurisdiction(key)
    a = jur.agendas
    days_ahead = a.get("days_ahead", 60)
    adapter_name = jur.agendas_adapter
    print(f"\n[upcoming]  {jur.name} ({adapter_name})...")

    results: dict = {}

    if adapter_name == "rule_schedule":
        from .adapters import rule_schedule
        # Optional: mine recent video descriptions for explicit dates first —
        # they carry real posted times the rules can't know.
        if jur.video and jur.video.get("scan_descriptions_for_upcoming"):
            from .adapters import youtube
            for e in youtube.scan_descriptions_for_upcoming(jur.video, key, days_ahead):
                results[(e["date"], e["name"])] = e
        rule_added = 0
        for e in rule_schedule.project(a["rules"], key, days_ahead):
            k = (e["date"], e["name"])
            if k not in results:
                results[k] = e
                rule_added += 1
        print(f"  [ok]  {key} rule-based: {rule_added} meetings projected")
        entries = sorted(results.values(), key=lambda x: (x["date"], x["time"]))
    else:
        adapter = get_adapter(adapter_name)
        try:
            entries = adapter.fetch_upcoming(a, key, days_ahead)
        except UpstreamUnavailableError as e:
            logger.warning("%s upcoming: %s", key, e)
            return None

    return entries


def _load_previous(data_path: Path) -> dict:
    if not data_path.exists():
        return {}
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Existing %s is not valid JSON — no last-known-good fallback", data_path)
        return {}


def _last_known_good(key: str, previous: dict) -> list[dict]:
    today = date.today().isoformat()
    kept = [e for e in previous.get(key, []) if e.get("date", "") >= today]
    print(f"  [warn]  {key}: no fresh upcoming list — keeping {len(kept)} "
          f"last-known-good upcoming meeting(s)")
    return kept


def update_upcoming(cfg: InstanceConfig, data_path: Path | None = None,
                    sources: list[str] | None = None,
                    previews: str = "cached") -> dict[str, list]:
    """Refresh data/upcoming.json for all (or the given) jurisdictions.
    A structural scrape failure in one jurisdiction is re-raised after the
    others complete — fail loud without losing the healthy sources. A
    jurisdiction with no fresh list (upstream didn't answer, or its structure
    drifted) keeps its last published future meetings.

    previews: "generate" writes agenda previews for meetings whose agenda is
    newly posted or changed (Claude calls, capped per run); "cached" attaches
    only previews already written; "off" attaches none."""
    data_path = data_path or UPCOMING_JSON
    keys = sources or [j.key for j in cfg.jurisdictions]
    previous = _load_previous(data_path)

    payload: dict[str, list] = {}
    structure_errors: list[AdapterStructureError] = []
    for key in keys:
        try:
            fresh = fetch_for_jurisdiction(cfg, key)
        except AdapterStructureError as e:
            logger.error("%s upcoming failed on structure drift: %s", key, e)
            structure_errors.append(e)
            fresh = None
        payload[key] = fresh if fresh is not None else _last_known_good(key, previous)

    if previews != "off":
        from . import previews as previews_mod
        try:
            previews_mod.attach(cfg, payload, generate=(previews == "generate"),
                                cache_file=data_path.parent / "previews.json")
        except Exception as e:      # previews are an extra; the schedule is not
            logger.error("agenda previews skipped this run: %s", e)

    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"\n[ok]  Wrote {data_path}")
    for key in keys:
        print(f"    {key}: {len(payload[key])}")

    if structure_errors:
        raise structure_errors[0]
    return payload
