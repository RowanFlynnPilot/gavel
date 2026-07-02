"""Summary file output — the .md report plus _summary.json / _votes.json
sidecars in SUMMARIES_DIR. Ported unchanged from marathon-meetings
save_summary(); the sidecars are the internal contract consumed by
engine.publish."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .config import SUMMARIES_DIR


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:80]


def save_summary(title: str, url: str, source_key: str, org_label: str,
                 summary: dict, doc_url: str | None = None,
                 civic_data: dict | None = None,
                 video_id: str | None = None) -> str:
    """Write the markdown report + JSON sidecars; return the .md path.

    The meeting ID is appended to the slug AFTER slugify so the 80-char
    truncation can't eat it — titles are not unique across meetings.
    """
    slug = slugify(f"{source_key}-{title}")
    if video_id:
        slug = f"{slug}-{slugify(str(video_id))}"
    path = SUMMARIES_DIR / f"{slug}.md"
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    overview = summary.get("overview", "") if isinstance(summary, dict) else str(summary)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"**Organization:** {org_label}  \n")
        f.write(f"**Source:** {url}  \n")
        if doc_url:
            f.write(f"**Documents:** {doc_url}  \n")
        f.write(f"**Summarized:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write("---\n\n")
        f.write(f"## Meeting Overview\n{overview}\n\n")
        if isinstance(summary, dict):
            if summary.get("discussions"):
                f.write("## Key Discussions\n")
                for d in summary["discussions"]:
                    f.write(f"### {d.get('item','')}\n{d.get('body','')}\n\n")
            if summary.get("publicComment"):
                f.write(f"## Public Comment\n{summary['publicComment']}\n\n")
            if summary.get("actionItems"):
                f.write("## Action Items\n")
                for a in summary["actionItems"]:
                    f.write(f"- {a}\n")

    summary_data = summary if isinstance(summary, dict) else {"overview": str(summary)}
    summary_data.update({
        "title": title, "url": url, "source": source_key,
        "doc_url": doc_url,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    })
    json_path = str(path).replace(".md", "_summary.json")
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(summary_data, jf, indent=2, ensure_ascii=False)

    if civic_data:
        votes_path = str(path).replace(".md", "_votes.json")
        with open(votes_path, "w", encoding="utf-8") as jf:
            json.dump(civic_data, jf, indent=2)

    return str(path)
