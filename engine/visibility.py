"""Hidden jurisdictions: tracked by the pipeline, kept off the public site.

A jurisdiction with `"hidden": true` in instance.json is scraped and
summarized like any other, so its record builds up, but it does not appear on
the reader-facing site, in the calendar feed, the newsletter image, the
roundup draft, the static meeting pages or the sitemap. A new board is added
hidden, looked at by an editor, and switched on by removing the flag.

For that look, the site carries a second copy of its data under preview/
whenever anything is hidden: the same app at <site>/preview/ shows every
board, with a banner and noindex. The preview address is unlisted, not
secret, and the repository this runs from is public either way; "hidden"
means "not published to readers", nothing stronger.

    python -m engine.visibility --out frontend/public --preview-dir frontend/public/preview
        writes the public instance.json + data/*.json (hidden boards removed)
        and, when anything is hidden, full copies under the preview dir.
    python -m engine.visibility --preview-index frontend/dist
        after the frontend build: gives dist/preview/ its index.html.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import (ARCHIVE_INDEX_JSON, MEETINGS_JSON, UPCOMING_JSON, InstanceConfig, load_instance,
                     utf8_console)

NOINDEX = '<meta name="robots" content="noindex, nofollow">'


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def public_instance(raw: dict) -> dict:
    """instance.json as readers get it: hidden jurisdictions removed."""
    out = dict(raw)
    out["jurisdictions"] = [j for j in raw.get("jurisdictions", []) if not j.get("hidden")]
    return out


def _write(path: Path, data) -> None:
    """Staged copies are for the browser, not for reading: no indentation.
    (The committed data/ files stay indented so their diffs stay readable.)
    On the reference instance that took the meeting list from 915 KB to 750 KB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8")


def write_views(cfg: InstanceConfig, out_dir: Path, preview_dir: Path | None = None) -> dict:
    """Stage the frontend's runtime files. Returns counts for the log."""
    raw = json.loads(cfg.path.read_text(encoding="utf-8"))
    hidden = cfg.hidden_keys
    meetings = _load(MEETINGS_JSON, [])
    upcoming = _load(UPCOMING_JSON, {})

    _write(out_dir / "instance.json", public_instance(raw))
    public_meetings = [m for m in meetings if m.get("source") not in hidden]
    _write(out_dir / "data" / "meetings.json", public_meetings)
    _write(out_dir / "data" / "upcoming.json", {k: v for k, v in upcoming.items() if k not in hidden})
    # The archive index (engine.archive), when one has been written.
    archive = _load(ARCHIVE_INDEX_JSON, None)
    if isinstance(archive, list):
        _write(out_dir / "data" / "archive.json",
               [r for r in archive if r.get("source") not in hidden])

    if preview_dir is not None:
        if preview_dir.exists():
            shutil.rmtree(preview_dir)
        if hidden:
            _write(preview_dir / "instance.json", {**raw, "preview": True})
            _write(preview_dir / "data" / "meetings.json", meetings)
            _write(preview_dir / "data" / "upcoming.json", upcoming)
            if isinstance(archive, list):
                _write(preview_dir / "data" / "archive.json", archive)
    return {"hidden": sorted(hidden), "public_meetings": len(public_meetings),
            "all_meetings": len(meetings), "preview": bool(hidden and preview_dir is not None)}


def write_preview_index(dist: Path) -> bool:
    """Give dist/preview/ the built app's index.html, marked noindex. The
    app's assets are referenced from the site root, so the same bundle runs;
    it reads its data from preview/ when its address is under preview/."""
    preview = dist / "preview"
    if not preview.is_dir():
        return False
    html = (dist / "index.html").read_text(encoding="utf-8")
    if NOINDEX not in html:
        html = html.replace("<head>", "<head>\n    " + NOINDEX, 1)
    (preview / "index.html").write_text(html, encoding="utf-8")
    return True


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description="Stage public (and preview) data for the frontend.")
    ap.add_argument("--out", help="write the public instance.json and data/ here")
    ap.add_argument("--preview-dir", help="write the full copies here when any board is hidden")
    ap.add_argument("--preview-index", metavar="DIST",
                    help="after the build: copy DIST/index.html to DIST/preview/")
    args = ap.parse_args()

    if args.preview_index:
        made = write_preview_index(Path(args.preview_index))
        print(f"[visibility]  preview page {'written' if made else 'not needed (nothing hidden)'}")
        return 0
    if not args.out:
        ap.error("--out or --preview-index is required")
    stats = write_views(load_instance(), Path(args.out),
                        Path(args.preview_dir) if args.preview_dir else None)
    if stats["hidden"]:
        print(f"[visibility]  hidden from the public site: {', '.join(stats['hidden'])} "
              f"({stats['all_meetings'] - stats['public_meetings']} meeting(s) held back); "
              f"preview copy {'written' if stats['preview'] else 'not requested'}")
    else:
        print(f"[visibility]  every board is public ({stats['public_meetings']} meetings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
