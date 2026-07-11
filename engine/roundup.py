"""Draft the weekly local-government newsletter blurb.

    python -m engine.roundup [--days-back 7] [--dry-run]

Port of marathon-meetings generate_roundup.py, instance-driven: newsroom
name/region from instance.json, jurisdiction labels from the config, links
to instance.tracker_page_url, brand colors in the HTML from theme tokens.
One Sonnet call per generation (via call_claude — cost-ledgered under
purpose="roundup"). CI runs it Mondays or on demand.

Outputs (newsletter paste-ready, marked DRAFT for editorial review):
    data/roundup.txt
    data/roundup.html
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .claude import call_claude, parse_summary_json
from .config import (CLAUDE_MODEL, MEETINGS_JSON, UPCOMING_JSON,
                     InstanceConfig, load_instance, setup_logging)


def collect(cfg: InstanceConfig, days_back: int):
    meetings = json.loads(Path(MEETINGS_JSON).read_text(encoding="utf-8"))
    upcoming = json.loads(Path(UPCOMING_JSON).read_text(encoding="utf-8"))
    today = date.today()
    start = today - timedelta(days=days_back)

    recent = []
    for m in meetings:
        try:
            d = datetime.strptime(m.get("date", ""), "%B %d, %Y").date()
        except ValueError:
            continue
        if start <= d <= today:
            recent.append((d, m))
    recent.sort(key=lambda x: x[0], reverse=True)

    week_ahead = []
    end = today + timedelta(days=7)
    for src, evs in upcoming.items():
        for e in evs:
            try:
                d = datetime.strptime(e.get("date", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= d <= end and "CANCEL" not in (e.get("name", "").upper()):
                week_ahead.append((d, src, e))
    week_ahead.sort(key=lambda x: x[0])
    return recent, week_ahead


def build_prompt(cfg: InstanceConfig, recent, week_ahead) -> str:
    def label(src: str) -> str:
        jur = cfg.by_key.get(src)
        return jur.name if jur else src

    blocks = []
    for d, m in recent:
        actions = "; ".join((m.get("actionItems") or [])[:4])
        kind = ("agenda only — outcomes not yet known" if m.get("isAgendaOnly")
                else "outcome-based summary")
        blocks.append(
            f"[{label(m['source'])}] {m['title']} — {m['date']} ({kind})\n"
            f"Overview: {(m.get('overview') or '')[:600]}\n"
            f"Actions: {actions[:400]}\n")

    ahead = "\n".join(
        f"- {d.strftime('%A %b %d')}: {label(src)} — {e.get('name', '')}"
        f"{(' at ' + e['time']) if e.get('time') else ''}"
        for d, src, e in week_ahead[:14])

    return f"""You are drafting the weekly local-government roundup for the {cfg.newsroom} newsletter. Audience: residents of {cfg.region}. Voice: tight, factual, neighborly — no hype, no editorializing.

Below are this week's meeting summaries from the newsroom's meeting tracker, then the coming week's schedule.

--- THIS WEEK'S MEETINGS ---
{chr(10).join(blocks) if blocks else "(no meetings in the window)"}
--- COMING UP ---
{ahead if ahead else "(nothing posted yet)"}
--- END ---

Write the roundup as JSON with this exact structure and nothing else:

{{
  "headline": "short newsletter section headline, e.g. 'This week in local government'",
  "lede": "1-2 sentence overview of the week's most consequential local-government news",
  "items": [
    {{"body": "one tight sentence (max ~30 words) on a notable decision or development, naming the jurisdiction"}}
  ],
  "coming_up": "1-2 sentences flagging the most notable meetings in the week ahead, with days"
}}

Rules:
- items: 3-6 bullets, most consequential first. Real outcomes over scheduled items; only cite agenda-only meetings with 'is set to' language.
- Never invent facts not in the summaries. Include vote results when given.
- Return ONLY the JSON."""


def render_outputs(cfg: InstanceConfig, data: dict) -> None:
    tracker = cfg.tracker_page_url or cfg.newsroom_url
    primary_dark = cfg.theme.get("primary_dark", "#333333")
    ink = cfg.theme.get("ink", "#1a1a1a")
    stamp = date.today().strftime("%B %d, %Y").replace(" 0", " ")
    items = data.get("items", [])

    txt = [f"{data.get('headline', 'This week in local government').upper()}",
           f"(DRAFT — auto-generated {stamp}; review before publishing)", "",
           data.get("lede", ""), ""]
    for it in items:
        txt.append(f"• {it.get('body', '')}")
    txt += ["", f"Coming up: {data.get('coming_up', '')}", "",
            f"Full summaries: {tracker}"]
    Path("data/roundup.txt").write_text("\n".join(txt), encoding="utf-8")

    lis = "\n".join(
        f'    <li style="margin:0 0 8px;">{it.get("body", "")}</li>' for it in items)
    html = f"""<!-- DRAFT — auto-generated {stamp}; review before publishing -->
<div style="max-width:600px;font-family:Georgia,'Times New Roman',serif;color:{ink};line-height:1.55;">
  <h2 style="font-family:Arial,Helvetica,sans-serif;font-size:15px;letter-spacing:0.12em;color:{primary_dark};margin:0 0 10px;">{data.get('headline', 'THIS WEEK IN LOCAL GOVERNMENT').upper()}</h2>
  <p style="margin:0 0 12px;">{data.get('lede', '')}</p>
  <ul style="margin:0 0 14px;padding-left:20px;">
{lis}
  </ul>
  <p style="margin:0 0 12px;"><em>Coming up:</em> {data.get('coming_up', '')}</p>
  <p style="margin:0;"><a href="{tracker}" style="color:{primary_dark};font-weight:bold;">Full summaries on the {cfg.name} &rarr;</a></p>
</div>"""
    Path("data/roundup.html").write_text(html, encoding="utf-8")


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Draft the weekly newsletter roundup.")
    ap.add_argument("--days-back", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be summarized; no API call")
    args = ap.parse_args()

    cfg = load_instance()
    recent, week_ahead = collect(cfg, args.days_back)
    print(f"[roundup] {len(recent)} meeting(s) in past {args.days_back} days, "
          f"{len(week_ahead)} upcoming in next 7")
    if args.dry_run:
        for d, m in recent:
            print(f"   {d} {m['source']:<14} {m['title'][:45]}")
        return
    if not recent:
        print("[roundup] no recent meetings — skipping generation.")
        return

    raw = call_claude(model=CLAUDE_MODEL, max_tokens=1500,
                      prompt=build_prompt(cfg, recent, week_ahead),
                      meeting_id="(roundup)", jurisdiction="(all)",
                      purpose="roundup")
    data = parse_summary_json(raw, "roundup")
    render_outputs(cfg, data)
    print(f"[roundup] wrote data/roundup.txt and data/roundup.html "
          f"({len(data.get('items', []))} items)")


if __name__ == "__main__":
    main()
