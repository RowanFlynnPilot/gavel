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
  - Channel videos CI never created an entry for (gap sweep): captions plus
    a transcripts/<id>.meta.json sidecar, since CI cannot ask YouTube for
    per-video metadata either

Exits 1, after doing everything else, when yt-dlp is unavailable or a gap
sweep channel listing fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import boardbook as bb_adapter
from .adapters import youtube
from .config import (MEETINGS_JSON, STATE_FILE, WHISPER_MODEL, InstanceConfig,
                     load_instance, setup_logging)
from .state import load_state
from .transcripts import (_fetch_via_youtube_transcript_api, build_whisper_hint,
                          parse_vtt)

TRANSCRIPTS_DIR = Path("./transcripts")
# Videos YouTube reports as private/removed. Committed with the transcripts so
# CI's stuck-video watchdog can dismiss them and later runs stop retrying.
UNAVAILABLE_FILE = TRANSCRIPTS_DIR / "unavailable.json"
WORKFLOW_FILE = "pipeline.yml"

# The display window is max_meetings wide, so older gaps can never surface and
# summarizing them is pure API spend. Widen for one run to recover from a
# longer outage.
GAP_SWEEP_DAYS = int(os.environ.get("GAP_SWEEP_DAYS", "45"))
# Whisper jobs take ~15+ minutes each; a backlog must not push a scheduled run
# past its time limit and lose everything before the push.
MAX_AUDIO_JOBS = int(os.environ.get("MAX_AUDIO_JOBS", "3"))

# Only explicit, permanent signals. A bare "Video unavailable" is NOT one:
# yt-dlp's default web client returns it spuriously for videos that extract
# fine through the android client.
_GONE_SIGNALS = (
    "private video",
    "has been removed",
    "no longer available",
    "account associated with this video has been terminated",
)


class VideoGone(Exception):
    """YouTube says the video is private or removed — retrying won't help."""


def _is_gone(ytdlp_output: str) -> bool:
    text = ytdlp_output.lower()
    return any(sig in text for sig in _GONE_SIGNALS)


def _load_unavailable() -> dict:
    if not UNAVAILABLE_FILE.exists():
        return {}
    return json.loads(UNAVAILABLE_FILE.read_text(encoding="utf-8"))


