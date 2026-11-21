"""Named errors for the Gavel engine.

Contract rules (RESHAPE.md): fail fast and loud with named errors — no
defaults for required config, no partial runs, no empty-and-continue.
"""

from __future__ import annotations


class ConfigError(Exception):
    """instance.json is missing, malformed, or fails schema validation."""


class UnknownAdapterError(ConfigError):
    """A jurisdiction names an adapter that isn't in the registry."""


class AdapterStructureError(Exception):
    """An upstream page/API returned a structure the adapter doesn't recognize.

    Raised instead of returning empty — silent HTML drift must surface as a
    pipeline failure, not as "no new meetings".
    """


class UpstreamUnavailableError(Exception):
    """An upstream site or API didn't answer after retries (timeout,
    connection failure, HTTP error, unparseable JSON). Distinct from "it
    answered with nothing": callers keep last-known-good data instead of
    publishing an empty result."""


class CorrectionsFileError(Exception):
    """corrections.json is malformed or an entry is missing a required
    field — a logged fix must never be skipped silently."""


class TranscriptAuthError(Exception):
    """A transcript fetch failed in a way that indicates bot-blocking or
    expired cookies (not "this video has no captions"). Identifies which
    jurisdiction needs cookie/IP attention so the operator alert is
    distinguishable from a quiet run."""

    def __init__(self, jurisdiction: str, detail: str):
        self.jurisdiction = jurisdiction
        self.detail = detail
        super().__init__(f"[{jurisdiction}] transcript auth failure: {detail}")


class TruncatedResponseError(Exception):
    """Claude stopped at max_tokens. Every prompt here asks for one JSON
    object, and a cut-off object is unusable: before this error existed the
    reference instance published four summaries whose "overview" was 16,000
    characters of raw, unfinished JSON."""


class UnknownModelPricingError(Exception):
    """An Anthropic call used a model with no pricing entry in costs.py —
    the cost ledger would silently under-report, so refuse to proceed."""
