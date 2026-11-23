"""File summaries written before the issue list existed under its issues.

    python -m engine.backfill_issues                 # the displayed list, published boards
    python -m engine.backfill_issues --include-hidden
    python -m engine.backfill_issues --all --limit 50
    python -m engine.backfill_issues --dry-run

New summaries are filed under the instance's `issues` as they are written
(engine.summarize). This does the same for older ones, from the summary's own
text: one call to the cheap model per meeting (purpose=issues_backfill, about
a sixth of a cent), with no source document re-read and nothing re-summarized.

The keys go into the summary's sidecar and onto the meeting's record in
data/meetings.json where it stands. Nothing is re-published, so no meeting
moves or gets a NEW badge. A summary that already has an `issues` key, even
an empty one, is left alone, so the command can be run again.

A Claude failure stops the run: an auth error or outage would repeat for
every meeting. Meetings already filed keep their issues.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .claude import call_claude
from .config import CLAUDE_MODEL_AGENDA, MEETINGS_JSON, InstanceConfig, load_instance, utf8_console
from .state import load_state
from .summarize import clean_issues, issues_block

logger = logging.getLogger(__name__)

MAX_ITEMS = 12
MAX_BODY_CHARS = 400


def _sidecar(info: dict) -> Path | None:
    sf = info.get("summary_file")
    return Path(f"{Path(sf).with_suffix('')}_summary.json") if sf else None


def build_prompt(cfg: InstanceConfig, title: str, org: str, summary: dict) -> str:
    items = "\n".join(
        f"- {d.get('item', '')}: {str(d.get('body', ''))[:MAX_BODY_CHARS]}"
        for d in (summary.get("discussions") or [])[:MAX_ITEMS] if isinstance(d, dict))
    topics = ", ".join(t for t in (summary.get("topics") or []) if isinstance(t, str))
    return f"""You are filing a local government meeting summary under a newsroom's fixed list of issues.

Meeting: {title}
Body: {org}

Summary:
{summary.get('overview', '')}

Items:
{items or '(none listed)'}

Topic tags: {topics or '(none)'}
{issues_block(cfg.issues)}

Return a JSON object and nothing else - no markdown, no preamble:
{{"issues": ["key", "key"]}}"""


def parse(raw: str, cfg: InstanceConfig) -> list[str] | None:
    """The reply's issue keys, or None when the reply isn't the JSON asked for."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
        return None
    return clean_issues(data["issues"], cfg.issues)


def pending(cfg: InstanceConfig, state: dict, *, displayed: list[dict] | None,
            include_hidden: bool) -> list[tuple[str, dict, Path, dict]]:
    """(id, state info, sidecar path, summary) for each summary not yet filed,
    newest list position first. `displayed` limits it to those meetings."""
    hidden = set() if include_hidden else set(cfg.hidden_keys)
    order = [m["id"] for m in displayed] if displayed is not None else list(state["processed"])
    out = []
    for mid in order:
        info = state["processed"].get(mid)
        if not info or info.get("source") in hidden or info.get("source") not in cfg.by_key:
            continue
        path = _sidecar(info)
        if not path or not path.exists():
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(summary, dict) and "issues" not in summary and summary.get("overview"):
            out.append((mid, info, path, summary))
    return out


def run(cfg: InstanceConfig, *, all_summaries: bool = False, include_hidden: bool = False,
        limit: int | None = None, dry_run: bool = False, state: dict | None = None,
        meetings_json: Path | None = None) -> dict:
    """File the pending summaries. Returns counts for the run log."""
    stats = {"pending": 0, "filed": 0, "none_fit": 0, "unusable": 0, "stopped": False}
    if not cfg.issues:
        print("[issues]  instance.json has no 'issues' list; nothing to file under.")
        return stats
    meetings_json = meetings_json or MEETINGS_JSON
    try:
        displayed = json.loads(meetings_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        displayed = []
    state = state if state is not None else load_state()
    todo = pending(cfg, state, displayed=None if all_summaries else displayed,
                   include_hidden=include_hidden)
    stats["pending"] = len(todo)
    todo = todo[:limit] if limit else todo
    print(f"[issues]  {stats['pending']} summary(ies) not yet filed under an issue"
          + (f"; doing {len(todo)}" if len(todo) != stats["pending"] else ""))
    if dry_run:
        for mid, info, _, _ in todo:
            print(f"   would file: {mid}  {info.get('title', '')}")
        return stats

    filed: dict[str, list[str]] = {}
    for mid, info, path, summary in todo:
        jur = cfg.by_key[info["source"]]
        try:
            raw = call_claude(model=CLAUDE_MODEL_AGENDA, max_tokens=100,
                              prompt=build_prompt(cfg, info.get("title", ""), jur.name, summary),
                              meeting_id=mid, jurisdiction=jur.key, purpose="issues_backfill")
        except Exception as e:
            logger.error("issues: Claude call failed (%s); stopping", e)
            stats["stopped"] = True
            break
        issues = parse(raw, cfg)
        if issues is None:
            stats["unusable"] += 1          # left unfiled; the next run asks again
            continue
        summary["issues"] = issues
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        filed[mid] = issues
        stats["filed" if issues else "none_fit"] += 1

    if filed and displayed:
        changed = False
        for m in displayed:
            if isinstance(m, dict) and filed.get(m.get("id")):
                m["issues"] = filed[m["id"]]
                changed = True
        if changed:
            meetings_json.write_text(json.dumps(displayed, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    print(f"[issues]  filed {stats['filed']}; {stats['none_fit']} fit no issue; "
          f"{stats['unusable']} unusable repl{'y' if stats['unusable'] == 1 else 'ies'}"
          + ("; stopped early on a Claude failure" if stats["stopped"] else ""))
    return stats


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="File older summaries under the instance's issues.")
    ap.add_argument("--all", action="store_true", dest="all_summaries",
                    help="every summary in the state file, not only the displayed list")
    ap.add_argument("--include-hidden", action="store_true",
                    help="also boards that are tracked but not yet published")
    ap.add_argument("--limit", type=int, help="file at most this many this run")
    ap.add_argument("--dry-run", action="store_true", help="list what would be filed; no calls")
    args = ap.parse_args()
    stats = run(load_instance(), all_summaries=args.all_summaries, include_hidden=args.include_hidden,
                limit=args.limit, dry_run=args.dry_run)
    return 1 if stats["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
