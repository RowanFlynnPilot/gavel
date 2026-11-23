"""Coverage check — did each meeting get an agenda before and a summary after?

    python -m engine.coverage                       # the last calendar month
    python -m engine.coverage --month 2026-12
    python -m engine.coverage --from 2026-12-01 --to 2026-12-31 --out coverage.md
    python -m engine.coverage --json
    python -m engine.coverage --write-dir data/coverage      # what CI runs

With --write-dir it writes one file per month, <dir>/<YYYY-MM>.md, for this
month so far and the two before it (--months). The earlier months are written
again each time because their answers keep changing: minutes for a meeting
held six weeks ago turn "waiting" into "summary". A month older than that
keeps the last file written for it.

For each board and each meeting in the period it answers three questions from
files the pipeline already keeps, with no network and no Claude calls:

  - Was it on the tracker? Every meeting the tracker made a card for is in
    the state file. A board whose regular schedule is in instance.json
    (`rules` / `fallback_rules`) is also checked against that schedule, so a
    regular meeting with no card shows up.
  - Was its agenda there before it was held? Yes when the tracker read the
    posted agenda before the meeting (data/agendas_seen.json, kept by
    engine.previews) or made the meeting's card from its agenda on or before
    the day. A board whose agendas the tracker has no way to read (a
    `rule_schedule` board) is marked as having no agenda source.
  - Is there a summary of what happened? Yes when the summary came from a
    recording, minutes or the official vote record. An agenda-only summary is
    "waiting" inside the board's `expected_lag_days` and "overdue" after it.

A regular meeting is matched to a card held the same day whose title has every
word of the rule's name (the test rule_schedule.covered_by_posted uses). A
card under different wording ("Finance and HR Committee" for the rule
"Finance & Human Resources Committee") is not matched: the report lists the
regular meeting under "Needs a look" with that day's cards beside it, for a
person to settle. It never guesses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from .adapters.rule_schedule import months_between, name_tokens, nth_weekday
from .config import InstanceConfig, Jurisdiction, load_instance, utf8_console
from .overdue import _CLOSED, _upgrade_path
from .previews import _AGENDA_TEXT, load_seen
from .publish import _is_synthetic
from .report import load_records

HAPPENED = ("recording", "minutes", "votes")


def _rules(jur: Jurisdiction) -> list[dict]:
    return list(jur.agendas.get("rules") or []) + list(jur.agendas.get("fallback_rules") or [])


def scheduled(jur: Jurisdiction, start: date, end: date) -> list[dict]:
    """The board's regular meetings in [start, end] by its rules:
    [{day, name}], one per rule and month, oldest first."""
    out = {}
    for yr, mo in months_between(start, end):
        for rule in _rules(jur):
            d = nth_weekday(yr, mo, rule["weekday"], rule["nth"])
            if d and start <= d <= end:
                out[(d, rule["name"])] = {"day": d, "name": rule["name"]}
    return [out[k] for k in sorted(out)]


def _covers(name: str, title: str) -> bool:
    want = name_tokens(name)
    return bool(want) and want <= name_tokens(title)


def _agenda_before(jur: Jurisdiction, day: date, name: str, seen: list[dict], since: date | None,
                   only_one_that_day: bool, card: dict | None) -> str:
    """"yes", "no", "no_source" (the tracker can't read this board's agendas)
    or "unknown" (held before the record of agendas began)."""
    if card and _is_synthetic(card["id"]) and card["processed"] and card["processed"] <= day:
        return "yes"                    # the card was made from the agenda, before the meeting
    same_day = [s for s in seen if s["date"] == day.isoformat() and s["seen"][:10] <= s["date"]]
    if any(_covers(s["name"], name) or _covers(name, s["name"]) for s in same_day):
        return "yes"
    if len(same_day) == 1 and only_one_that_day:
        return "yes"
    if jur.agendas_adapter not in _AGENDA_TEXT:
        return "no_source"
    if since is None or day < since:
        return "unknown"
    return "no"


def _after(jur: Jurisdiction, card: dict, today: date) -> str:
    """"summary" (of what happened), "waiting", "overdue", "closed" (a closed
    session is never recorded) or "agenda_final" (nothing to upgrade from)."""
    if card["kind"] in HAPPENED:
        return "summary"
    if _CLOSED.search(card["title"]):
        return "closed"
    if _upgrade_path(jur) is None:
        return "agenda_final"
    return "waiting" if (today - card["day"]).days <= jur.expected_lag_days else "overdue"


def _board(jur: Jurisdiction, cards: list[dict], seen: list[dict], since: date | None,
           start: date, end: date, today: date) -> dict:
    per_day = Counter(c["day"] for c in cards)
    rows, matched = [], set()
    for c in sorted(cards, key=lambda c: (c["day"], c["title"])):
        rows.append({
            "date": c["day"].isoformat(), "name": c["title"], "id": c["id"],
            "regular": False, "on_tracker": True,
            "before": _agenda_before(jur, c["day"], c["title"], seen, since,
                                     per_day[c["day"]] == 1, c),
            "after": _after(jur, c, today),
        })
    regular = scheduled(jur, start, end)
    look = []
    for r in regular:
        hit = next((row for row in rows if row["date"] == r["day"].isoformat()
                    and row["id"] not in matched and _covers(r["name"], row["name"])), None)
        if hit:
            hit["regular"] = True
            matched.add(hit["id"])
            continue
        # No card under that name. Inside the board's normal lag a recording
        # may simply not be up yet; after it, a person should look.
        late = (today - r["day"]).days > jur.expected_lag_days
        that_day = [row["name"] for row in rows if row["date"] == r["day"].isoformat()]
        rows.append({
            "date": r["day"].isoformat(), "name": r["name"], "id": None,
            "regular": True, "on_tracker": False,
            "before": _agenda_before(jur, r["day"], r["name"], seen, since, False, None),
            "after": "no_card" if late else "waiting",
            "same_day": that_day,
        })
        if late:
            look.append({"date": r["day"].isoformat(), "name": r["name"],
                         "why": "regular meeting with no card under that name",
                         "same_day": that_day})
    for row in rows:
        if row["after"] == "overdue":
            look.append({"date": row["date"], "name": row["name"],
                         "why": f"still agenda-only past {jur.expected_lag_days} days"})
        if row["on_tracker"] and row["before"] == "no":
            look.append({"date": row["date"], "name": row["name"],
                         "why": "no agenda was read before the meeting"})
    rows.sort(key=lambda r: (r["date"], r["name"]))
    on = [r for r in rows if r["on_tracker"]]
    reg = [r for r in rows if r["regular"]]
    return {
        "key": jur.key, "name": jur.name, "lag_days": jur.expected_lag_days,
        "has_schedule": bool(_rules(jur)),
        "agenda_source": jur.agendas_adapter in _AGENDA_TEXT,
        "regular": {"scheduled": len(reg),
                    "on_tracker": sum(1 for r in reg if r["on_tracker"]),
                    "waiting": sum(1 for r in reg if not r["on_tracker"] and r["after"] == "waiting"),
                    "no_card": sum(1 for r in reg if r["after"] == "no_card")},
        "meetings": len(on),
        "before": dict(Counter(r["before"] for r in on)),
        "after": dict(Counter(r["after"] for r in on)),
        "rows": rows,
        "look": sorted(look, key=lambda x: (x["date"], x["name"])),
    }


def build(cfg: InstanceConfig, start: date, end: date, *, today: date | None = None,
          state: dict | None = None, seen: dict | None = None,
          include_hidden: bool = False) -> dict:
    """Coverage for [start, end], inclusive; days after today are left out."""
    today = today or date.today()
    end = min(end, today)
    seen_rows = list((seen if seen is not None else load_seen()).values())
    since = min((date.fromisoformat(s["seen"][:10]) for s in seen_rows), default=None)
    records = [r for r in load_records(state) if r["day"] and start <= r["day"] <= end]
    boards = []
    for jur in cfg.jurisdictions:
        if jur.hidden and not include_hidden:
            continue
        boards.append(_board(jur, [r for r in records if r["source"] == jur.key],
                             [s for s in seen_rows if s["source"] == jur.key],
                             since, start, end, today))
    return {
        "instance": cfg.name, "start": start.isoformat(), "end": end.isoformat(),
        "generated": today.isoformat(),
        "agenda_record_since": since.isoformat() if since else None,
        "not_published": [j.name for j in cfg.jurisdictions if j.hidden and not include_hidden],
        "boards": boards,
    }


# ── Rendering ────────────────────────────────────────────────────────────────

def _long(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _short(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def render_markdown(d: dict) -> str:
    out = [
        f"# {d['instance']}: coverage check, {_long(d['start'])} to {_long(d['end'])}",
        "",
        f"Counted from the tracker's own records on {_long(d['generated'])}. \"Agenda before\" "
        f"means the tracker had read the meeting's posted agenda before it was held. \"Summary "
        f"after\" means a summary of what happened, written from a recording, minutes or the "
        f"official vote record.",
        "",
        "## By board",
        "",
        "| Board | Regular meetings scheduled | Of those, on the tracker | All meetings on the "
        "tracker | Agenda before | Summary after | Waiting | Overdue |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for b in d["boards"]:
        r, bf, af = b["regular"], b["before"], b["after"]
        sched = str(r["scheduled"]) if b["has_schedule"] else "no schedule on file"
        on = (f"{r['on_tracker']}" + (f" ({r['waiting']} not due yet)" if r["waiting"] else "")
              if b["has_schedule"] else "n/a")
        if not b["agenda_source"]:
            before = "no agenda source"
        else:
            known = bf.get("yes", 0) + bf.get("no", 0)
            before = f"{bf.get('yes', 0)} of {known}" if known else "not known"
            if bf.get("unknown"):
                before += f" ({bf['unknown']} not known)"
        out.append(f"| {b['name']} | {sched} | {on} | {b['meetings']} | {before} | "
                   f"{af.get('summary', 0)} | {af.get('waiting', 0)} | {af.get('overdue', 0)} |")

    out += ["", "## Needs a look", ""]
    any_look = False
    for b in d["boards"]:
        for x in b["look"]:
            any_look = True
            extra = ""
            if "same_day" in x:
                extra = (f" The tracker has these that day: {'; '.join(x['same_day'])}."
                         if x["same_day"] else " The tracker has nothing from this board that day.")
            out.append(f"- **{b['name']}**, {_short(x['date'])}: {x['name']}: {x['why']}.{extra}")
    if not any_look:
        out.append("Nothing.")

    notes = []
    no_sched = [b["name"] for b in d["boards"] if not b["has_schedule"]]
    if no_sched:
        notes.append(f"- **No regular schedule on file** for {', '.join(no_sched)}. Every meeting "
                     f"the board posted is counted, but the tracker has no calendar to check "
                     f"them against, so a meeting it never saw would not show up here.")
    no_src = [b["name"] for b in d["boards"] if not b["agenda_source"]]
    if no_src:
        notes.append(f"- **No agenda source** for {', '.join(no_src)}: the tracker has no way to "
                     f"read this board's agendas, so nothing is shown before its meetings beyond "
                     f"the date and time.")
    if d["agenda_record_since"]:
        notes.append(f"- The record of agendas read before meetings starts on "
                     f"{_long(d['agenda_record_since'])}. For a meeting held before that, \"agenda "
                     f"before\" is known only where the meeting's card was made from its agenda.")
    else:
        notes.append("- The record of agendas read before meetings has not started yet, so "
                     "\"agenda before\" is known only where a meeting's card was made from its "
                     "agenda.")
    notes.append("- **A regular meeting with no card** may have been cancelled or moved, or its "
                 "card may be worded differently from the schedule. The tracker does not guess; "
                 "check the board's own calendar.")
    notes.append("- **Closed sessions** are never recorded and are not counted as waiting or "
                 "overdue.")
    if d["not_published"]:
        notes.append(f"- **Tracked but not yet published:** {', '.join(d['not_published'])}. "
                     f"Left out; run with --include-hidden to check them.")
    out += ["", "## How to read this", ""] + notes
    return "\n".join(out) + "\n"


def _period(args, today: date) -> tuple[date, date]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("--from and --to go together")
        return date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.month:
        first = date.fromisoformat(f"{args.month}-01")
    else:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def months_back(today: date, count: int) -> list[tuple[date, date]]:
    """(first day, last day) of this month and the count-1 before it, oldest first."""
    out, first = [], today.replace(day=1)
    for _ in range(max(count, 1)):
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((first, nxt - timedelta(days=1)))
        first = (first - timedelta(days=1)).replace(day=1)
    return out[::-1]


def write_months(cfg: InstanceConfig, out_dir: Path, *, months: int = 3, today: date | None = None,
                 include_hidden: bool = False, state: dict | None = None,
                 seen: dict | None = None) -> list[Path]:
    """Write <out_dir>/<YYYY-MM>.md for the last `months` months. Returns the files."""
    today = today or date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for start, end in months_back(today, months):
        data = build(cfg, start, end, today=today, state=state, seen=seen,
                     include_hidden=include_hidden)
        path = out_dir / f"{start.strftime('%Y-%m')}.md"
        path.write_text(render_markdown(data), encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(
        description="Check each meeting for an agenda before and a summary after.")
    ap.add_argument("--write-dir", help="write one file per month here, for this month and the ones before")
    ap.add_argument("--months", type=int, default=3, help="with --write-dir: how many months (default 3)")
    ap.add_argument("--month", help="a calendar month, YYYY-MM (default: last month)")
    ap.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (with --to)")
    ap.add_argument("--to", dest="end", help="last day, YYYY-MM-DD (with --from)")
    ap.add_argument("--out", help="write the check here instead of stdout")
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    ap.add_argument("--include-hidden", action="store_true",
                    help="check boards that are tracked but not yet published")
    args = ap.parse_args()
    if args.write_dir:
        files = write_months(load_instance(), Path(args.write_dir), months=args.months,
                             include_hidden=args.include_hidden)
        print(f"[coverage]  wrote {', '.join(p.name for p in files)} in {args.write_dir}")
        return 0
    try:
        start, end = _period(args, date.today())
    except ValueError as e:
        print(f"[err]  {e}")
        return 1
    if start > end:
        print("[err]  --from is after --to")
        return 1
    data = build(load_instance(), start, end, include_hidden=args.include_hidden)
    text = json.dumps(data, indent=2) + "\n" if args.json else render_markdown(data)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[coverage]  wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
