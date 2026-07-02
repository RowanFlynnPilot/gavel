"""Per-meeting API cost ledger → data/costs.json.

Every Anthropic call in the pipeline is recorded as
    {ts, meeting_id, jurisdiction, purpose, model, input_tokens,
     output_tokens, usd}
so a month of Instance Zero telemetry yields a real per-meeting /
per-jurisdiction cost number for the pricing-floor decision.

Pricing is stored as constants here (USD per million tokens, from the
published Anthropic price list). The pipeline is two-tier by design —
Sonnet for transcripts/minutes, Haiku for agenda-only — so pricing is
resolved per call by model ID. An unrecognized model raises
UnknownModelPricingError rather than silently under-reporting.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import COSTS_JSON
from .errors import UnknownModelPricingError

# USD per million tokens: {model prefix: (input, output)}.
# Matched by longest prefix so dated snapshots (e.g. -20251001) resolve.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4": (15.00, 75.00),
}


def price_for(model: str) -> tuple[float, float]:
    for prefix in sorted(PRICING_PER_MTOK, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICING_PER_MTOK[prefix]
    raise UnknownModelPricingError(
        f"no pricing entry for model '{model}' — add it to "
        f"engine/costs.py PRICING_PER_MTOK")


def usd_for(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = price_for(model)
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 6)


def record(*, meeting_id: str, jurisdiction: str, purpose: str, model: str,
           input_tokens: int, output_tokens: int) -> dict:
    """Append one ledger entry to data/costs.json and return it."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "meeting_id": meeting_id,
        "jurisdiction": jurisdiction,
        "purpose": purpose,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usd": usd_for(model, input_tokens, output_tokens),
    }
    COSTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    ledger: list = []
    if COSTS_JSON.exists():
        ledger = json.loads(COSTS_JSON.read_text(encoding="utf-8"))
    ledger.append(entry)
    COSTS_JSON.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return entry
