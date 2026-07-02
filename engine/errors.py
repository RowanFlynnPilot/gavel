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


class TranscriptAuthError(Exception):
    """A transcript fetch failed in a way that indicates bot-blocking or
    expired cookies (not "this video has no captions"). Identifies which
    jurisdiction needs cookie/IP attention so the operator alert is
    distinguishable from a quiet run."""

    def __init__(self, jurisdiction: str, detail: str):
        self.jurisdiction = jurisdiction
        self.detail = detail
        super().__init__(f"[{jurisdiction}] transcript auth failure: {detail}")


class UnknownModelPricingError(Exception):
    """An Anthropic call used a model with no pricing entry in costs.py —
    the cost ledger would silently under-report, so refuse to proceed."""