def _mark_unavailable(video_id: str, reason: str, title: str = "") -> None:
    gone = _load_unavailable()
    gone[video_id] = {
        "reason": reason[:200],
        "title": title,
        "detected": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    UNAVAILABLE_FILE.write_text(json.dumps(gone, indent=2, sort_keys=True),
                                encoding="utf-8")


def meta_sidecar_path(meeting_id: str) -> Path:
    return TRANSCRIPTS_DIR / f"{meeting_id}.meta.json"


def _ytdlp_cmd() -> list[str] | None:
    """yt-dlp invocation, handling both PATH and module installs."""
    # Both probes must see exit code 0: `python -m yt_dlp` on an interpreter
    # without the module exits 1 but doesn't raise.
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return ["yt-dlp"]
    except FileNotFoundError:
        pass
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    print("  [err]  yt-dlp not available (neither on PATH nor as a module of "
          f"{sys.executable}) — run: python -m pip install yt-dlp")
    return None


def fetch_transcript_ytdlp(video_id: str) -> str | None:
    ytdlp = _ytdlp_cmd()
    if not ytdlp:
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
        if _is_gone(combined):
            err = next((ln for ln in r.stderr.splitlines() if "ERROR" in ln),
                       r.stderr.strip())
            raise VideoGone(err.strip()[:200])
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
    '05052026 UC ...', 'April  30, 2026 ...', '2026 09 21 PC Meeting Recording'."""
    t = re.sub(r"\s+", " ", title)
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", t)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                     "%B %d %Y").strftime("%Y%m%d")
        except ValueError:
            pass
    m = re.search(r"\b(20\d{2})[ ._-]+(\d{1,2})[ ._-]+(\d{1,2})\b", t)   # YYYY MM DD
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))).strftime("%Y%m%d")
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


def _pick_audio_track(cands: list[dict]) -> dict | None:
    """The recording among same-date, same-type tracks, or None when they are
    different recordings. Identical titles are one recording uploaded twice;
    the listing is newest-first, so the first is the latest upload."""
    titles = {re.sub(r"\s+", " ", c["title"]).strip().lower() for c in cands}
    return cands[0] if len(titles) == 1 else None


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

        # A title-format change shows up in the newest uploads; older
        # oddities aren't news.
        undated = [t["title"] for t in tracks[:5] if not t["date"]]
        if undated:
            print(f"  [warn] {jur.key}: {len(undated)} of the 5 newest audio titles "
                  f"have no recognizable date (format change?) e.g. {undated[0]!r}")

        for m in cands:
            try:
                mdate = datetime.strptime(m.get("date", ""), "%B %d, %Y").strftime("%Y%m%d")
            except ValueError:
                continue
            mtype = _municipal_meeting_type(f"{m.get('title','')} {m.get('committee','')}")
            hit = _pick_audio_track(
                [t for t in tracks if t["date"] == mdate and t["type"] == mtype])
            if hit:
                out.append({"id": m["id"], "fetch_url": hit["url"],
                            "title": m.get("title") or m["id"],
                            "whisper_hint": build_whisper_hint(jur.name, jur.officials)})
    return out


def find_unprocessed_channel_videos(cfg: InstanceConfig,
                                    failures: list[str]) -> list[dict]:
    """Channel videos that never made it into the tracker at all.

    CI creates YouTube-first meetings from captions or an agenda fallback;
    when both fail the video is skipped WITHOUT creating an entry, so the
    agenda-only scans above never see it. Sweeping the channel listings
    against processed state catches those gaps. A channel that can't be
    listed is appended to `failures`; the other channels still sweep.
    """
    processed = load_state().get("processed", {})
    window_start = (datetime.now() - timedelta(days=GAP_SWEEP_DAYS)).strftime("%Y%m%d")
    cutoff = max(cfg.date_cutoff, window_start)

    out = []
    for jur in cfg.jurisdictions:
        # Must stay the same test as youtube_first in engine.pipeline.main.
        if jur.video is None or jur.agendas_adapter in ("boardbook", "municode"):
            continue
        try:
            videos = youtube.list_videos(jur.video, jur.key, jur.name)
        except Exception as e:
            print(f"  [err]  {jur.key} channel fetch failed: {str(e)[:120]}")
            failures.append(f"{jur.key}: gap sweep could not list the channel "
                            f"({str(e)[:120]})")
            continue
        for v in videos:
            vid = v.get("id", "")
            if not vid or vid in processed or vid in cfg.skip_ids:
                continue
            # Undated videos can't be placed in the tracker.
            date = v.get("meeting_date") or v.get("upload_date") or ""
            if not date or date < cutoff:
                continue
            out.append({"id": vid, "title": v.get("title") or vid,
                        "source": jur.key,
                        "meeting_date": v.get("meeting_date") or "",
                        "upload_date": v.get("upload_date") or "",
                        "duration": v.get("duration")})
    return out


def _write_meta_sidecar(job: dict) -> None:
    """Ship what the channel listing knows alongside a gap-swept transcript;
    engine.ingest_transcript reads it instead of asking YouTube."""
    meta = job.get("meta")
    if not meta:
        return
    mpath = meta_sidecar_path(job["save"])
    if not mpath.exists():
        mpath.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"[meta] Wrote {mpath.name}")


def _cap_audio_jobs(jobs: list[dict], max_audio: int) -> tuple[list[dict], int]:
    """Run order (caption jobs, then at most max_audio audio jobs) and the
    number of audio jobs left for the next run."""
    audio = [j for j in jobs if j.get("method") == "whisper"]
    captions = [j for j in jobs if j.get("method") != "whisper"]
    return captions + audio[:max_audio], max(0, len(audio) - max_audio)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Fetch transcripts locally for agenda-only meetings.")
    parser.add_argument("video_ids", nargs="*",
                        help="YouTube video IDs to fetch transcripts for")
    parser.add_argument("--all", action="store_true",
                        help="Fetch for ALL agenda-only meetings with recordings, "
                             "plus channel videos the pipeline never ingested")
    parser.add_argument("--push", action="store_true",
                        help="Git commit + push after fetching, then trigger CI")
    args = parser.parse_args()
    if not args.all and not args.video_ids:
        parser.print_help()
        return

    cfg = load_instance()
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    failures: list[str] = []
    if _ytdlp_cmd() is None:
        failures.append(f"yt-dlp is not available to {sys.executable}")
    _run(args, cfg, failures)
    if failures:
        print(f"\n{'!'*60}")
        print(f"[FETCHER FAILURE]  {len(failures)} problem(s) kept this run "
              f"from covering everything:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)


def _run(args: argparse.Namespace, cfg: InstanceConfig, failures: list[str]) -> None:
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
        jobs += [{"save": m["id"], "fetch": m["id"], "title": m["title"],
                  "meta": {"title": m["title"], "source": m["source"],
                           "meeting_date": m["meeting_date"],
                           "upload_date": m["upload_date"],
                           "duration": m["duration"]}}
                 for m in find_unprocessed_channel_videos(cfg, failures)]
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
    else:
        jobs = [{"save": v, "fetch": v, "title": v} for v in args.video_ids]

    gone_ids = set(_load_unavailable())
    to_fetch = []
    for j in jobs:
        tpath = TRANSCRIPTS_DIR / f"{j['save']}.txt"
        if j.get("method") != "whisper" and j["fetch"] in gone_ids:
            print(f"\n[skip] {j['save']} — video is private/removed ({UNAVAILABLE_FILE})")
            continue
        # Before the exists check: transcripts fetched before sidecars existed
        # still need one.
        _write_meta_sidecar(j)
        if tpath.exists():
            print(f"\n[skip] {j['save']} — transcript already exists ({tpath})")
        else:
            to_fetch.append(j)

    to_fetch, deferred = _cap_audio_jobs(to_fetch, MAX_AUDIO_JOBS)
    if deferred:
        print(f"\n[defer] {deferred} audio transcription(s) left for the next run "
              f"(MAX_AUDIO_JOBS={MAX_AUDIO_JOBS})")

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
            try:
                text = fetch_transcript(j["fetch"])
            except VideoGone as e:
                print(f"  [gone] {e} — recorded in {UNAVAILABLE_FILE}")
                _mark_unavailable(j["fetch"], str(e), j.get("title", ""))
                failed.append(j["save"])
                continue
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
        # The whole directory: metadata sidecars and unavailable.json ship too.
        subprocess.run(["git", "add", str(TRANSCRIPTS_DIR)], check=True)
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
