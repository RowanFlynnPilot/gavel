"""Instance configuration: load + validate instance.json, plus engine-level
operational settings (models, timeouts, filesystem layout).

instance.json is THE deployment contract — one file defines an entire
customer instance. Validation is strict: missing required fields or an
unknown adapter name aborts the run with a named error before any network
or API call is made.

Engine-level knobs (model IDs, timeouts, cookie file) stay env-overridable
because they are operator concerns, not instance identity.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from .errors import ConfigError, UnknownAdapterError

logger = logging.getLogger(__name__)

# ── Adapter registry names (modules live in engine/adapters/) ─────────────────
VIDEO_ADAPTERS = {"youtube"}
AGENDA_ADAPTERS = {"civicclerk", "legistar", "primegov", "boardbook", "agendacenter", "municode", "rule_schedule"}
# Agenda adapters that read a meeting's agenda from its portal page (fetch_agenda_text(agendas_cfg, doc_url)).
PORTAL_ADAPTERS = {"civicclerk", "legistar", "primegov"}
# Of those, the ones whose API also gives structured items with recorded votes (fetch_meeting_data).
VOTE_ADAPTERS = {"civicclerk", "legistar"}

# ── Anthropic client (two-tier model strategy, env-overridable) ───────────────
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MODEL_AGENDA = os.environ.get("CLAUDE_MODEL_AGENDA", "claude-haiku-4-5-20251001")
ANTHROPIC_TIMEOUT_SECONDS = int(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "180"))
ANTHROPIC_MAX_RETRIES = int(os.environ.get("ANTHROPIC_MAX_RETRIES", "4"))
# Transcript characters sent to Claude in one call. It was 90,000 until
# Oct. 2026, which cut the reference instance's longest meetings (every council
# and county board meeting among them) right where the night's big votes come:
# one summary called an ordinance "Approved" because the chair's "Motion
# failed. 5 to six." at character 95,595 never reached the model. The longest
# transcript there is ~184,000 characters (~46k tokens, ~$0.14 on Sonnet).
MAX_TRANSCRIPT_CHARS = int(os.environ.get("MAX_TRANSCRIPT_CHARS", "300000"))

# ── Filesystem layout ──────────────────────────────────────────────────────────
SUMMARIES_DIR = Path(os.environ.get("SUMMARIES_DIR", "./summaries"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "./processed_meetings.json"))
INJECTED_FILE = Path(os.environ.get("INJECTED_FILE", "./injected_meetings.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
MEETINGS_JSON = DATA_DIR / "meetings.json"
UPCOMING_JSON = DATA_DIR / "upcoming.json"
# Meeting records rebuilt this run that are too old for the displayed list;
# publish queues them here and pages rewrites their archive pages, then
# removes the file.
ARCHIVE_UPDATES_JSON = DATA_DIR / "archive_updates.json"
COSTS_JSON = DATA_DIR / "costs.json"
INSTANCE_JSON = Path(os.environ.get("INSTANCE_JSON", "./instance.json"))
# The public log of fixes (engine.scrub re-applies it every run).
CORRECTIONS_FILE = Path(os.environ.get("CORRECTIONS_FILE", "./corrections.json"))
# Written by every pipeline run for the workflow's watchdog steps; not committed.
RUN_REPORT = Path(os.environ.get("RUN_REPORT", "./agenda_only_report.json"))

# ── Stuck-video watchdog (engine.watchdog) ─────────────────────────────────────
# When the pipeline first saw each video it could not process. Committed by CI.
SKIPPED_FILE = Path(os.environ.get("SKIPPED_FILE", "./skipped_videos.json"))
# Written by the residential fetcher when YouTube reports a video private or
# removed; keyed by video ID.
UNAVAILABLE_FILE = Path(os.environ.get("UNAVAILABLE_FILE", "./transcripts/unavailable.json"))
# The residential fetcher normally clears a video within ~12 hours.
STALE_VIDEO_HOURS = int(os.environ.get("STALE_VIDEO_HOURS", "36"))

# Days a meeting may stay agenda-only before its upgrade counts as late, for a
# jurisdiction that doesn't set expected_lag_days (engine.overdue).
DEFAULT_EXPECTED_LAG_DAYS = 7

# ── YouTube / yt-dlp / Whisper (operator-side transcript acquisition) ──────────
COOKIES_FILE = os.environ.get("YT_COOKIES_FILE", "")
# YouTube blocks caption AND metadata fetches from cloud IPs regardless of
# cookies (verified Aug 30, 2026). CI sets this false so runs skip the futile
# attempts; the residential fetcher is the transcript path.
CAPTION_FETCH = os.environ.get("CAPTION_FETCH", "true").lower() == "true"
USE_WHISPER_FALLBACK = os.environ.get("USE_WHISPER_FALLBACK", "true").lower() == "true"
# "small" over "tiny": audio-only bodies (Kronenwetter) feed Sonnet from this
# text; garbled names/figures degrade summaries. Local CPU is the only cost.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_SOURCES = [
    s.strip() for s in os.environ.get("WHISPER_SOURCES", "marathon,wausau,weston").split(",")
    if s.strip()
]

# Re-try agenda-only YouTube entries for captions this many days after upload.
AGENDA_RETRY_DAYS = int(os.environ.get("AGENDA_RETRY_DAYS", "14"))


def utf8_console() -> None:
    """Make stdout/stderr UTF-8 (Windows consoles default to a legacy code
    page that can't print every meeting title)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once with a CI-friendly, UTF-8-safe format."""
    utf8_console()
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level)


