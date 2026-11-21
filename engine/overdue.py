"""Find displayed meetings still agenda-only past their jurisdiction's normal
recording lag.

    python -m engine.overdue                          # summary to stderr
    python -m engine.overdue --print-count            # just the count on stdout
    python -m engine.overdue --markdown overdue.md    # issue body (when overdue)
    python -m engine.overdue --today 2026-09-25 --source KEY

Agenda-only is the normal first state for most meetings: the recording,
audio, minutes or vote records that upgrade a summary arrive hours to weeks
later. What matters is an upgrade that is *late*. Each jurisdiction's normal
lag is `expected_lag_days` in instance.json (default 7). Several overdue
meetings from one jurisdiction usually mean a matcher broke upstream; a
standing list of every agenda-only meeting signals nothing.

Not flagged: closed sessions (never recorded), and jurisdictions with nothing
to upgrade from (no video channel, no vote records, no minutes). A
portal-first meeting already summarized from its agenda and recorded votes is
published with isAgendaOnly false, so it never appears here.

The workflow turns a nonzero count into a single self-closing issue.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

from .config import (DEFAULT_EXPECTED_LAG_DAYS, MEETINGS_JSON, MINUTES_ADAPTERS, VOTE_ADAPTERS,
                     InstanceConfig, Jurisdiction, load_instance, utf8_console)

_CLOSED = re.compile(r"\bclosed\b", re.IGNORECASE)


def _upgrade_path(jur: Jurisdiction) -> str | None:
    """Where a late upgrade for this jurisdiction would come from, or None
    when its agenda-only summaries are final."""
    if jur.agendas_adapter == "municode":
        return ("posted minutes (`minutes_upgrade_window_days`)"
                + (", or the audio match in `engine.fetch_transcripts`" if jur.audio_url else ""))
    if jur.video is not None:
        if jur.agendas_adapter == "boardbook":
            return "channel recording match (`boardbook.match_recording`, `engine.fetch_transcripts`)"
        return "residential caption sweep (`python -m engine.fetch_transcripts --all`)"
    if jur.agendas_adapter in VOTE_ADAPTERS:
        return "the portal's vote records (`votes_upgrade_window_days`)"
    if jur.agendas_adapter in MINUTES_ADAPTERS:
        return "posted minutes (`minutes_upgrade_window_days`)"
    return None


def find_overdue(cfg: InstanceConfig, meetings: list[dict], today: date,
                 source: str | None = None) -> list[dict]:
    """Agenda-only meetings held more than expected_lag_days ago, longest
    wait first. Each carries _waited, _lag and _where."""
    overdue = []
    for m in meetings:
        if not isinstance(m, dict) or not m.get("isAgendaOnly"):
            continue
        if source and m.get("source") != source:
            continue
        if _CLOSED.search(m.get("title", "")):
            continue
        jur = cfg.by_key.get(m.get("source", ""))
        where = _upgrade_path(jur) if jur else "unknown jurisdiction (not in instance.json)"
        if where is None:
            continue
        try:
            held = datetime.strptime(m.get("date", ""), "%B %d, %Y").date()
        except ValueError:
            continue
        waited = (today - held).days
        lag = jur.expected_lag_days if jur else DEFAULT_EXPECTED_LAG_DAYS
        if waited > lag:
            overdue.append({**m, "_waited": waited, "_lag": lag, "_where": where})
    return sorted(overdue, key=lambda m: -m["_waited"])


def render_markdown(overdue: list[dict], today: date) -> str:
    lines = [
        f"These meetings are still summarized from the **agenda only**, past the point "
        f"where a recording, audio, minutes or vote records normally arrive (checked "
        f"{today.isoformat()}). Readers see what was scheduled, not what happened.",
        "",
        "| Meeting | Jurisdiction | Held | Waiting | Normally within | Where to look |",
        "|---|---|---|---|---|---|",
    ]
    for m in overdue:
        lines.append(
            f"| {m.get('title', m.get('id'))} | {m.get('source', '')} | {m.get('date')} | "
            f"{m['_waited']} days | {m['_lag']} days | {m['_where']} |")
    lines += [
        "",
        "Several overdue meetings from one jurisdiction usually means a matcher broke (an "
        "upstream title or format change). A single one usually means the recording "
        "doesn't exist; it drops off this list when the meeting scrolls off the tracker. "
        "This issue closes itself when nothing is overdue.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    utf8_console()
    parser = argparse.ArgumentParser(
        description="List displayed meetings still agenda-only past their "
                    "jurisdiction's normal recording lag.")
    parser.add_argument("--data", default=str(MEETINGS_JSON))
    parser.add_argument("--today", help="evaluate as of YYYY-MM-DD (default: today)")
    parser.add_argument("--source", help="only check one jurisdiction key")
    parser.add_argument("--markdown", help="write an issue body here when anything is overdue")
    parser.add_argument("--print-count", action="store_true",
                        help="print only the overdue count on stdout")
    args = parser.parse_args()

    cfg = load_instance()
    if args.source:
        cfg.jurisdiction(args.source)
    today = date.fromisoformat(args.today) if args.today else date.today()
    meetings = json.loads(Path(args.data).read_text(encoding="utf-8"))
    overdue = find_overdue(cfg, meetings, today, args.source)

    for m in overdue:
        print(f"  overdue: {m['_waited']:3d}d (lag {m['_lag']}d)  {m.get('source', ''):12s} "
              f"{m.get('title', '')[:60]}", file=sys.stderr)
    print(f"{len(overdue)} overdue agenda-only meeting(s)", file=sys.stderr)

    if args.markdown and overdue:
        Path(args.markdown).write_text(render_markdown(overdue, today), encoding="utf-8")
    if args.print_count:
        print(len(overdue))
    return 0


if __name__ == "__main__":
    sys.exit(main())
