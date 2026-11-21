"""Which jurisdictions' sites have stopped answering.

A board whose portal refuses the pipeline (an outage, a new bot wall, a moved
page) used to leave the run green: the error was logged, the board simply got
no new meetings, and the first visible sign was an "overdue" list weeks later.
The pipeline now notes, per jurisdiction and per job ("meetings": finding new
meetings; "upcoming": the calendar), whether its source answered. One that has
gone unanswered for UNREACHABLE_HOURS opens a self-closing issue.

    python -m engine.health --print-count --markdown unreachable_body.md

State is source_health.json at the repo root, committed by CI:
    {"<key>:<job>": {"failing_since", "last_error", "checks"}}
An entry exists only while that job is failing; one success removes it.
Failures are measured by time, not by count, because several steps of one
workflow run can each check the same source.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_instance, utf8_console

HEALTH_FILE = Path(os.environ.get("HEALTH_FILE", "./source_health.json"))
UNREACHABLE_HOURS = int(os.environ.get("UNREACHABLE_HOURS", "12"))
JOBS = {"meetings": "finding new meetings", "upcoming": "the upcoming calendar"}


def _load() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save(data: dict) -> None:
    HEALTH_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def note(key: str, job: str, error: str | None, now: datetime | None = None) -> None:
    """Record that a jurisdiction's source answered (error=None) or didn't."""
    data = _load()
    slot = f"{key}:{job}"
    if error is None:
        if slot not in data:
            return                      # healthy and already clean: leave the file alone
        del data[slot]
    else:
        stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        entry = data.get(slot) or {"failing_since": stamp, "checks": 0}
        entry["last_error"] = str(error)[:300]
        entry["checks"] = entry.get("checks", 0) + 1
        data[slot] = entry
    _save(data)


def unreachable(known_keys: set[str] | None = None, now: datetime | None = None,
                hours: int | None = None) -> list[dict]:
    """Jobs failing for at least `hours`, longest first:
    {key, job, hours, last_error, checks}. Entries for jurisdictions no longer
    in the instance are dropped from the file."""
    now = now or datetime.now(timezone.utc)
    limit = timedelta(hours=UNREACHABLE_HOURS if hours is None else hours)
    data = _load()
    if known_keys is not None:
        stale = [s for s in data if s.split(":")[0] not in known_keys]
        if stale:
            for s in stale:
                del data[s]
            _save(data)
    out = []
    for slot, entry in data.items():
        key, _, job = slot.partition(":")
        try:
            since = datetime.fromisoformat(entry["failing_since"])
        except (KeyError, ValueError):
            continue
        if now - since >= limit:
            out.append({"key": key, "job": job, "hours": int((now - since).total_seconds() // 3600),
                        "last_error": entry.get("last_error", ""), "checks": entry.get("checks", 0)})
    return sorted(out, key=lambda r: -r["hours"])


def render_markdown(rows: list[dict], names: dict[str, str]) -> str:
    lines = [
        "The tracker has not been able to read these boards' sites. Until it can, the board "
        "gets no new meetings or its calendar stops updating; nothing already published is lost.",
        "",
        "| Board | What is failing | For | Last error |",
        "|---|---|---|---|",
    ]
    for r in rows:
        err = r["last_error"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {names.get(r['key'], r['key'])} | {JOBS.get(r['job'], r['job'])} | "
                     f"{r['hours']} hours ({r['checks']} checks) | {err} |")
    lines += [
        "",
        "What it usually means:",
        "- **HTTP 403 or a challenge page**: the site has started refusing automated readers or "
        "cloud servers. If it still answers from a home connection, set `\"fetch_from\": "
        "\"residential\"` in that board's `agendas` block (see `engine/portal_cache.py`).",
        "- **\"no documents from the residential fetcher\"**: the board is fetched from the home "
        "machine and that machine has not delivered; check its `logs/refresh-*.log`.",
        "- **Timeouts or 5xx**: the site is down; this clears on its own when it returns.",
        "- **\"structure changed\"**: the site was redesigned and its adapter needs updating.",
        "",
        "This issue closes itself when every board's site answers again.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="List jurisdictions whose sites have stopped answering.")
    ap.add_argument("--print-count", action="store_true", help="print only the count on stdout")
    ap.add_argument("--markdown", help="write an issue body here when anything is unreachable")
    ap.add_argument("--hours", type=int, help=f"threshold (default {UNREACHABLE_HOURS})")
    args = ap.parse_args()

    cfg = load_instance()
    rows = unreachable({j.key for j in cfg.jurisdictions}, hours=args.hours)
    if args.markdown and rows:
        Path(args.markdown).write_text(
            render_markdown(rows, {j.key: j.name for j in cfg.jurisdictions}), encoding="utf-8")
    if args.print_count:
        print(len(rows))
    else:
        for r in rows:
            print(f"  unreachable: {r['hours']:4d}h  {r['key']:16s} {r['job']:9s} {r['last_error'][:100]}")
        print(f"[health]  {len(rows)} source(s) unreachable for {UNREACHABLE_HOURS}h or more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