# ── instance.json loading + validation ─────────────────────────────────────────

def _require(obj: dict, key: str, ctx: str):
    if key not in obj:
        raise ConfigError(f"instance.json: missing required field '{key}' in {ctx}")
    return obj[key]


def _validate_rules(rules, ctx: str) -> None:
    """nth-weekday rules: {name, weekday 0-6 (0=Mon), nth 1-5, time, url?}."""
    if not isinstance(rules, list):
        raise ConfigError(f"{ctx}: must be a list of rules")
    for i, rule in enumerate(rules):
        rctx = f"{ctx}[{i}]"
        if not isinstance(rule, dict):
            raise ConfigError(f"{rctx}: must be an object")
        for field in ("name", "time"):
            if not isinstance(_require(rule, field, rctx), str):
                raise ConfigError(f"{rctx}: '{field}' must be a string")
        for field, lo, hi in (("weekday", 0, 6), ("nth", 1, 5)):
            v = _require(rule, field, rctx)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ConfigError(
                    f"{rctx}: '{field}' must be an integer {lo}-{hi}"
                    f"{' (0=Monday)' if field == 'weekday' else ''}, got {v!r}")


class Jurisdiction:
    """One jurisdiction block from instance.json, validated."""

    def __init__(self, raw: dict):
        ctx = f"jurisdiction '{raw.get('key', '?')}'"
        self.key: str = _require(raw, "key", "jurisdictions[]")
        self.name: str = _require(raw, "name", ctx)
        self.short: str = _require(raw, "short", ctx)
        self.accent: str = _require(raw, "accent", ctx)
        self.doc_host: str = raw.get("doc_host", "")
        self.avatar: str = raw.get("avatar", "")
        self.audio_url: str = raw.get("audio_url", "")
        self.title_strip_patterns: list[str] = raw.get("title_strip_patterns", [])
        self.committee_map: dict[str, str] = raw.get("committee_map", {})
        # Optional officials roster ("Name (Role)" strings) — authoritative
        # name spellings for summarization prompts and Whisper vocabulary
        # hints. Update after each spring election.
        self.officials: list[str] = raw.get("officials", [])
        if not isinstance(self.officials, list) or \
                not all(isinstance(o, str) for o in self.officials):
            raise ConfigError(f"{ctx}: 'officials' must be a list of strings")

        self.video: dict | None = raw.get("video")
        if self.video is not None:
            adapter = _require(self.video, "adapter", f"{ctx}.video")
            if adapter not in VIDEO_ADAPTERS:
                raise UnknownAdapterError(
                    f"{ctx}: unknown video adapter '{adapter}' "
                    f"(known: {sorted(VIDEO_ADAPTERS)})")
            _require(self.video, "channel_url", f"{ctx}.video")

        self.agendas: dict = _require(raw, "agendas", ctx)
        adapter = _require(self.agendas, "adapter", f"{ctx}.agendas")
        if adapter not in AGENDA_ADAPTERS:
            raise UnknownAdapterError(
                f"{ctx}: unknown agendas adapter '{adapter}' "
                f"(known: {sorted(AGENDA_ADAPTERS)})")
        # Per-adapter required fields — fail at load, not mid-scrape.
        if adapter == "civicclerk":
            _require(self.agendas, "api_base", f"{ctx}.agendas")
            _require(self.agendas, "portal", f"{ctx}.agendas")
        elif adapter in ("legistar", "primegov"):
            _require(self.agendas, "client", f"{ctx}.agendas")
        elif adapter == "boardbook":
            _require(self.agendas, "base", f"{ctx}.agendas")
            _require(self.agendas, "org_id", f"{ctx}.agendas")
            _require(self.agendas, "label", f"{ctx}.agendas")
        elif adapter == "agendacenter":
            _require(self.agendas, "base", f"{ctx}.agendas")
        elif adapter == "municode":
            _require(self.agendas, "base", f"{ctx}.agendas")
            _require(self.agendas, "ada_url", f"{ctx}.agendas")
            _require(self.agendas, "blob_base", f"{ctx}.agendas")
            _require(self.agendas, "id_prefix", f"{ctx}.agendas")
        elif adapter == "rule_schedule":
            _validate_rules(_require(self.agendas, "rules", f"{ctx}.agendas"),
                            f"{ctx}.agendas.rules")
        if "fallback_rules" in self.agendas:
            _validate_rules(self.agendas["fallback_rules"], f"{ctx}.agendas.fallback_rules")

        # Days a meeting here normally stays agenda-only before its recording,
        # audio, minutes or votes arrive; engine.overdue flags anything later.
        lag = raw.get("expected_lag_days", DEFAULT_EXPECTED_LAG_DAYS)
        if isinstance(lag, bool) or not isinstance(lag, int) or lag < 1:
            raise ConfigError(
                f"{ctx}: 'expected_lag_days' must be a positive integer, got {lag!r}")
        self.expected_lag_days: int = lag

    @property
    def video_adapter(self) -> str | None:
        return self.video["adapter"] if self.video else None

    @property
    def agendas_adapter(self) -> str:
        return self.agendas["adapter"]


