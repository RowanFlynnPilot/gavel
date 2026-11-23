"""Reporter tip sheet — what in the tracker is worth a reporter's time this week.

    python -m engine.tipsheet            # writes data/tipsheet.md and data/tipsheet.html

The tracker's readers get every meeting; a newsroom's reporters need the
short list. This sorts what the pipeline already holds into leads, with no
Claude call:

  - Decisions coming up: votes and public hearings on agendas for the next
    week (from the agenda previews)
  - Contested votes: motions that failed, were tabled, or split, with the
    members who voted no where the record names them (the city portal's vote
    records when a meeting has them, else the recording or minutes)
  - Who voted no most: members on record against two or more motions in a month
  - Crowded public comment: meetings where several residents spoke
  - By issue: next week's and last week's meetings under each of the
    instance's issues, for a reporter on a beat
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
from .textutil import plain

AHEAD_DAYS = 7
BACK_DAYS = 7
TOPIC_DAYS = 30
CROWD = 3
MAX_TOPICS = 8
MIN_NO_VOTES = 2          # no votes in TOPIC_DAYS before a member is listed
MAX_MEMBERS = 10
MAX_PER_ISSUE = 4         # meetings listed under an issue, each way

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


def _portal_votes(items):
    """Every (item name, vote) in a portal's vote records, sub-items included."""
    for it in items or []:
        for v in it.get("votes") or []:
            yield it.get("name", ""), v
        yield from _portal_votes(it.get("children"))


def vote_leads(m: dict) -> list[dict]:
    """A meeting's contested votes: {item, why, no, yes, abstain}, the names
    being members' where the record gives them. The portal's own vote records
    (civicItems) are used when they hold any vote, since they name everyone;
    otherwise the votes drawn from the recording or minutes. `yes` is filled
    only for a motion that failed, where the yes votes are the story."""
    portal = list(_portal_votes(m.get("civicItems")))
    leads = []
    if portal:
        for name, v in portal:
            yes, no, abstain = (list(v.get(k) or []) for k in ("yes", "no", "abstain"))
            # A portal entry with no one recorded either way is not a vote
            # that failed; it is a vote that was never entered.
            failed = v.get("passed") is False and bool(yes or no)
            if not failed and not no:
                continue
            tally = f" {len(yes)}-{len(no)}"
            motion = plain(v.get("motion"))
            # An item can be voted on more than once (amend, then adopt): say which.
            leads.append({"item": f"{plain(name)} [{motion}]" if name and motion else plain(name) or motion,
                          "why": ("failed" if failed else "split") + tally,
                          "no": no, "yes": yes if failed else [], "abstain": abstain})
        return leads
    for v in m.get("votes") or []:
        why = contested(v) if isinstance(v, dict) else None
        if why:
            names = {k: [n for n in v.get(k) or [] if isinstance(n, str)] if isinstance(v.get(k), list) else []
                     for k in ("no", "abstain")}
            leads.append({"item": v.get("item", ""), "why": why, "yes": [], **names})
    return leads


