"""In-place cutover: turn a checkout of the reference instance's repo
(marathon-meetings) into the engine layout, keeping everything it has
published.

    python tools/cutover_in_place.py --target ../mm-checkout [--rehearsal]

Run from the gavel repo, against a clean checkout of the target at the commit
to cut over from. It only rewrites the working tree and stages the result
(`git add -A`); committing and pushing are left to the person running it, so
the whole change can be read as one diff first.

What moves
  src/data/meetings.json, upcoming.json  -> data/
  public/meetings.ics, roundup.*         -> data/
  costs.json                             -> data/costs.json (rows gain the
                                            engine's purpose/meeting_id fields)
  public/meetings/, sitemap.xml,
  robots.txt                             -> frontend/public/
  CLAUDE.md                              -> docs/pre-cutover-CLAUDE.md (the
                                            instance's own notes, for reference)
(public/digest.png is not carried: every run renders it before deploying.)
What stays where it is
  processed_meetings.json, injected_meetings.json, skipped_videos.json,
  corrections.json, summaries/, transcripts/, docs/, README.md, .venv, logs
What is replaced by the engine's
  every script and the single-file frontend -> engine/, frontend/, tests/,
  tools/, assets/, instance.json, requirements.txt, CLAUDE.md,
  scripts/refresh-transcripts.ps1 (same path: the scheduled task on the
  residential machine keeps working unchanged)
Workflows
  .github/workflows/update-meetings.yml keeps its file name (run history, the
  staleness sentinel and manual dispatch all key on it) with the engine's
  pipeline inside, on the target's cron minute.

instance.json is written with the target's public address and indexing on.

--rehearsal additionally removes the job's `environment:` (a deployment
environment usually admits only the main branch), so the result can be pushed
to a side branch and run by hand: the workflow itself skips the deploy and the
issue steps off main.
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

# Engine files that are this repo's own state or soak paperwork, not code.
GAVEL_SKIP = (
    "processed_meetings.json", "injected_meetings.json", "corrections.json",
    "skipped_videos.json", "data/", "summaries/", "transcripts/",
    "frontend/public/meetings/", "frontend/public/sitemap.xml", "frontend/public/robots.txt",
    "SOAK.md", "RESHAPE.md", ".claude/", ".github/workflows/",
)

# Target files that survive untouched.
TARGET_KEEP = (
    "processed_meetings.json", "injected_meetings.json", "skipped_videos.json",
    "corrections.json", "summaries/", "transcripts/", "docs/", "README.md",
)

MOVES = (
    ("src/data/meetings.json", "data/meetings.json"),
    ("src/data/upcoming.json", "data/upcoming.json"),
    ("public/meetings.ics", "data/meetings.ics"),
    ("public/roundup.txt", "data/roundup.txt"),
    ("public/roundup.html", "data/roundup.html"),
    ("public/meetings", "frontend/public/meetings"),
    ("public/sitemap.xml", "frontend/public/sitemap.xml"),
    ("public/robots.txt", "frontend/public/robots.txt"),
    ("CLAUDE.md", "docs/pre-cutover-CLAUDE.md"),
)


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed in {cwd}:\n{r.stderr}")
    return r.stdout


def tracked(repo: Path) -> list[str]:
    return [f for f in git(repo, "ls-files", "-z").split("\0") if f]


def under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes)


def convert_ledger(rows: list[dict]) -> list[dict]:
    """The reference ledger names the script that made each call; the engine's
    names a purpose and a meeting. Old rows keep their numbers and get a
    purpose (the script) and a unique placeholder meeting, so the spending
    alarm never reads them as one meeting summarized many times."""
    out = []
    for i, r in enumerate(rows):
        row = dict(r)
        row.setdefault("purpose", row.pop("script", "legacy"))
        row.setdefault("meeting_id", f"legacy-{i}")
        row.setdefault("jurisdiction", "")
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", required=True, help="checkout of the repo to convert")
    ap.add_argument("--base-url", help="public address of the target's site "
                    "(default: https://<owner>.github.io/<repo> from its origin remote)")
    ap.add_argument("--rehearsal", action="store_true",
                    help="make the workflow runnable on a side branch")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not (target / "processed_meetings.json").exists() or not (target / ".git").exists():
        sys.exit(f"{target} does not look like a checkout of the instance to convert")
    if (target / "engine").exists():
        sys.exit(f"{target} already has engine/: it has been converted")
    if git(target, "status", "--porcelain", "--untracked-files=no").strip():
        sys.exit(f"{target} has uncommitted changes; start from a clean checkout")
    if git(GAVEL, "status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("the gavel repo has uncommitted changes; the cutover must come from a commit")

    base_url = args.base_url
    if not base_url:
        m = re.search(r"github\.com[:/]([^/]+)/([^/.\s]+)", git(target, "remote", "get-url", "origin"))
        if not m:
            sys.exit("could not read owner/repo from the target's origin; pass --base-url")
        base_url = f"https://{m.group(1).lower()}.github.io/{m.group(2)}"

    old_workflow = (target / ".github/workflows/update-meetings.yml").read_text(encoding="utf-8")
    cron = re.search(r"cron:\s*'([^']+)'", old_workflow)
    if not cron:
        sys.exit("could not read the target workflow's cron line")

    # 1. Moves (before anything is deleted).
    staged: list[tuple[Path, str]] = []
    holding = target / ".cutover_tmp"
    holding.mkdir()
    for i, (src, dst) in enumerate(MOVES):
        s = target / src
        if not s.exists():
            print(f"[skip]  {src} not present")
            continue
        h = holding / str(i)
        shutil.move(str(s), str(h))
        staged.append((h, dst))
    ledger = json.loads((target / "costs.json").read_text(encoding="utf-8")) \
        if (target / "costs.json").exists() else []

    # 2. Remove every tracked file that isn't kept.
    removed = 0
    for f in tracked(target):
        if under(f, TARGET_KEEP):
            continue
        p = target / f
        if p.exists():
            p.unlink()
            removed += 1
    for d in sorted((p for p in target.rglob("*") if p.is_dir() and ".git" not in p.parts
                     and ".venv" not in p.parts and "node_modules" not in p.parts),
                    key=lambda p: -len(p.parts)):
        if d != holding and holding not in d.parents and not any(d.iterdir()):
            d.rmdir()

    # 3. The engine's tracked files.
    copied = 0
    for f in tracked(GAVEL):
        if under(f, GAVEL_SKIP):
            continue
        dst = target / f
        if under(f, TARGET_KEEP) and dst.exists():
            continue                    # the target's own copy wins
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(GAVEL / f, dst)
        copied += 1

    # 4. Published data into the engine's layout.
    for h, dst in staged:
        d = target / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists():
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        shutil.move(str(h), str(d))
    holding.rmdir()
    (target / "data").mkdir(exist_ok=True)
    (target / "data/costs.json").write_text(
        json.dumps(convert_ledger(ledger), indent=2) + "\n", encoding="utf-8", newline="\n")

    # 4b. assets/rosters.json, which other tools read for name spellings, is
    #     from here on an export of instance.json's officials.
    r = subprocess.run([sys.executable, "-m", "engine.rosters"], cwd=target,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit(f"engine.rosters failed in the converted tree:\n{r.stdout}{r.stderr}")

    # 5. instance.json: this deployment's address, indexing on.
    inst_path = target / "instance.json"
    text = inst_path.read_text(encoding="utf-8")
    text, n1 = re.subn(r'("pages_base_url":\s*)"[^"]*"', lambda m: f'{m.group(1)}"{base_url}"', text)
    text, n2 = re.subn(r'("noindex":\s*)true', r"\1false", text)
    if n1 != 1 or n2 != 1:
        sys.exit("instance.json: expected one pages_base_url and one noindex: true")
    inst_path.write_text(text, encoding="utf-8", newline="\n")
    inst = json.loads(text)
    assert inst["seo"]["pages_base_url"] == base_url and inst["seo"]["noindex"] is False

    # 6. Workflows: the engine's pipeline under the target's file name and
    #    cron; the sentinel pointed at that file.
    wf_dir = target / ".github/workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    pipeline = (GAVEL / ".github/workflows/pipeline.yml").read_text(encoding="utf-8")
    pipeline, n = re.subn(r"(- cron:\s*)'[^']+'", lambda m: f"{m.group(1)}'{cron.group(1)}'", pipeline)
    if n != 1:
        sys.exit("pipeline.yml: expected exactly one cron line")
    pipeline = re.sub(r"    # Off the top of the hour[^\n]*\n(?:    #[^\n]*\n)*",
                      "    # Off the top of the hour: GitHub drops scheduled runs under load and\n"
                      "    # names :00 as its busiest time.\n", pipeline, count=1)
    if args.rehearsal:
        pipeline, n = re.subn(r"\n    environment:\n      name: github-pages\n      url: [^\n]*\n",
                              "\n", pipeline)
        if n != 1:
            sys.exit("pipeline.yml: could not find the environment block to drop for a rehearsal")
    (wf_dir / "update-meetings.yml").write_text(pipeline, encoding="utf-8", newline="\n")
    sentinel = (GAVEL / ".github/workflows/staleness-sentinel.yml").read_text(encoding="utf-8")
    sentinel, n = re.subn(r"PIPELINE_WORKFLOW: pipeline\.yml", "PIPELINE_WORKFLOW: update-meetings.yml",
                          sentinel)
    if n != 1:
        sys.exit("staleness-sentinel.yml: expected one PIPELINE_WORKFLOW line")
    (wf_dir / "staleness-sentinel.yml").write_text(sentinel, encoding="utf-8", newline="\n")

    git(target, "add", "-A")
    summary = git(target, "status", "--short")
    kinds = {k: sum(1 for line in summary.splitlines() if line[:2].strip().startswith(k))
             for k in ("A", "D", "M", "R")}
    print(f"[cutover]  {target}")
    print(f"   address      {base_url}")
    print(f"   cron         {cron.group(1)}")
    print(f"   removed      {removed} legacy file(s); copied {copied} engine file(s)")
    print(f"   staged       {kinds['A']} added, {kinds['D']} deleted, {kinds['M']} modified, "
          f"{kinds['R']} renamed")
    print(f"   ledger       {len(ledger)} row(s) carried into data/costs.json")
    print("   Nothing is committed. Read `git status` and `git diff --cached --stat`, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
