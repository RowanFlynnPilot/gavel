"""Transcript acquisition: youtube-transcript-api → yt-dlp captions → Whisper.

Ported from marathon-meetings transcript_utils.py + fetch_transcript().
The auth-failure path raises TranscriptAuthError (named, per-jurisdiction)
so an expired-cookie/bot-block condition is distinguishable from "this
video has no captions" (NoCaptionsError → agenda fallback).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Optional

from .config import (
    COOKIES_FILE, USE_WHISPER_FALLBACK, WHISPER_MODEL, WHISPER_SOURCES,
)
from .errors import TranscriptAuthError

__all__ = ["parse_vtt", "fetch_transcript", "NoCaptionsError"]


class NoCaptionsError(Exception):
    """The video genuinely has no captions (and Whisper was unavailable)."""


_VTT_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Signatures in yt-dlp output that mean "YouTube is blocking this client",
# not "no captions exist". These trigger TranscriptAuthError.
_AUTH_BLOCK_SIGNATURES = (
    "sign in to confirm you're not a bot",
    "confirm you’re not a bot",
    "http error 429",
    "use --cookies",
)

_EXPLICIT_NO_CAPTIONS = (
    "no subtitles found",
    "subtitles are disabled",
    "there are no captions",
    "no automatic captions",
)


def parse_vtt(raw: str) -> str:
    """Convert raw WebVTT into a flat transcript with ~5-minute timestamps.

    Only consecutive duplicate lines are dropped; repeated words across a
    transcript (e.g. "yes", "all in favor") survive intact.
    """
    lines: list[str] = []
    last_ts_minute = -5
    for line in raw.splitlines():
        line = line.strip()
        if (not line
                or line.startswith("WEBVTT")
                or line.startswith("Kind:")
                or line.startswith("Language:")):
            continue
        if "-->" in line:
            ts_m = _VTT_TS_RE.match(line)
            if ts_m:
                h, m, s = (int(ts_m.group(i)) for i in (1, 2, 3))
                total_min = h * 60 + m
                if total_min >= last_ts_minute + 5:
                    ts_str = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"
                    lines.append(f"[{ts_str}]")
                    last_ts_minute = total_min
            continue
        line = _HTML_TAG_RE.sub("", line)
        if not line:
            continue
        if lines and line == lines[-1]:
            continue
        lines.append(line)
    return " ".join(lines)


def _fetch_via_youtube_transcript_api(
    video_id: str,
    cookies_file: Optional[str] = None,
    languages: tuple[str, ...] = ("en", "en-US", "en-GB", "en-orig"),
) -> Optional[str]:
    """Try youtube-transcript-api, honoring a Netscape cookies file if given.
    Tolerates both the v1.x instance API and the v0.6 classmethod."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        return None

    try:
        import requests
        from http.cookiejar import MozillaCookieJar

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if cookies_file and os.path.exists(cookies_file):
            jar = MozillaCookieJar(cookies_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar

        api = YouTubeTranscriptApi(http_client=session)
        result = api.fetch(video_id, languages=list(languages))
        entries = list(result)
        text = " ".join(getattr(s, "text", "").strip() for s in entries).strip()
        if text and len(text) > 200:
            return text
    except TypeError:
        pass  # v0.6 doesn't accept http_client= — fall through.
    except Exception:
        pass

    try:
        get_transcript = getattr(YouTubeTranscriptApi, "get_transcript", None)
        if get_transcript:
            entries = get_transcript(video_id, languages=list(languages))
            text = " ".join(e["text"] for e in entries).strip()
            if len(text) > 200:
                return text
    except Exception:
        pass

    return None


def _vid_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def fetch_transcript(url: str, source_key: str = "", upload_date: str = "") -> str:
    """Fetch a transcript for a YouTube video.

    Method 1: youtube-transcript-api (cookie-injected session when available)
    Method 2: yt-dlp caption download
    Method 3: Whisper audio transcription (local-only fallback)

    Raises NoCaptionsError when captions genuinely don't exist, or
    TranscriptAuthError when YouTube is blocking the client (cookie refresh
    needed for this jurisdiction).
    """
    vid_id = _vid_id_from_url(url)

    if vid_id:
        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            print("     [fetch] Trying youtube-transcript-api with cookie session...")
        else:
            print("     [fetch] Trying youtube-transcript-api (no cookies)...")
        try:
            text = _fetch_via_youtube_transcript_api(vid_id, cookies_file=COOKIES_FILE or None)
            if text:
                print(f"     [ok]  Transcript via youtube-transcript-api ({len(text):,} chars)")
                return text
            print("     [warn]  youtube-transcript-api returned no usable transcript - trying yt-dlp...")
        except Exception as e:
            print(f"     [warn]  youtube-transcript-api: {str(e)[:120]} - trying yt-dlp...")

    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "meeting")
        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            # Some channels (e.g. DC Everest) return "video unavailable" on the
            # default web client but extract fine via the android client.
            "--extractor-args", "youtube:player_client=default,android",
            "--write-sub",
            "--write-auto-sub",
            "--skip-download",
            "--sub-format", "vtt",
            "--sub-lang", "en,en-US,en-orig",
            "--sleep-requests", "1",
            "-o", out,
        ]
        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            cmd += ["--cookies", COOKIES_FILE]
            print("     [fetch] yt-dlp with cookies...")
        else:
            print("     [warn]  No cookies file - transcripts may fail on CI IPs")
        cmd.append(url)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            print("     [fetch] yt-dlp transcript fetch timed out after 180s — skipping.")
            raise NoCaptionsError("yt-dlp transcript fetch timed out")

        # Authoritative check: was a .vtt actually written? If yes we have
        # captions regardless of what stderr says.
        vtt_files = [f for f in os.listdir(tmpdir) if f.endswith(".vtt")]
        if vtt_files:
            with open(os.path.join(tmpdir, vtt_files[0]), encoding="utf-8") as f:
                text = parse_vtt(f.read())
                if len(text) > 200:
                    print(f"     [ok]  Transcript via yt-dlp ({len(text):,} chars)")
                    return text

        combined = (r.stdout + r.stderr).lower()
        if any(sig in combined for sig in _AUTH_BLOCK_SIGNATURES):
            raise TranscriptAuthError(
                source_key or "unknown",
                f"YouTube is blocking caption fetch for {url} — cookie refresh "
                f"or residential fetch needed. yt-dlp said: "
                f"{r.stderr.strip()[-200:]}")

        if any(sig in combined for sig in _EXPLICIT_NO_CAPTIONS):
            print("     [fetch] No captions - trying Whisper before giving up...")
            whisper_text = _fetch_transcript_whisper(url, source_key=source_key,
                                                     upload_date=upload_date)
            if whisper_text:
                return whisper_text
            raise NoCaptionsError("No captions available and Whisper unavailable - skipping.")

        if r.returncode != 0:
            print(f"     [warn]  yt-dlp failed: {r.stderr.strip()[-200:]}")

    whisper_text = _fetch_transcript_whisper(url, source_key=source_key,
                                             upload_date=upload_date)
    if whisper_text:
        return whisper_text

    raise NoCaptionsError(
        "Could not fetch transcript via captions or Whisper. "
        "Video may have no audio, or cookies may be expired.")


