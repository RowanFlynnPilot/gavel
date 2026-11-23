"""The archive index — every summarized meeting, findable after it leaves the list.

    python -m engine.archive [--out data/archive.json] [--pages-dir frontend/public/meetings]

The displayed list holds the newest `instance.max_meetings`; everything older
keeps its permanent page (engine.pages) but was reachable only by its address.
This writes one compact row per summarized meeting, newest first, for the
frontend's search of older meetings:

    {id, source, title, date "2026-09-08", committee, agendaOnly,
     overview, topics, issues, text, page}

`text` is the discussion and vote item titles, so a search for a project or a
street finds the meeting; `page` says whether the meeting has a static page
to open. Rows are built from the same sidecars and the same record builder as
the displayed list, after the scrub, so they carry every correction. No
network, no Claude calls.

The file holds every board. engine.visibility stages the copy readers get,
with hidden boards left out.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

from .config import ARCHIVE_INDEX_JSON, InstanceConfig, load_instance, setup_logging
from .publish import build_meeting
from .report import _load_summary, meeting_day
from .state import load_state

logger = logging.getLogger(__name__)

OVERVIEW_CHARS = 260
TEXT_CHARS = 500
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


def records(cfg: InstanceConfig, state: dict | None = None):
    """(meeting day, normalized record) for every meeting in the state file
    that has a summary, in no particular order. The record is the one publish
    would put on the displayed list."""
    state = state if state is not None else load_state()
    for mid, info in state.get("processed", {}).items():
        if info.get("source") not in cfg.by_key or not _SAFE_ID.fullmatch(mid):
            continue
        summary = _load_summary(info)
        day = meeting_day(info)
        if summary is None or day is None:
            continue
        try:
            rec = build_meeting(cfg, mid, info, summary, None)
        except Exception as e:             # one odd record must not cost the whole index
            logger.warning("archive: could not build %s: %s", mid, e)
            continue
        if rec:
            yield day, rec


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…"


def build_index(cfg: InstanceConfig, state: dict | None = None,
                pages_dir: Path | None = None) -> list[dict]:
    """The archive rows, newest first."""
    rows = []
    for day, m in records(cfg, state):
        items = [d.get("item", "") for d in m.get("discussions") or [] if isinstance(d, dict)]
        items += [v.get("item", "") for v in m.get("votes") or [] if isinstance(v, dict)]
        rows.append({
            "id": m["id"], "source": m["source"], "title": m["title"],
            "date": day.isoformat(), "committee": m.get("committee") or "",
            "agendaOnly": bool(m.get("isAgendaOnly")),
            "overview": _clip(m.get("overview", ""), OVERVIEW_CHARS),
            "topics": [t for t in m.get("topics") or [] if isinstance(t, str)],
            "issues": m.get("issues") or [],
            "text": _clip(" · ".join(dict.fromkeys(i.strip() for i in items if i and i.strip())),
                          TEXT_CHARS),
            "page": bool(pages_dir and (pages_dir / m["id"] / "index.html").exists()),
        })
    rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    return rows


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Write the archive index of every summarized meeting.")
    ap.add_argument("--out", default=str(ARCHIVE_INDEX_JSON))
    ap.add_argument("--pages-dir", default="frontend/public/meetings",
                    help="where engine.pages writes meeting pages")
    args = ap.parse_args()
    rows = build_index(load_instance(), pages_dir=Path(args.pages_dir))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[archive]  {len(rows)} meeting(s) indexed in {out} "
          f"({sum(1 for r in rows if r['page'])} with a page)")


if __name__ == "__main__":
    main()
