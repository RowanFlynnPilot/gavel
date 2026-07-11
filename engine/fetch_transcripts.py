"""Residential transcript fetcher — shared operator infrastructure.

Runs on the operator's machine (NOT blocked by YouTube like CI IPs are) to
grab transcripts for meetings that fell back to agenda-only, saving them to
transcripts/ so the next CI run re-summarizes from the real transcript
(engine.ingest_transcript, driven by the Actions workflow).

    python -m engine.fetch_transcripts VIDEO_ID [VIDEO_ID ...]
    python -m engine.fetch_transcripts --all          # every agenda-only meeting
    python -m engine.fetch_transcripts --all --push   # commit, push, trigger CI

Sources per meeting kind:
  - YouTube-first entries: captions from the video's own ID
  - BoardBook (bb_) entries: recording matched on the district channel
  - Municode entries with a jurisdiction audio_url (e.g. Kronenwetter's
    SoundCloud): audio download → local Whisper transcription
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .adapters import boardbook as bb_adapter
from .adapters import youtube
from .config import (MEETINGS_JSON, STATE_FILE, WHISPER_MODEL, InstanceConfig,
                     load_instance, setup_logging)
from .transcripts import (_fetch_via_youtube_transcript_api, build_whisper_hint,
                          parse_vtt)

TRANSCRIPTS_DIR = Path("./transcripts")
WORKFLOW_FILE = "pipeline.yml"


def _ytdlp_cmd() -> list[str] | None:
    """yt-dlp invocation, handling both PATH and module installs."""
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
        return ["yt-dlp"]
    except FileNotFoundError:
        pass
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, timeout=5)
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    return None


def fetch_transcript_ytdlp(video_id: str) -> str | None:
    ytdlp = _ytdlp_cmd()
    if not ytdlp:
        print("  yt-dlp: not installed (python -m pip install yt-dlp)")
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "meeting")
        cmd = [
            *ytdlp,
            "--no-check-certificate",
            "--extractor-args", "youtube:player_client=default,android",
            "--write-sub",
            "--write-auto-sub",
            "--skip-download",
            "--sub-format", "vtt",
            "--sub-lang", "en,en-US,en-orig",
            "-o", out,
            url,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            print("  yt-dlp: timed out (180s) — video may be very long, try again later")
            return None

        no_caption_signals = [
            "only images are available", "no subtitles found",
            "subtitles are disabled", "no captions", "there are no captions",
        ]
        combined = (r.stdout + r.stderr).lower()
        if any(sig in combined for sig in no_caption_signals):
            print("  yt-dlp: no captions available")
            return None

        if r.returncode == 0:
            vtt_files = [f for f in os.listdir(tmpdir) if f.endswith(".vtt")]
            if vtt_files:
                with open(os.path.join(tmpdir, vtt_files[0]), encoding="utf-8") as f:
                    text = parse_vtt(f.read())
                    if len(text) > 200:
                        return text
        else:
            print(f"  yt-dlp failed: {r.stderr.strip()[-100:]}")
    return None


def fetch_transcript(video_id: str) -> str | None:
    print("  Trying youtube-transcript-api...")
    try:
        text = _fetch_via_youtube_transcript_api(video_id)
        if text:
            return text
    except Exception as e:
        print(f"  youtube-transcript-api: {str(e)[:100]}")
    print("  Trying yt-dlp...")
    return fetch_transcript_ytdlp(video_id)


def fetch_transcript_whisper_url(media_url: str, whisper_hint: str = "") -> str | None:
    """Download audio from any yt-dlp-supported URL (e.g. SoundCloud) and
    transcribe locally with faster-whisper."""
    try:
        import faster_whisper
    except ImportError:
        print("  faster-whisper not installed (python -m pip install faster-whisper) — skipping")
        return None
    ytdlp = _ytdlp_cmd()
    if not ytdlp:
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "audio.%(ext)s")
        print("  Downloading audio...")
        r = subprocess.run(
            [*ytdlp, "--no-check-certificate", "--no-playlist",
             "-f", "bestaudio/best", "-o", out, media_url],
            capture_output=True, text=True, timeout=900,
        )
        files = [f for f in os.listdir(tmpdir)
                 if not f.endswith(".part")
                 and os.path.getsize(os.path.join(tmpdir, f)) > 10000]
        if not files:
            print(f"  Audio download failed: {(r.stderr or '').strip()[-150:]}")
            return None
        audio_path = os.path.join(tmpdir, files[0])
        size_mb = os.path.getsize(audio_path) / 1024 / 1024
        print(f"  Audio: {size_mb:.0f} MB — transcribing with Whisper ({WHISPER_MODEL})...")
        import time
        t0 = time.time()
        model = faster_whisper.WhisperModel(WHISPER_MODEL, device="cpu",
                                            compute_type="int8")
        if whisper_hint:
            print(f"  Whisper vocabulary hint: {whisper_hint[:90]}...")
        segments, _info = model.transcribe(
            audio_path, language="en", beam_size=1, vad_filter=True,
            initial_prompt=whisper_hint or None,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        print(f"  Whisper: {len(text):,} chars in {(time.time()-t0)/60:.1f} min")
        return text if len(text) > 200 else None


# ── Candidate discovery ───────────────────────────────────────────────────────

def _load_meetings() -> list[dict]:
    if not MEETINGS_JSON.exists():
        print(f"Error: {MEETINGS_JSON} not found")
        return []
    try:
        return [m for m in json.loads(MEETINGS_JSON.read_text(encoding="utf-8"))
                if isinstance(m, dict)]
    except json.JSONDecodeError as e:
        print(f"Error: {MEETINGS_JSON} is not valid JSON: {e}")
        return []


def find_agenda_only_meetings() -> list[dict]:
    """Agenda-only meetings whose card URL is their own YouTube video."""
    results = []
    for m in _load_meetings():
        if not m.get("isAgendaOnly"):
            continue
        url = m.get("url") or ""
        if "youtube.com" not in url and "youtu.be" not in url:
            continue
        results.append({"id": m.get("id", ""),
                        "title": m.get("title") or m.get("id", ""),
                        "url": url})
    return results


def find_boardbook_video_matches(cfg: InstanceConfig) -> list[dict]:
    """Match agenda-only bb_ entries to recordings on each district's channel.
    CI can LIST the channels but caption downloads are cloud-IP-blocked, so
    the actual fetch happens here. Returns [{'id', 'fetch_id', 'title'}]."""
    meetings = _load_meetings()
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("processed", {})
        except json.JSONDecodeError:
            pass

    out = []
    for jur in cfg.jurisdictions:
        if jur.agendas_adapter != "boardbook" or not jur.video:
            continue
        bb = [m for m in meetings
              if m.get("isAgendaOnly")
              and str(m.get("id", "")).startswith("bb_")
              and m.get("source") == jur.key]
        if not bb:
            continue
        try:
            videos = youtube.list_videos(jur.video, jur.key, jur.name)
        except Exception as e:
            print(f"  {jur.key} channel fetch failed: {str(e)[:120]}")
            continue
        for m in bb:
            info = state.get(m["id"]) or {}
            if not info:
                try:
                    d = datetime.strptime(m.get("date", ""), "%B %d, %Y")
                    info = {"title": f"{m.get('title', '')} - {d.strftime('%Y-%m-%d')}"}
                except ValueError:
                    continue
            v = bb_adapter.match_recording(info, videos)
            if v:
                out.append({"id": m["id"], "fetch_id": v["id"],
                            "title": m.get("title") or m["id"]})
    return out


def _parse_audio_track_date(title: str) -> str:
    """Parse YYYYMMDD from an audio track title. Formats seen on the
    Kronenwetter SoundCloud: 'June 8, 2026 ...', 'May 19th 2026 ...',
    '05052026 UC ...', 'April  30, 2026 ...'."""
    t = re.sub(r"\s+", " ", title)
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", t)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                     "%B %d %Y").strftime("%Y%m%d")
        except ValueError:
            pass
    m = re.search(r"\b(\d{2})(\d{2})(20\d{2})\b", t)   # MMDDYYYY
    if m:
        return m.group(3) + m.group(1) + m.group(2)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", t)
    if m:
        return f"{m.group(3)}{int(m.group(1)):02d}{int(m.group(2)):02d}"
    return ""


def _municipal_meeting_type(text: str) -> tuple:
    """Classify a municipal meeting/track title into (body, special, resched)."""
    t = " " + text.lower() + " "
    if "community life" in t or "clipp" in t:
        body = "clipp"
    elif "utility" in t or " uc " in t:
        body = "utility"
    elif "administrative policy" in t or " apc " in t:
        body = "admin_policy"
    elif "board of review" in t or " bor " in t:
        body = "review"
    elif "plan commission" in t or " pc " in t:
        body = "plan"
    elif "police and fire" in t or "police & fire" in t or " pfc " in t:
        body = "police_fire"
    elif "redevelopment" in t:
        body = "redevelopment"
    elif "village board" in t or " vb " in t or "board meeting" in t:
        body = "village_board"
    else:
        body = "other"
    return (body, "special" in t, "reschedul" in t)


def find_audio_matches(cfg: InstanceConfig) -> list[dict]:
    """Match agenda-only synthetic entries to a jurisdiction's posted audio
    recordings (jurisdiction.audio_url, e.g. Kronenwetter's SoundCloud).
    Transcription happens locally via Whisper since audio has no captions."""
    meetings = _load_meetings()
    ytdlp = _ytdlp_cmd()
    if not ytdlp:
        return []

    out = []
    for jur in cfg.jurisdictions:
        if not jur.audio_url or jur.agendas_adapter != "municode":
            continue
        prefix = jur.agendas["id_prefix"]
        cands = [m for m in meetings
                 if m.get("isAgendaOnly")
                 and str(m.get("id", "")).startswith(f"{prefix}_")
                 and m.get("source") == jur.key]
        if not cands:
            continue

        print(f"  Fetching {jur.key} audio track list ({jur.audio_url})...")
        try:
            r = subprocess.run(
                [*ytdlp, "--no-check-certificate", "--flat-playlist",
                 "--dump-json", "--playlist-end", "50", jur.audio_url],
                capture_output=True, text=True, timeout=120,
            )
            tracks = []
            for line in r.stdout.strip().splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                title = d.get("title") or ""
                url = d.get("url") or d.get("webpage_url") or ""
                if not url:
                    continue
                tracks.append({"title": title, "url": url,
                               "date": _parse_audio_track_date(title),
                               "type": _municipal_meeting_type(title)})
        except Exception as e:
            print(f"  audio list fetch failed: {str(e)[:100]}")
            continue

        for m in cands:
            try:
                mdate = datetime.strptime(m.get("date", ""), "%B %d, %Y").strftime("%Y%m%d")
            except ValueError:
                continue
            mtype = _municipal_meeting_type(f"{m.get('title','')} {m.get('committee','')}")
            hits = [t for t in tracks if t["date"] == mdate and t["type"] == mtype]
            if len(hits) == 1:
                out.append({"id": m["id"], "fetch_url": hits[0]["url"],
                            "title": m.get("title") or m["id"],
                            "whisper_hint": build_whisper_hint(jur.name, jur.officials)})
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Fetch transcripts locally for agenda-only meetings.")
    parser.add_argument("video_ids", nargs="*",
                        help="YouTube video IDs to fetch transcripts for")
    parser.add_argument("--all", action="store_true",
                        help="Fetch for ALL agenda-only meetings with recordings")
    parser.add_argument("--push", action="store_true",
                        help="Git commit + push after fetching, then trigger CI")
    args = parser.parse_args()

    cfg = load_instance()
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    # Each job: save (transcript filename / meeting ID) + fetch (video ID or
    # audio URL). They differ for BoardBook meetings, whose bb_ IDs are
    # matched to recordings on the district channel.
    if args.all:
        jobs = [{"save": m["id"], "fetch": m["id"], "title": m["title"]}
                for m in find_agenda_only_meetings()]
        jobs += [{"save": m["id"], "fetch": m["fetch_id"], "title": m["title"]}
                 for m in find_boardbook_video_matches(cfg)]
        jobs += [{"save": m["id"], "fetch": m["id"], "fetch_url": m["fetch_url"],
                  "method": "whisper", "title": m["title"],
                  "whisper_hint": m.get("whisper_hint", "")}
                 for m in find_audio_matches(cfg)]
        if not jobs:
            print("No agenda-only meetings with available recordings found.")
            return
        print(f"Found {len(jobs)} agenda-only meeting(s) with recordings:\n")
        for j in jobs:
            if j.get("method") == "whisper":
                via = " (audio -> Whisper)"
            elif j["fetch"] != j["save"]:
                via = f" (video {j['fetch']})"
            else:
                via = ""
            print(f"  [{j['save']}] {j['title']}{via}")
    elif args.video_ids:
        jobs = [{"save": v, "fetch": v, "title": v} for v in args.video_ids]
    else:
        parser.print_help()
        return

    to_fetch = []
    for j in jobs:
        tpath = TRANSCRIPTS_DIR / f"{j['save']}.txt"
        if tpath.exists():
            print(f"\n[skip] {j['save']} — transcript already exists ({tpath})")
        else:
            to_fetch.append(j)

    if not to_fetch:
        print("\nAll transcripts already fetched. Nothing to do.")
        return

    fetched, failed = [], []
    for j in to_fetch:
        print(f"\n{'='*50}")
        print(f"Fetching: {j['save']}")
        if j.get("method") == "whisper":
            print(f"  {j['fetch_url']}")
            text = fetch_transcript_whisper_url(j["fetch_url"],
                                                whisper_hint=j.get("whisper_hint", ""))
        else:
            print(f"  https://www.youtube.com/watch?v={j['fetch']}")
            text = fetch_transcript(j["fetch"])
        if text:
            tpath = TRANSCRIPTS_DIR / f"{j['save']}.txt"
            tpath.write_text(text, encoding="utf-8")
            print(f"  [OK] Saved: {tpath} ({len(text):,} chars)")
            fetched.append(j["save"])
        else:
            print("  [FAIL] No transcript available")
            failed.append(j["save"])

    print(f"\n{'='*50}")
    print(f"Results: {len(fetched)} fetched, {len(failed)} failed")
    if fetched:
        print(f"  Saved to: {TRANSCRIPTS_DIR}/")
        for vid in fetched:
            print(f"    {vid}.txt")
    if failed:
        print("  Failed (no captions):")
        for vid in failed:
            print(f"    {vid}")

    if args.push and fetched:
        print("\nPushing to GitHub...")
        files = [str(TRANSCRIPTS_DIR / f"{vid}.txt") for vid in fetched]
        subprocess.run(["git", "add"] + files, check=True)
        msg = f"chore: add {len(fetched)} transcript(s) for re-summarization"
        subprocess.run(["git", "commit", "-m", msg], check=True)

        push_result = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push_result.returncode != 0:
            stderr_text = push_result.stderr or ""
            if "rejected" in stderr_text or "fetch first" in stderr_text:
                print("  Remote has new commits — pulling and rebasing...")
                rebase_result = subprocess.run(["git", "pull", "--rebase"],
                                               capture_output=True, text=True)
                if rebase_result.returncode != 0:
                    print(f"  Rebase failed:\n{rebase_result.stderr}")
                    print("  Resolve conflicts manually and run 'git push'.")
                    return
                print("  Rebase complete. Retrying push...")
                push_result = subprocess.run(["git", "push"],
                                             capture_output=True, text=True)
            if push_result.returncode != 0:
                print(f"  Push failed:\n{push_result.stderr}")
                return
        print("[OK] Pushed. The next CI run will auto-summarize from these transcripts.")

        try:
            subprocess.run(["gh", "workflow", "run", WORKFLOW_FILE],
                           check=True, capture_output=True)
            print("[OK] Triggered CI workflow.")
        except Exception:
            print("   (Install 'gh' CLI to auto-trigger the workflow)")


if __name__ == "__main__":
    main()