def _fetch_transcript_whisper(url: str, source_key: str = "",
                              upload_date: str = "") -> str | None:
    """Download audio and transcribe locally with faster-whisper. Only runs
    when USE_WHISPER_FALLBACK is on and the source is whisper-enabled."""
    if not USE_WHISPER_FALLBACK:
        return None
    if source_key and source_key not in WHISPER_SOURCES:
        return None

    print("       Attempting Whisper audio transcription...")
    try:
        import faster_whisper
    except ImportError:
        print("       faster-whisper not installed - skipping Whisper fallback")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_out = os.path.join(tmpdir, "audio")
        # Format 18 = 360p mp4 with audio — legacy format that doesn't need
        # JS runtime decryption, always available on YouTube videos.
        dl_cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--extractor-args", "youtube:player_client=default,android",
            "-f", "18",
            "--no-playlist",
            "--max-filesize", "300m",
            "-o", audio_out + ".%(ext)s",
        ]
        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            dl_cmd += ["--cookies", COOKIES_FILE]
        dl_cmd.append(url)

        print("     [dl]  Downloading audio...")
        r = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            print(f"       Audio download failed: {r.stderr.strip()[-150:]}")
            return None

        audio_files = [f for f in os.listdir(tmpdir)
                       if not f.endswith(".json") and not f.endswith(".part")
                       and os.path.getsize(os.path.join(tmpdir, f)) > 1000]
        if not audio_files:
            print("       No audio file found after download")
            return None

        audio_path = os.path.join(tmpdir, audio_files[0])
        size_mb = os.path.getsize(audio_path) / 1024 / 1024
        print(f"       Audio: {size_mb:.0f} MB - transcribing with Whisper ({WHISPER_MODEL})...")

        import time
        t0 = time.time()
        model = faster_whisper.WhisperModel(WHISPER_MODEL, device="cpu",
                                            compute_type="int8")
        segments, _info = model.transcribe(
            audio_path,
            language="en",
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        elapsed = time.time() - t0
        print(f"       Whisper transcribed {len(text):,} chars in {elapsed/60:.1f} min")

        if len(text) < 100:
            print("       Whisper output too short - likely empty audio")
            return None
        return text
