"""Funder / board report — the tracker's own numbers for a reporting period.

    python -m engine.report --from 2027-01-01 --to 2027-06-30 --out report.md
    python -m engine.report                 # the last 180 days, to stdout
    python -m engine.report --json          # the numbers alone

Everything here is counted from files the pipeline already keeps (the state
file, the summary sidecars, corrections.json, the cost ledger), so the report
costs nothing to produce and can be re-run for any period. It makes no Claude
calls.

What it reports, per board and in total:
  - meetings summarized, and how many of those from a recording or minutes
    (what happened) versus the agenda alone (what was scheduled)
  - residents who spoke at public comment per meeting, next to the same
    figure for the equal-length period before, with the number of meetings
    the count is known for
  - corrections and privacy fixes logged
  - agenda previews written
  - Claude spend

What it cannot know is listed at the end of the report rather than left out:
reader analytics live in the analytics service, and alert subscribers in the
mailing service.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import costs
from .config import MEETINGS_JSON, InstanceConfig, load_instance, utf8_console
from .overdue import find_overdue
from .publish import _is_synthetic, parse_date_from_title
from .scrub import load_corrections
from .speakers import speaker_count
from .state import load_state

RECORD_SOURCES = ("transcript", "minutes")


def _ymd(s) -> date | None:
    s = str(s or "")
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def meeting_day(info: dict) -> date | None:
    """The day a meeting was held, by the same precedence publish uses:
    meeting_date, a date in the title, upload_date, then processed_at."""
    d = _ymd(info.get("meeting_date"))
    if d:
        return d
    dt = parse_date_from_title(info.get("title", ""))
    if dt:
        return dt.date()
    d = _ymd(info.get("upload_date"))
    if d:
        return d
    try:
        return datetime.fromisoformat(
            (info.get("processed_at") or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _load_summary(info: dict) -> dict | None:
    sf = info.get("summary_file")
    if not sf:
        return None
    p = Path(f"{Path(sf).with_suffix('')}_summary.json")
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def summary_kind(meeting_id: str, summary: dict | None) -> str:
    """"recording", "minutes", "votes" (agenda plus the portal's vote
    records), "agenda", or "missing" when the sidecar is gone."""
    if summary is None:
        return "missing"
    src = summary.get("_source")
    if src == "transcript":
        return "recording"
    if src == "minutes":
        return "minutes"
    if src == "agenda_with_votes":
        return "votes"
    if src is None and not _is_synthetic(meeting_id):
        return "recording"          # summaries from before the tag existed
    return "agenda"


def load_records(state: dict | None = None) -> list[dict]:
    """One row per meeting in the state file:
    {id, source, title, day, kind, speakers}. speakers is None when the
    summary doesn't state a count (and always for agenda-only summaries)."""
    state = state if state is not None else load_state()
    rows = []
    for mid, info in state.get("processed", {}).items():
        summary = _load_summary(info)
        kind = summary_kind(mid, summary)
        rows.append({
            "id": mid,
            "source": info.get("source", ""),
            "title": info.get("title", ""),
            "day": meeting_day(info),
            "kind": kind,
            "speakers": speaker_count({**summary, "_source": "transcript"})
            if summary and kind in ("recording", "minutes") else None,
        })
    return rows


def _speaker_stats(rows: list[dict]) -> dict:
    counts = [r["speakers"] for r in rows if r["speakers"] is not None]
    on_record = [r for r in rows if r["kind"] in ("recording", "minutes")]
    return {
        "meetings_with_record": len(on_record),
        "meetings_counted": len(counts),
        "speakers": sum(counts),
        "per_meeting": round(sum(counts) / len(counts), 2) if counts else None,
        "meetings_with_a_speaker": sum(1 for c in counts if c > 0),
    }


def _in(rows: list[dict], start: date, end: date) -> list[dict]:
    return [r for r in rows if r["day"] and start <= r["day"] <= end]