def _who(lead: dict) -> str:
    """" (no: A, B; abstained: C)" for a vote whose record names members."""
    parts = [f"{label}: {', '.join(lead[key])}"
             for key, label in (("yes", "yes"), ("no", "no"), ("abstain", "abstained")) if lead.get(key)]
    return f" ({'; '.join(parts)})" if parts else ""


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
    labels = {i["key"]: i["label"] for i in cfg.issues}
    issue_ahead: dict[str, list] = defaultdict(list)
    issue_past: dict[str, list] = defaultdict(list)
    for e in ahead:
        p = e.get("preview")
        if not p:
            unread[_board(cfg, e.get("source", ""))] += 1
            continue
        for key in p.get("issues") or []:
            if key in labels:
                issue_ahead[key].append(
                    f"{_day(e['date'])}: {_board(cfg, e.get('source', ''))}, {e.get('name', '')}")
        items = [i for i in p.get("items", []) if i.get("action") in ("vote", "hearing")]
        # Every hearing on the agenda, not only the ones among the preview's six items.
        listed = " ".join(i.get("item", "").lower() for i in items)
        items += [{"item": h, "why": "", "action": "hearing"} for h in p.get("hearings") or []
                  if isinstance(h, str) and h.strip() and h.strip().lower() not in listed]
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
    dissent: dict[tuple, list] = defaultdict(list)       # (member, board) -> votes they opposed
    seen_votes: set[tuple] = set()
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
        # A meeting posted as two recordings carries the same portal votes on
        # both; each vote is a lead once.
        leads = []
        for lead in ([] if m.get("isAgendaOnly") else vote_leads(m)):
            mark = (ref["board"], ref["date"], lead["item"], lead["why"])
            if mark not in seen_votes:
                seen_votes.add(mark)
                leads.append(lead)
        if held >= topic_since:
            for lead in leads:
                for name in lead["no"]:
                    dissent[(name, ref["board"])].append({**ref, "item": lead["item"]})
        if held < since:
            continue
        # Today's meeting summarized from its agenda hasn't been held yet; it
        # is already listed as ahead.
        for key in ([] if held == today and m.get("isAgendaOnly") else m.get("issues") or []):
            if key in labels:
                issue_past[key].append(f"{ref['board']}, {ref['title']} ({ref['date']})")
        if m.get("isAgendaOnly"):
            continue
        for lead in leads:
            votes.append({**ref, "item": lead["item"], "why": lead["why"], "who": _who(lead)})
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

    # A member who voted no once is not a pattern; twice or more in a month is a lead.
    opposed = [{"member": name, "board": board, "count": len(refs), "votes": refs}
               for (name, board), refs in dissent.items() if len(refs) >= MIN_NO_VOTES]
    opposed.sort(key=lambda o: (-o["count"], o["board"], o["member"]))

    by_issue = [{"issue": labels[key], "ahead": issue_ahead.get(key, []), "past": issue_past.get(key, [])}
                for key in labels if issue_ahead.get(key) or issue_past.get(key)]
    by_issue.sort(key=lambda b: (-(len(b["ahead"]) + len(b["past"])), b["issue"]))

    return {
        "instance": cfg.name, "today": today.isoformat(),
        "decisions": decisions,
        "votes": votes,
        "opposed": opposed[:MAX_MEMBERS],
        "by_issue": by_issue,
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
        head[2].append(f"{v['why'].upper()}: {v['item']}{v.get('who', '')}")
    out.append(("Contested votes, past week",
                "No failed, tabled or split votes in the past week's summaries.",
                list(by_meeting.values())))

    out.append((f"Who voted no most, past {TOPIC_DAYS} days",
                f"No member is on record voting no {MIN_NO_VOTES} or more times.",
                [(f"{o['member']} ({o['board']}): {o['count']} no votes", "",
                  [f"{r['item']} ({r['title']}, {r['date']})" for r in o["votes"][:5]])
                 for o in d["opposed"]]))

    def issue_line(b: dict) -> tuple:
        counts = [f"{len(b[k])} {word}" for k, word in (("ahead", "ahead"), ("past", "in the past week")) if b[k]]
        subs = [f"AHEAD: {x}" for x in b["ahead"][:MAX_PER_ISSUE]] \
            + [f"PAST WEEK: {x}" for x in b["past"][:MAX_PER_ISSUE]]
        return (f"{b['issue']}: {', '.join(counts)}", "", subs)
    out.append(("By issue, next week and past week",
                "No meeting ahead or in the past week is filed under an issue.",
                [issue_line(b) for b in d["by_issue"]]))

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
          f"{len(data['votes'])} contested vote(s), {len(data['opposed'])} member(s) with repeated no votes, "
          f"{len(data['by_issue'])} issue(s) with activity, {len(data['crowds'])} crowded comment period(s), "
          f"{len(data['topics'])} recurring topic(s), {len(data['overdue'])} overdue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
