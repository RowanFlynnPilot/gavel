"""Reporter tip sheet — what in the tracker is worth a reporter's time this week.

    python -m engine.tipsheet            # writes data/tipsheet.md and data/tipsheet.html

The tracker's readers get every meeting; a newsroom's reporters need the
short list. This sorts what the pipeline already holds into leads, with no
Claude call:

  - Decisions coming up: votes and public hearings on agendas for the next
    week (from the agenda previews)
  - Contested votes: motions that failed, were tabled, or split
  - Crowded public comment: meetings where several residents spoke
  - Recurring specifics: named projects and places that came up at more than
    one meeting in the last month
  - Still waiting: meetings summarized from the agenda past the point their
    recording or minutes normally arrive
  - Coverage gaps: next week's meetings with no agenda read yet

It reports counts of residents who spoke, never who they were or what they
said: the tip sheet points a reporter at the meeting, and the summary's own
privacy rules stay with the summary.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR, MEETINGS_JSON, UPCOMING_JSON, InstanceConfig, load_instance, utf8_console
from .overdue import find_overdue
from .speakers import speaker_count

AHEAD_DAYS = 7
BACK_DAYS = 7
TOPIC_DAYS = 30
CROWD = 3
MAX_TOPICS = 8

# Catch-all tags every board earns most weeks; a lead is the named project.
GENERIC_TOPICS = {
    "budget", "roads", "public safety", "zoning", "staffing", "parks", "utilities",
    "development", "curriculum", "facilities", "policy", "athletics", "public works",
    "economic development", "finance", "personnel", "infrastructure", "planning",
    "education", "housing", "transportation", "governance", "administration",
    "debt service", "fiscal planning", "claims", "contracts", "capital projects",
    "taxes", "property taxes", "tax levy", "borrowing", "licenses", "permits",
}

_SPLIT = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b")
_NOT_UNANIMOUS = re.compile(r"not unanimous|died|tie\b|opposed|dissent|\bnay", re.IGNORECASE)
_CLOSED = re.compile(r"\bclosed\b", re.IGNORECASE)


def _held(m: dict) -> date | None:
    try:
        return datetime.strptime(m.get("date", ""), "%B %d, %Y").date()
    except ValueError:
        return None


def contested(vote: dict) -> str | None:
    """Why a vote is worth a look ("failed", "tabled", "split 4-3"), or None
    for a routine approval."""
    outcome = str(vote.get("outcome") or "").strip().lower()
    tally = str(vote.get("tally") or "")
    if outcome.startswith("fail"):
        return "failed" + (f" {tally}" if _SPLIT.search(tally) else "")
    if outcome.startswith("table") or outcome.startswith("postpone"):
        return "tabled"
    m = _SPLIT.search(tally)
    if m and min(int(m.group(1)), int(m.group(2))) > 0:
        return f"split {m.group(1)}-{m.group(2)}"
    if _NOT_UNANIMOUS.search(tally) and "unanimous" not in tally.lower().replace("not unanimous", ""):
        return "not unanimous"
    return None


def meeting_url(cfg: InstanceConfig, meeting_id: str) -> str:
    base = (cfg.seo.get("pages_base_url") or "").rstrip("/")
    if base:
        return f"{base}/meetings/{meeting_id}/"
    return f"{(cfg.tracker_page_url or cfg.newsroom_url).rstrip('/')}/#m/{meeting_id}"


def _board(cfg: InstanceConfig, key: str) -> str:
    jur = cfg.by_key.get(key)
    return jur.name if jur else key


def build(cfg: InstanceConfig, *, today: date | None = None, meetings: list[dict] | None = None,
          upcoming: dict[str, list] | None = None) -> dict:
    today = today or date.today()
    if meetings is None:
        meetings = json.loads(MEETINGS_JSON.read_text(encoding="utf-8")) if MEETINGS_JSON.exists() else []
    if upcoming is None:
        upcoming = json.loads(UPCOMING_JSON.read_text(encoding="utf-8")) if UPCOMING_JSON.exists() else {}

    # ── Ahead ──────────────────────────────────────────────────────────────
    horizon = (today + timedelta(days=AHEAD_DAYS)).isoformat()
    ahead = sorted((e for es in upcoming.values() for e in es
                    if today.isoformat() <= e.get("date", "") <= horizon),
                   key=lambda e: (e["date"], e.get("time", ""), e.get("name", "")))
    decisions, unread = [], defaultdict(int)
    for e in ahead:
        p = e.get("preview")
        if not p:
            unread[_board(cfg, e.get("source", ""))] += 1
            continue
        items = [i for i in p.get("items", []) if i.get("action") in ("vote", "hearing")]
        if not items and not p.get("closedSession"):
            continue
        decisions.append({
            "date": e["date"], "time": e.get("time", ""), "name": e.get("name", ""),
            "board": _board(cfg, e.get("source", "")), "url": e.get("url", ""),
            "items": [{"item": i.get("item", ""), "why": i.get("why", ""), "action": i["action"]}
                      for i in items],
            "publicComment": bool(p.get("publicComment")),
            "closedSession": bool(p.get("closedSession")),
        })

    # ── Behind ─────────────────────────────────────────────────────────────
    since = today - timedelta(days=BACK_DAYS)
    votes, crowds = [], []
    topic_hits: dict[str, list] = defaultdict(list)
    topic_since = today - timedelta(days=TOPIC_DAYS)
    for m in meetings:
        held = _held(m)
        if held is None or held > today:
            continue
        ref = {"id": m.get("id"), "title": m.get("title", ""), "date": m.get("date", ""),
               "board": _board(cfg, m.get("source", "")), "url": meeting_url(cfg, m.get("id", ""))}
        # Closed sessions are tagged from their agenda's legal notice, which
        # says the same thing every time; they are not a lead.
        if held >= topic_since and not _CLOSED.search(m.get("title", "")):
            for t in m.get("topics") or []:
                if isinstance(t, str) and t.strip() and t.strip().lower() not in GENERIC_TOPICS:
                    topic_hits[t.strip()].append(ref)
        if held < since or m.get("isAgendaOnly"):
            continue
        for v in m.get("votes") or []:
            why = contested(v) if isinstance(v, dict) else None
            if why:
                votes.append({**ref, "item": v.get("item", ""), "why": why})
        n = speaker_count({"publicComment": m.get("publicComment"),
                           "publicCommentSpeakers": m.get("publicCommentSpeakers")})
        if n is not None and n >= CROWD:
            crowds.append({**ref, "speakers": n})

    topics = []
    for name, refs in topic_hits.items():
        seen, uniq = set(), []
        for r in refs:
            if r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        if len(uniq) >= 2:
            topics.append({"topic": name, "meetings": uniq,
                           "boards": sorted({r["board"] for r in uniq})})
    topics.sort(key=lambda t: (-len(t["boards"]), -len(t["meetings"]), t["topic"]))

    overdue = [{"title": m.get("title", ""), "board": _board(cfg, m.get("source", "")),
                "date": m.get("date", ""), "waited": m["_waited"],
                "url": meeting_url(cfg, m.get("id", ""))}
               for m in find_overdue(cfg, meetings, today)]

    return {
        "instance": cfg.name, "today": today.isoformat(),
        "decisions": decisions,
        "votes": votes,
        "crowds": sorted(crowds, key=lambda c: -c["speakers"]),
        "topics": topics[:MAX_TOPICS],
        "overdue": overdue,
        "unread": dict(sorted(unread.items())),
        "ahead_total": len(ahead),
    }


# ── Rendering ────────────────────────────────────────────────────────────────

def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.strftime('%b')} {d.day}"


def _sections(d: dict) -> list[tuple[str, str, list]]:
    """(heading, note when empty, lines). A line is (text, url, sub-lines)."""
    out = []

    lines = []
    for m in d["decisions"]:
        flags = [f for f, on in (("residents can speak", m["publicComment"]),
                                 ("closed session", m["closedSession"])) if on]
        head = f"{_day(m['date'])}{', ' + m['time'] if m['time'] else ''}: {m['board']}, {m['name']}"
        if flags:
            head += f" ({'; '.join(flags)})"
        subs = [f"{i['action'].upper()}: {i['item']}" + (f". {i['why']}" if i["why"] else "")
                for i in m["items"]]
        lines.append((head, m["url"], subs))
    out.append(("Decisions coming up", "No votes or hearings on the agendas read for the next week.", lines))

    by_meeting: dict[str, tuple] = {}
    for v in d["votes"]:
        head = by_meeting.setdefault(v["id"], (f"{v['board']}, {v['title']} ({v['date']})", v["url"], []))
        head[2].append(f"{v['why'].upper()}: {v['item']}")
    out.append(("Contested votes, past week",
                "No failed, tabled or split votes in the past week's summaries.",
                list(by_meeting.values())))

    out.append(("Crowded public comment, past week",
                f"No meeting in the past week had {CROWD} or more residents speak.",
                [(f"{c['speakers']} residents spoke: {c['board']}, {c['title']} ({c['date']})",
                  c["url"], []) for c in d["crowds"]]))

    out.append(("Recurring specifics, past 30 days",
                "No named project or place came up at more than one meeting.",
                [(f"{t['topic']}: {len(t['meetings'])} meetings ({', '.join(t['boards'])})", "",
                  [f"{r['board']}, {r['title']} ({r['date']})" for r in t["meetings"][:5]])
                 for t in d["topics"]]))

    out.append(("Still waiting on a recording or minutes",
                "Nothing is overdue.",
                [(f"{o['board']}, {o['title']} ({o['date']}): agenda only, {o['waited']} days on",
                  o["url"], []) for o in d["overdue"]]))

    gaps = [(f"{board}: {n} meeting{'s' if n != 1 else ''} with no agenda read yet", "", [])
            for board, n in d["unread"].items()]
    out.append(("Next week's meetings without a preview",
                "Every meeting in the next week has a preview.", gaps))
    return out


def render_markdown(d: dict) -> str:
    out = [f"# Reporter tip sheet: week of {_day(d['today'])}", "",
           f"{d['instance']}. Sorted from the tracker's summaries and agenda previews; "
           f"software wrote those, so confirm with the record before reporting.", ""]
    for heading, empty, lines in _sections(d):
        out += [f"## {heading}", ""]
        if not lines:
            out += [empty, ""]
            continue
        for text, url, subs in lines:
            out.append(f"- [{text}]({url})" if url else f"- {text}")
            out += [f"  - {s}" for s in subs]
        out.append("")
    return "\n".join(out)


def render_html(cfg: InstanceConfig, d: dict) -> str:
    esc = html.escape
    th = cfg.theme
    ink, bg = th.get("ink", "#1a1a1a"), th.get("background", "#ffffff")
    accent, rule = th.get("primary_dark", "#333333"), th.get("divider", "#dddddd")
    body = []
    for heading, empty, lines in _sections(d):
        body.append(f"<h2>{esc(heading)}</h2>")
        if not lines:
            body.append(f'<p class="none">{esc(empty)}</p>')
            continue
        body.append("<ul>")
        for text, url, subs in lines:
            label = f'<a href="{esc(url, quote=True)}">{esc(text)}</a>' if url else esc(text)
            sub = "<ul>" + "".join(f"<li>{esc(s)}</li>" for s in subs) + "</ul>" if subs else ""
            body.append(f"<li>{label}{sub}</li>")
        body.append("</ul>")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Reporter tip sheet: {esc(cfg.name)}</title>
<style>
  body {{ margin: 0; background: {bg}; color: {ink}; font: 17px/1.55 Georgia, 'Times New Roman', serif; }}
  main {{ max-width: 720px; margin: 0 auto; padding: 28px 16px 56px; }}
  h1 {{ font-size: 28px; line-height: 1.2; margin: 0 0 6px; }}
  h2 {{ font: 700 14px/1.3 Arial, Helvetica, sans-serif; letter-spacing: .1em; text-transform: uppercase;
       color: {accent}; border-top: 1px solid {rule}; padding-top: 18px; margin: 28px 0 10px; }}
  p.note, p.none {{ margin: 0 0 8px; }}
  p.none {{ font-style: italic; }}
  ul {{ margin: 0; padding-left: 22px; }}
  li {{ margin: 0 0 8px; }}
  li ul {{ margin-top: 6px; font-size: 15px; }}
  a {{ color: {ink}; text-decoration-color: {accent}; text-underline-offset: 3px; }}
  a:focus-visible {{ outline: 2px solid {accent}; outline-offset: 2px; }}
</style>
</head>
<body>
<main>
<h1>Reporter tip sheet: week of {esc(_day(d['today']))}</h1>
<p class="note">{esc(d['instance'])}. Sorted from the tracker's summaries and agenda previews; software wrote those, so confirm with the record before reporting.</p>
{chr(10).join(body)}
</main>
</body>
</html>
"""


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="Write the reporter tip sheet.")
    ap.add_argument("--date", help="treat this day as today, YYYY-MM-DD")
    ap.add_argument("--stdout", action="store_true", help="print the markdown; write nothing")
    args = ap.parse_args()

    cfg = load_instance()
    data = build(cfg, today=date.fromisoformat(args.date) if args.date else None)
    md = render_markdown(data)
    if args.stdout:
        sys.stdout.write(md)
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "tipsheet.md").write_text(md, encoding="utf-8")
    (DATA_DIR / "tipsheet.html").write_text(render_html(cfg, data), encoding="utf-8")
    print(f"[tipsheet]  {len(data['decisions'])} meeting(s) with decisions ahead, "
          f"{len(data['votes'])} contested vote(s), {len(data['crowds'])} crowded comment period(s), "
          f"{len(data['topics'])} recurring topic(s), {len(data['overdue'])} overdue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
