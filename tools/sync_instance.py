"""Bring the engine in this repo into a deployed instance's repo.

    python tools/sync_instance.py --target ../marathon-meetings [--check]

The engine is developed here; a deployment (the reference instance lives in
RowanFlynnPilot/marathon-meetings) is this code plus that instance's own
state and data. This copies the code across and leaves everything the
deployment owns alone. It stages nothing and commits nothing: read
`git status` and `git diff` in the target, run its tests, then commit there.

Copied from here (tracked files only, from a clean checkout):
  engine/, tests/, tools/, frontend/ (not frontend/public's published pages),
  assets/, scripts/, docs/, requirements.txt, CLAUDE.md, .gitignore,
  .gitattributes
  A file under engine/ or tests/ that no longer exists here is removed there.
Written for the target:
  instance.json            this repo's, with the target's own
                           seo.pages_base_url and seo.noindex kept
  .github/workflows/update-meetings.yml
                           this repo's pipeline.yml, on the target's cron
  .github/workflows/staleness-sentinel.yml
                           pointed at update-meetings.yml
Never touched:
  processed_meetings.json, injected_meetings.json, skipped_videos.json,
  corrections.json, summaries/, transcripts/, data/, README.md,
  frontend/public/meetings, sitemap.xml, robots.txt, assets/rosters.json

--check reports what would change and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

GAVEL = Path(__file__).resolve().parent.parent

# This repo's own state, data and paperwork: never copied.
SKIP = (
    "processed_meetings.json", "injected_meetings.json", "corrections.json",
    "skipped_videos.json", "data/", "summaries/", "transcripts/",
    "frontend/public/meetings/", "frontend/public/sitemap.xml", "frontend/public/robots.txt",
    "SOAK.md", "RESHAPE.md", ".claude/", ".github/workflows/", "instance.json", "README.md",
)
# Directories where a file deleted here is deleted in the target too.
MIRRORED = ("engine/", "tests/")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed in {cwd}:\n{r.stderr}")
    return r.stdout


def tracked(repo: Path) -> list[str]:
    return [f for f in git(repo, "ls-files", "-z").split("\0") if f]


def under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes)


def same_text(a: bytes, b: bytes) -> bool:
    """Equal apart from line endings (a Windows checkout may hold CRLF)."""
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def instance_json(target: Path) -> str:
    """This repo's instance.json carrying the target's address and indexing."""
    theirs = json.loads((target / "instance.json").read_text(encoding="utf-8")).get("seo", {})
    base, noindex = theirs.get("pages_base_url", ""), bool(theirs.get("noindex", False))
    text = (GAVEL / "instance.json").read_text(encoding="utf-8")
    text, n1 = re.subn(r'("pages_base_url":\s*)"[^"]*"', lambda m: f'{m.group(1)}"{base}"', text)
    text, n2 = re.subn(r'("noindex":\s*)(?:true|false)',
                       lambda m: m.group(1) + ("true" if noindex else "false"), text)
    if n1 != 1 or n2 != 1:
        sys.exit("instance.json: expected one seo.pages_base_url and one seo.noindex")
    return text


def workflows(target: Path) -> dict[str, str]:
    wf = target / ".github/workflows/update-meetings.yml"
    cron = re.search(r"cron:\s*'([^']+)'", wf.read_text(encoding="utf-8"))
    if not cron:
        sys.exit("could not read the target workflow's cron line")
    pipeline = (GAVEL / ".github/workflows/pipeline.yml").read_text(encoding="utf-8")
    pipeline, n = re.subn(r"(- cron:\s*)'[^']+'", lambda m: f"{m.group(1)}'{cron.group(1)}'", pipeline)
    if n != 1:
        sys.exit("pipeline.yml: expected exactly one cron line")
    pipeline = re.sub(r"    # Off the top of the hour[^\n]*\n(?:    #[^\n]*\n)*",
                      "    # Off the top of the hour: GitHub drops scheduled runs under load and\n"
                      "    # names :00 as its busiest time.\n", pipeline, count=1)
    sentinel = (GAVEL / ".github/workflows/staleness-sentinel.yml").read_text(encoding="utf-8")
    sentinel, n = re.subn(r"PIPELINE_WORKFLOW: pipeline\.yml", "PIPELINE_WORKFLOW: update-meetings.yml",
                          sentinel)
    if n != 1:
        sys.exit("staleness-sentinel.yml: expected one PIPELINE_WORKFLOW line")
    return {".github/workflows/update-meetings.yml": pipeline,
            ".github/workflows/staleness-sentinel.yml": sentinel}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", required=True, help="checkout of the instance repo")
    ap.add_argument("--check", action="store_true", help="report what would change; write nothing")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not (target / "engine").is_dir() or not (target / "instance.json").exists():
        sys.exit(f"{target} is not a converted instance (no engine/ or instance.json)")
    if git(GAVEL, "status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("this repo has uncommitted changes; sync from a commit")
    if git(target, "status", "--porcelain", "--untracked-files=no").strip():
        sys.exit(f"{target} has uncommitted changes; commit or discard them first")

    changed: list[str] = []
    ours = [f for f in tracked(GAVEL) if not under(f, SKIP)]
    for f in ours:
        src, dst = (GAVEL / f).read_bytes(), target / f
        if dst.exists() and same_text(src, dst.read_bytes()):
            continue
        changed.append(("update " if dst.exists() else "add    ") + f)
        if not args.check:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(GAVEL / f, dst)

    keep = set(ours)
    for f in tracked(target):
        if under(f, MIRRORED) and f not in keep:
            changed.append("remove " + f)
            if not args.check:
                (target / f).unlink()

    generated = {"instance.json": instance_json(target), **workflows(target)}
    for f, text in generated.items():
        dst = target / f
        if dst.exists() and same_text(text.encode("utf-8"), dst.read_bytes()):
            continue
        changed.append(("update " if dst.exists() else "add    ") + f)
        if not args.check:
            dst.write_text(text, encoding="utf-8", newline="\n")

    head = git(GAVEL, "rev-parse", "--short", "HEAD").strip()
    verb = "would change" if args.check else "changed"
    print(f"[sync]  gavel {head} -> {target}: {len(changed)} file(s) {verb}")
    for line in changed:
        print("   " + line)
    if changed and not args.check:
        print("   Nothing is staged or committed. Run the target's tests, read the diff, then commit there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