def build(cfg: InstanceConfig, start: date, end: date, *, today: date | None = None,
          state: dict | None = None, ledger: list[dict] | None = None,
          corrections: list[dict] | None = None,
          displayed: list[dict] | None = None) -> dict:
    """The report's numbers for [start, end], inclusive."""
    today = today or date.today()
    rows = load_records(state)
    span = (end - start).days + 1
    prior_start, prior_end = start - timedelta(days=span), start - timedelta(days=1)
    now_rows, prior_rows = _in(rows, start, end), _in(rows, prior_start, prior_end)

    first_seen: dict[str, date] = {}
    for r in rows:
        if r["day"] and (r["source"] not in first_seen or r["day"] < first_seen[r["source"]]):
            first_seen[r["source"]] = r["day"]

    boards = []
    for jur in cfg.jurisdictions:
        mine = [r for r in now_rows if r["source"] == jur.key]
        kinds = Counter(r["kind"] for r in mine)
        boards.append({
            "key": jur.key,
            "name": jur.name,
            "first_meeting": first_seen[jur.key].isoformat() if jur.key in first_seen else None,
            "added_this_period": jur.key in first_seen and start <= first_seen[jur.key] <= end,
            "meetings": len(mine),
            "from_record": kinds["recording"] + kinds["minutes"],
            "with_votes_only": kinds["votes"],
            "agenda_only": kinds["agenda"] + kinds["missing"],
            "speakers": _speaker_stats(mine),
            "speakers_prior": _speaker_stats([r for r in prior_rows if r["source"] == jur.key]),
        })

    corr = corrections if corrections is not None else load_corrections()
    corr_now = [c for c in corr if start.isoformat() <= str(c.get("date", "")) <= end.isoformat()]

    led = ledger if ledger is not None else costs.load_ledger()
    led_now = [r for r in led if start.isoformat() <= str(r.get("ts", ""))[:10] <= end.isoformat()]
    by_purpose: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for r in led_now:
        p = by_purpose[r.get("purpose") or r.get("script") or "(untagged)"]
        p[0] += 1
        p[1] += r.get("usd", 0)
    previews = [r for r in led_now if r.get("purpose") == "agenda_preview"]

    if displayed is None:
        try:
            displayed = json.loads(MEETINGS_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            displayed = []

    return {
        "instance": cfg.name,
        "newsroom": cfg.newsroom,
        "start": start.isoformat(), "end": end.isoformat(),
        "prior_start": prior_start.isoformat(), "prior_end": prior_end.isoformat(),
        "generated": today.isoformat(),
        "boards": boards,
        "totals": {
            "boards": len(cfg.jurisdictions),
            "boards_added": sum(1 for b in boards if b["added_this_period"]),
            "meetings": len(now_rows),
            "from_record": sum(b["from_record"] for b in boards),
            "with_votes_only": sum(b["with_votes_only"] for b in boards),
            "agenda_only": sum(b["agenda_only"] for b in boards),
            "speakers": _speaker_stats(now_rows),
            "speakers_prior": _speaker_stats(prior_rows),
            "undated": sum(1 for r in rows if r["day"] is None),
        },
        "corrections": {
            "total": len(corr_now),
            "by_kind": dict(Counter(c.get("kind", "correction") for c in corr_now)),
            "meetings": len({c["id"] for c in corr_now}),
            "all_time": len(corr),
        },
        "previews": {
            "written": len(previews),
            "meetings": len({r.get("meeting_id") for r in previews}),
            "by_board": dict(Counter(r.get("jurisdiction", "") for r in previews)),
            "usd": round(sum(r.get("usd", 0) for r in previews), 2),
        },
        "spend": {
            "usd": round(sum(r.get("usd", 0) for r in led_now), 2),
            "calls": len(led_now),
            "by_purpose": {k: [n, round(u, 2)] for k, (n, u) in
                           sorted(by_purpose.items(), key=lambda kv: -kv[1][1])},
            "ledger_from": min((str(r.get("ts", ""))[:10] for r in led), default=None),
        },
        "overdue_now": len(find_overdue(cfg, displayed, today)),
    }


# ── Rendering ────────────────────────────────────────────────────────────────

def _long(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _rate(s: dict) -> str:
    if s["per_meeting"] is None:
        return "not known"
    return f"{s['per_meeting']:.2f}"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def render_markdown(d: dict) -> str:
    t = d["totals"]
    sp, sp0 = t["speakers"], t["speakers_prior"]
    out = [
        f"# {d['instance']}: report for {_long(d['start'])} to {_long(d['end'])}",
        "",
        f"Counted from the tracker's own records on {_long(d['generated'])}. "
        f"A person should read it against the tracker before it goes to a funder.",
        "",
        "## At a glance",
        "",
        f"- **{t['boards']} boards** covered"
        + (f", {t['boards_added']} of them added in this period" if t["boards_added"] else "") + ".",
        f"- **{t['meetings']} meetings** summarized: {t['from_record']} from a recording or "
        f"minutes (what happened), {t['with_votes_only']} from the agenda plus the official vote "
        f"record, {t['agenda_only']} from the agenda alone (what was scheduled).",
    ]
    if sp["meetings_counted"]:
        line = (f"- **{sp['speakers']} residents spoke** at public comment across "
                f"{sp['meetings_counted']} meetings where the count is known "
                f"({_rate(sp)} per meeting; at least one resident spoke at "
                f"{_pct(sp['meetings_with_a_speaker'], sp['meetings_counted'])} of them).")
        if sp0["meetings_counted"]:
            line += (f" The {_long(d['prior_start'])} to {_long(d['prior_end'])} period before: "
                     f"{_rate(sp0)} per meeting across {sp0['meetings_counted']} meetings.")
        out.append(line)
    else:
        out.append("- No meeting in this period has a public-comment count yet.")
    c = d["corrections"]
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(c["by_kind"].items()))
    out.append(f"- **{c['total']} corrections** logged"
               + (f" ({kinds}), on {_n(c['meetings'], 'meeting')}" if c["total"] else "")
               + f". {c['all_time']} since the log began.")
    p = d["previews"]
    out.append(f"- **{p['written']} agenda previews** written before meetings, for "
               f"{_n(p['meetings'], 'meeting')}." if p["written"] else
               "- No agenda previews were written in this period.")
    s = d["spend"]
    ledger_covers = not s["ledger_from"] or s["ledger_from"] <= d["start"]
    per = (f"; ${s['usd'] / t['meetings']:.2f} per meeting summarized"
           if t["meetings"] and s["usd"] and ledger_covers else "")
    out.append(f"- **${s['usd']:.2f}** in Claude API spend over {s['calls']} calls{per}.")

    out += ["", "## Meetings by board", "",
            "| Board | In the tracker since | Meetings | From a recording or minutes | "
            "Agenda plus votes | Agenda only |",
            "|---|---|---|---|---|---|"]
    for b in d["boards"]:
        since = _long(b["first_meeting"]) if b["first_meeting"] else "no meetings yet"
        if b["added_this_period"]:
            since += " (added this period)"
        out.append(f"| {b['name']} | {since} | {b['meetings']} | {b['from_record']} | "
                   f"{b['with_votes_only']} | {b['agenda_only']} |")

    out += ["", "## Residents speaking at public comment", "",
            "Speakers per meeting, for meetings summarized from a recording or minutes. "
            "\"Counted\" is the number of those meetings where the summary states how many "
            "residents spoke; the rest are left out of the average, not counted as zero.",
            "",
            f"| Board | Meetings on the record | Counted | Residents who spoke | Per meeting | "
            f"Per meeting, prior period |",
            "|---|---|---|---|---|---|"]
    for b in d["boards"]:
        a, z = b["speakers"], b["speakers_prior"]
        prior = f"{_rate(z)} ({_n(z['meetings_counted'], 'meeting')})" if z["meetings_counted"] else "no data"
        out.append(f"| {b['name']} | {a['meetings_with_record']} | {a['meetings_counted']} | "
                   f"{a['speakers']} | {_rate(a)} | {prior} |")
    out += ["",
            f"The prior period is {_long(d['prior_start'])} to {_long(d['prior_end'])}. A board "
            f"with few meetings swings widely from one period to the next; read the per-meeting "
            f"figure with the meeting count beside it."]

    out += ["", "## Corrections", ""]
    if c["total"]:
        out.append(f"{c['total']} entries in the corrections log are dated in this period "
                   f"({kinds}), touching {c['meetings']} meeting summaries. \"Privacy\" entries "
                   f"remove a private resident's name or details under the naming policy; "
                   f"\"correction\" entries fix a fact.")
    else:
        out.append("No corrections were logged in this period.")

    out += ["", "## Agenda previews", ""]
    if p["written"]:
        by = ", ".join(f"{k}: {n}" for k, n in sorted(p["by_board"].items(), key=lambda kv: -kv[1]))
        out.append(f"{p['written']} previews were written for {p['meetings']} upcoming meetings "
                   f"(${p['usd']:.2f}). By board: {by}. A revised agenda gets a fresh preview, so "
                   f"previews outnumber meetings.")
    else:
        out.append("None written in this period.")

    out += ["", "## Cost", "",
            "| Work | Calls | Spend |", "|---|---|---|"]
    for purpose, (n, usd) in s["by_purpose"].items():
        out.append(f"| {purpose} | {n} | ${usd:.2f} |")
    out.append(f"| **Total** | **{s['calls']}** | **${s['usd']:.2f}** |")
    if not ledger_covers:
        out += ["", f"The cost ledger starts on {_long(s['ledger_from'])}, after this period "
                    f"began; spend before that date is not in the total."]

    out += ["", "## Not in this report", "",
            "- **Reader numbers** (visits, agenda-preview opens before a meeting, correction-link "
            "clicks) are in the analytics service, under the goals \"Agenda Preview\", "
            "\"Correction Link\" and the tracker's page views. Agenda-preview opens carry the "
            "board and the number of days before the meeting.",
            "- **Alert subscribers** are in the mailing service.",
            f"- **Right now**, {d['overdue_now']} displayed meeting(s) are still summarized from "
            f"the agenda past the point their recording or minutes normally arrive."]
    if t["undated"]:
        out.append(f"- {t['undated']} meeting(s) in the state file have no usable date and are "
                   f"in no period.")
    return "\n".join(out) + "\n"


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="Report the tracker's numbers for a period.")
    ap.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (default: 180 days back)")
    ap.add_argument("--to", dest="end", help="last day, YYYY-MM-DD (default: today)")
    ap.add_argument("--out", help="write the report here instead of stdout")
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=179)
    if start > end:
        print("[err]  --from is after --to")
        return 1
    data = build(load_instance(), start, end)
    text = json.dumps(data, indent=2) + "\n" if args.json else render_markdown(data)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[report]  wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