class InstanceConfig:
    """Validated instance.json."""

    def __init__(self, raw: dict, path: Path):
        self.path = path
        inst = _require(raw, "instance", "top level")
        self.name: str = _require(inst, "name", "instance")
        self.newsroom: str = _require(inst, "newsroom", "instance")
        self.newsroom_url: str = _require(inst, "newsroom_url", "instance")
        self.region: str = _require(inst, "region", "instance")
        self.timezone: str = _require(inst, "timezone", "instance")
        self.max_meetings: int = int(_require(inst, "max_meetings", "instance"))
        self.date_cutoff: str = inst.get("date_cutoff", "")
        self.skip_ids: frozenset[str] = frozenset(inst.get("skip_ids", []))
        # Where the tracker is embedded/published for readers (links in the
        # ics feed, roundup, and static pages point here). Optional; modules
        # fall back to newsroom_url.
        self.tracker_page_url: str = inst.get("tracker_page_url", "")
        # Suffix of every calendar-feed UID ("<hash>@<domain>"). Subscribers'
        # calendars key events on the UID, so changing this on a live feed
        # duplicates every event they hold.
        self.ics_uid_domain: str = inst.get("ics_uid_domain", "gavel")
        if not isinstance(self.ics_uid_domain, str) or \
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.ics_uid_domain):
            raise ConfigError(
                "instance.ics_uid_domain must be letters, digits, dots and hyphens "
                f"(e.g. 'gavel'), got {self.ics_uid_domain!r}")
        # The calendar feed's display name and description, as subscribers'
        # calendar apps show them. Optional; default to the instance name and
        # a description built from the jurisdictions.
        self.calendar_name: str = inst.get("calendar_name", "")
        self.calendar_description: str = inst.get("calendar_description", "")
        for field in ("calendar_name", "calendar_description"):
            if not isinstance(getattr(self, field), str):
                raise ConfigError(f"instance.{field} must be a string")
        # Where readers send correction requests. Optional: with none, static
        # pages and the frontend show no "Request a correction" link.
        self.corrections_email: str = inst.get("corrections_email", "")
        if not isinstance(self.corrections_email, str) or (
                self.corrections_email
                and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.corrections_email)):
            raise ConfigError(
                "instance.corrections_email must be an email address, "
                f"got {self.corrections_email!r}")

        self.theme: dict = _require(raw, "theme", "top level")
        self.committee_styles: dict = raw.get("committee_styles", {})
        # Optional feature blocks. seo.pages_base_url enables static meeting
        # pages; seo.noindex=true adds robots/meta noindex (parity-soak
        # deployments must not compete with production for search ranking).
        self.seo: dict = raw.get("seo", {})
        self.analytics: dict = raw.get("analytics", {})
        # Optional hand-off to a companion repo that drafts video reels from
        # newly transcribed meetings (engine.reels): {repo: "owner/name",
        # bodies: regex a meeting's title must match}. Absent = no hand-off.
        self.reels: dict = raw.get("reels", {})
        if not isinstance(self.reels, dict):
            raise ConfigError("instance.json: 'reels' must be an object")
        if self.reels:
            repo = self.reels.get("repo", "")
            if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise ConfigError(f"instance.json: reels.repo must be 'owner/name', got {repo!r}")
            try:
                re.compile(self.reels.get("bodies", ""))
            except (re.error, TypeError) as e:
                raise ConfigError(f"instance.json: reels.bodies is not a valid pattern: {e}") from e
        # Optional: municipality and state names that may trail a street
        # address (", Springfield, IL"). engine.scrub removes one along with a
        # resident's address; with none listed, only the address itself goes.
        self.place_names: list[str] = raw.get("place_names", [])
        if not isinstance(self.place_names, list) or \
                not all(isinstance(p, str) and p.strip() for p in self.place_names):
            raise ConfigError("instance.json: 'place_names' must be a list of non-empty strings")

        jur_raw = _require(raw, "jurisdictions", "top level")
        if not isinstance(jur_raw, list) or not jur_raw:
            raise ConfigError("instance.json: 'jurisdictions' must be a non-empty list")
        self.jurisdictions: list[Jurisdiction] = [Jurisdiction(j) for j in jur_raw]
        keys = [j.key for j in self.jurisdictions]
        if len(keys) != len(set(keys)):
            raise ConfigError(f"instance.json: duplicate jurisdiction keys in {keys}")
        self.by_key: dict[str, Jurisdiction] = {j.key: j for j in self.jurisdictions}

    def jurisdiction(self, key: str) -> Jurisdiction:
        if key not in self.by_key:
            raise ConfigError(
                f"unknown jurisdiction '{key}' (known: {sorted(self.by_key)})")
        return self.by_key[key]


def load_instance(path: Path | None = None) -> InstanceConfig:
    """Load and validate instance.json. Any problem is a named ConfigError."""
    path = path or INSTANCE_JSON
    if not path.exists():
        raise ConfigError(f"instance.json not found at {path.resolve()}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"instance.json is not valid JSON: {e}") from e
    cfg = InstanceConfig(raw, path)
    logger.info("Loaded instance '%s' (%d jurisdictions) from %s",
                cfg.name, len(cfg.jurisdictions), path)
    return cfg
