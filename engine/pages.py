"""Static, indexable HTML pages for every meeting summary + sitemap/robots.

    python -m engine.pages [--out-dir frontend/public]

Port of marathon-meetings generate_pages.py, instance-driven: colors from
theme tokens, newsroom/tracker links from instance.json, jurisdiction
labels/accents from the jurisdictions list.

Feature-gated by seo.pages_base_url — absent means the instance doesn't
want static pages and the module exits cleanly. seo.noindex=true emits
robots.txt Disallow + per-page noindex meta (REQUIRED for parity-soak
deployments so they never compete with the production site for ranking).

Pages are written for currently-displayed meetings and never deleted —
the pages directory becomes the instance's permanent archive; the sitemap
covers everything on disk.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path

from .config import MEETINGS_JSON, InstanceConfig, load_instance, setup_logging

E = html.escape


def _iso(date_str):
    try:
        return datetime.strptime(date_str, "%B %d, %Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def render_page(cfg: InstanceConfig, m: dict, base_url: str, noindex: bool) -> str:
    jur = cfg.by_key.get(m["source"])
    label = jur.name if jur else m["source"]
    accent = jur.accent if jur else cfg.theme.get("primary_dark", "#3e847a")
    primary_dark = cfg.theme.get("primary_dark", "#3e847a")
    background = cfg.theme.get("background", "#ffffff")
    ink = cfg.theme.get("ink", "#1a1a1a")
    divider = cfg.theme.get("divider", "#dddddd")
    tracker = cfg.tracker_page_url or cfg.newsroom_url

    title = f"{m['title']} — {label} — {m['date']}"
    desc = (m.get("overview") or "")[:300]
    page_url = f"{base_url}/meetings/{m['id']}/"
    app_url = f"{base_url}/#{m['id']}"

    topics = "".join(f'<span class="tag">{E(t)}</span>' for t in (m.get("topics") or []))
    disc = "".join(
        f"<h3>{E(d.get('item', ''))}</h3><p>{E(d.get('body', ''))}</p>"
        for d in (m.get("discussions") or []))

    votes_rows = ""
    for v in (m.get("votes") or []):
        who = " · ".join(x for x in [
            f"Moved by {v['mover']}" if v.get("mover") else "",
            f"Seconded by {v['second']}" if v.get("second") else ""] if x)
        votes_rows += (
            f"<li><strong>{E(v.get('item', ''))}</strong> — "
            f"{E(v.get('outcome', ''))}{(' ' + E(v['tally'])) if v.get('tally') else ''}"
            f"{('<br><em>' + E(who) + '</em>') if who else ''}</li>")
    votes_html = f"<h2>Votes</h2><ul>{votes_rows}</ul>" if votes_rows else ""

    actions = "".join(f"<li>{E(a)}</li>" for a in (m.get("actionItems") or []))
    actions_html = f"<h2>Action items</h2><ul>{actions}</ul>" if actions else ""

    provenance = ("Summary based on the published agenda — outcomes were not yet "
                  "available when this was written." if m.get("isAgendaOnly") else
                  "Summary generated from the meeting recording or official minutes.")

    ld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "datePublished": _iso(m.get("date")),
        "publisher": {"@type": "Organization", "name": cfg.newsroom,
                      "url": cfg.newsroom_url},
        "mainEntityOfPage": page_url,
    }, ensure_ascii=False)

    robots_meta = '<meta name="robots" content="noindex, nofollow">\n' if noindex else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{robots_meta}<title>{E(title)}</title>
<meta name="description" content="{E(desc)}">
<link rel="canonical" href="{page_url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="{E(cfg.newsroom)}">
<meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(desc)}">
<meta property="og:url" content="{page_url}">
<script type="application/ld+json">{ld}</script>
<style>
  body {{ margin:0; font-family: Georgia, 'Times New Roman', serif; background:{background}; color:{ink}; line-height:1.6; }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 0 18px 48px; }}
  header.site {{ background:{primary_dark}; padding:14px 18px; }}
  header.site a {{ color:#fff; text-decoration:none; font-family: Arial, Helvetica, sans-serif; font-weight:bold; letter-spacing:0.08em; font-size:14px; }}
  .kicker {{ font-family: Arial, Helvetica, sans-serif; font-size:12px; letter-spacing:0.14em; color:{accent}; text-transform:uppercase; margin:26px 0 6px; font-weight:bold; }}
  h1 {{ font-size:28px; line-height:1.2; margin:0 0 4px; }}
  .date {{ color:#6b6156; font-size:14px; margin-bottom:14px; }}
  .tag {{ display:inline-block; border:1px solid {accent}; color:{accent}; border-radius:999px; padding:2px 10px; font-size:11px; font-family: Arial, Helvetica, sans-serif; letter-spacing:0.08em; margin:0 6px 6px 0; text-transform:uppercase; }}
  h2 {{ font-size:18px; border-bottom:1px solid {divider}; padding-bottom:4px; margin-top:28px; }}
  h3 {{ font-size:15px; margin:18px 0 4px; }}
  .prov {{ font-size:12.5px; color:#6b6156; border-left:3px solid {divider}; padding-left:10px; margin:18px 0; }}
  .cta {{ display:inline-block; background:{accent}; color:#fff; text-decoration:none; padding:9px 16px; font-family: Arial, Helvetica, sans-serif; font-size:13px; font-weight:bold; letter-spacing:0.06em; margin:8px 12px 0 0; }}
  .cta.alt {{ background:#fff; color:{accent}; border:1px solid {accent}; }}
  footer {{ margin-top:40px; font-size:12px; color:#6b6156; border-top:1px solid {divider}; padding-top:12px; }}
</style>
</head>
<body>
<header class="site"><a href="{E(tracker)}">{E(cfg.newsroom.upper())} — {E(cfg.name.upper())}</a></header>
<div class="wrap">
  <div class="kicker">{E(label)} · {E(m.get('committee', ''))}</div>
  <h1>{E(m['title'])}</h1>
  <div class="date">{E(m['date'])}{(' · ' + E(m['duration'])) if m.get('duration') else ''}</div>
  <div>{topics}</div>
  <p>{E(m.get('overview', ''))}</p>
  <p>
    <a class="cta" href="{E(tracker)}">Open the interactive tracker</a>
    {f'<a class="cta alt" href="{E(m.get("url", ""))}">Watch / source</a>' if m.get('url') else ''}
    {f'<a class="cta alt" href="{E(m.get("docUrl", ""))}">Agenda &amp; documents</a>' if m.get('docUrl') else ''}
  </p>
  <div class="prov">{provenance} These AI-assisted summaries review the public record and are not a substitute for official minutes.</div>
  {votes_html}
  <h2>Discussion</h2>
  {disc or '<p><em>No detailed discussion summary available.</em></p>'}
  {actions_html}
  <footer>Published by <a href="{E(cfg.newsroom_url)}">{E(cfg.newsroom)}</a> · <a href="{app_url}">Permalink in the tracker</a></footer>
</div>
</body>
</html>
"""


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Render static meeting pages + sitemap.")
    ap.add_argument("--out-dir", default="frontend/public")
    args = ap.parse_args()

    cfg = load_instance()
    base_url = (cfg.seo or {}).get("pages_base_url", "").rstrip("/")
    if not base_url:
        print("[pages] seo.pages_base_url not configured — static pages disabled.")
        return
    noindex = bool((cfg.seo or {}).get("noindex", False))

    out_root = Path(args.out_dir)
    pages_dir = out_root / "meetings"
    pages_dir.mkdir(parents=True, exist_ok=True)

    meetings = json.loads(Path(MEETINGS_JSON).read_text(encoding="utf-8"))
    written = 0
    for m in meetings:
        mid = m.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", mid):
            continue
        d = pages_dir / mid
        d.mkdir(exist_ok=True)
        (d / "index.html").write_text(render_page(cfg, m, base_url, noindex),
                                      encoding="utf-8")
        written += 1

    all_pages = sorted(p.parent.name for p in pages_dir.glob("*/index.html"))
    urls = [f"{base_url}/"] + [f"{base_url}/meetings/{pid}/" for pid in all_pages]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sm.append(f"  <url><loc>{html.escape(u)}</loc></url>")
    sm.append("</urlset>")
    (out_root / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")

    if noindex:
        robots = "User-agent: *\nDisallow: /\n"
    else:
        robots = f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n"
    (out_root / "robots.txt").write_text(robots, encoding="utf-8")

    mode = "NOINDEX (parity soak)" if noindex else "indexable"
    print(f"[pages] wrote {written} meeting page(s) ({mode}); sitemap covers "
          f"{len(all_pages)} page(s) total.")


if __name__ == "__main__":
    main()
