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
import sys
from pathlib import Path

from .errors import ConfigError, UnknownAdapterError

logger = logging.getLogger(__name__)

# ── Adapter registry names (modules live in engine/adapters/) ─────────────────
VIDEO_ADAPTERS = {"youtube"}
AGENDA_ADAPTERS = {"civicclerk", "boardbook", "agendacenter", "municode", "rule_schedule"}

# ── Anthropic client (two-tier model strategy, env-overridable) ───────────────
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MODEL_AGENDA = os.environ.get("CLAUDE_MODEL_AGENDA", "claude-haiku-4-5-20251001")
ANTHROPIC_TIMEOUT_SECONDS = int(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "180"))
ANTHROPIC_MAX_RETRIES = int(os.environ.get("ANTHROPIC_MAX_RETRIES", "4"))
MAX_TRANSCRIPT_CHARS = int(os.environ.get("MAX_TRANSCRIPT_CHARS", "90000"))

# ── Filesystem layout ──────────────────────────────────────────────────────────
SUMMARIES_DIR = Path(os.environ.get("SUMMARIES_DIR", "./summaries"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "./processed_meetings.json"))
INJECTED_FILE = Path(os.environ.get("INJECTED_FILE", "./injected_meetings.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
MEETINGS_JSON = DATA_DIR / "meetings.json"
UPCOMING_JSON = DATA_DIR / "upcoming.json"
COSTS_JSON = DATA_DIR / "costs.json"
INSTANCE_JSON = Path(os.environ.get("INSTANCE_JSON", "./instance.json"))

# ── YouTube / yt-dlp / Whisper (operator-side transcript acquisition) ──────────
COOKIES_FILE = os.environ.get("YT_COOKIES_FILE", "")
USE_WHISPER_FALLBACK = os.environ.get("USE_WHISPER_FALLBACK", "true").lower() == "true"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")
WHISPER_SOURCES = [
    s.strip() for s in os.environ.get("WHISPER_SOURCES", "marathon,wausau,weston").split(",")
    if s.strip()
]

# Re-try agenda-only YouTube entries for captions this many days after upload.
AGENDA_RETRY_DAYS = int(os.environ.get("AGENDA_RETRY_DAYS", "14"))


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once with a CI-friendly, UTF-8-safe format."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
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
        self.title_strip_patterns: list[str] = raw.get("title_strip_patterns", [])
        self.committee_map: dict[str, str] = raw.get("committee_map", {})

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
            _require(self.agendas, "rules", f"{ctx}.agendas")

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

        self.theme: dict = _require(raw, "theme", "top level")
        self.committee_styles: dict = raw.get("committee_styles", {})

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
