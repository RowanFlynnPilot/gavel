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

The ledger also drives the spending alarm:

    python -m engine.costs --summary
    python -m engine.costs --check --print-status --markdown spend_body.md

--check trips when the last 24 hours exceed SPEND_ALARM_DAY_USD (default 2.00)
or SPEND_ALARM_DAY_CALLS (default 40); the workflow turns a trip into one
self-closing issue. A one-off recovery (a large transcript backlog) is
expected to trip it; a trip with no such event is the signature of a
re-summarization loop, which otherwise shows up only on the monthly bill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


# ── Spending alarm ────────────────────────────────────────────────────────────

def load_ledger() -> list[dict]:
    if not COSTS_JSON.exists():
        return []
    return json.loads(COSTS_JSON.read_text(encoding="utf-8"))


def window(rows: list[dict], hours: int, now: datetime | None = None) -> list[dict]:
    """Ledger entries from the last `hours` hours."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    return [r for r in rows if datetime.fromisoformat(r["ts"]) >= cutoff]


def total_usd(rows: list[dict]) -> float:
    return sum(r.get("usd", 0) for r in rows)


def breakdown(rows: list[dict]) -> list[tuple[str, str, int, float]]:
    """(purpose, model, calls, usd), costliest first."""
    agg: dict = defaultdict(lambda: [0, 0.0])
    for r in rows:
        k = (r.get("purpose", "?"), r.get("model", "?"))
        agg[k][0] += 1
        agg[k][1] += r.get("usd", 0)
    return sorted(((p, m, n, v) for (p, m), (n, v) in agg.items()), key=lambda x: -x[3])


def repeated_calls(rows: list[dict]) -> list[tuple[str, str, int]]:
    """(meeting_id, purpose, calls) for every meeting summarized more than once
    for the same purpose: the shape a re-summarization loop leaves."""
    counts = Counter((r.get("meeting_id", "?"), r.get("purpose", "?")) for r in rows)
    return sorted(((m, p, n) for (m, p), n in counts.items() if n > 1), key=lambda x: -x[2])


def is_tripped(day: list[dict], max_usd: float, max_calls: int) -> bool:
    return total_usd(day) > max_usd or len(day) > max_calls


def render_markdown(day: list[dict], week: list[dict], max_usd: float,
                    max_calls: int) -> str:
    lines = [
        f"The pipeline made **{len(day)} Claude call(s) costing ${total_usd(day):.2f}** in the "
        f"last 24 hours (alarm thresholds: ${max_usd:.2f} or {max_calls} calls).",
        "",
        "| Purpose | Model | Calls | Cost |",
        "|---|---|---|---|",
    ]
    lines += [f"| {p} | {m} | {n} | ${v:.2f} |" for p, m, n, v in breakdown(day)]
    top = sorted(day, key=lambda r: -r.get("usd", 0))[:5]
    lines += ["", "Costliest calls:", ""]
    lines += [f"- {r['ts']} {r.get('jurisdiction')} `{r.get('meeting_id')}` {r.get('purpose')} "
              f"{r.get('model')}: {r.get('input_tokens', 0):,} in / "
              f"{r.get('output_tokens', 0):,} out = ${r.get('usd', 0):.3f}" for r in top]
    repeats = repeated_calls(day)
    if repeats:
        lines += ["", "Summarized more than once in the window:", ""]
        lines += [f"- `{m}` {p}: {n} calls" for m, p, n in repeats[:10]]
    lines += [
        "",
        f"Last 7 days: {len(week)} calls, ${total_usd(week):.2f}.",
        "",
        "**Expected causes:** a recovery run ingesting a transcript backlog, or a backfill "
        "dispatch. **Leak signature:** the same meeting summarized run after run for an "
        "unchanged transcript or agenda: a \"Summarized more than once\" list above, or "
        "repeated `Summarizing from transcript` lines in the ingest step's log. Per-call detail is "
        "in `data/costs.json`. This issue closes itself once the last 24 hours are back "
        "under the thresholds.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude API cost ledger and spending alarm.")
    parser.add_argument("--summary", action="store_true", help="print 24h / 7d / 30d totals")
    parser.add_argument("--check", action="store_true", help="evaluate the spending alarm")
    parser.add_argument("--max-day-usd", type=float,
                        default=float(os.environ.get("SPEND_ALARM_DAY_USD", "2.00")))
    parser.add_argument("--max-day-calls", type=int,
                        default=int(os.environ.get("SPEND_ALARM_DAY_CALLS", "40")))
    parser.add_argument("--markdown", help="write an issue body here when the alarm trips")
    parser.add_argument("--print-status", action="store_true",
                        help="print 'TRIPPED <usd>' or 'OK <usd>' for the last 24h")
    args = parser.parse_args()

    rows = load_ledger()
    day, week = window(rows, 24), window(rows, 24 * 7)

    if args.summary:
        for label, hours in (("24h", 24), ("7d", 24 * 7), ("30d", 24 * 30)):
            w = window(rows, hours)
            print(f"{label:>4}: {len(w):4d} calls  ${total_usd(w):.2f}", file=sys.stderr)
        for p, m, n, v in breakdown(window(rows, 24 * 30)):
            print(f"      {p:28s} {m:28s} {n:4d} calls  ${v:.2f}", file=sys.stderr)

    if args.check:
        tripped = is_tripped(day, args.max_day_usd, args.max_day_calls)
        if tripped and args.markdown:
            Path(args.markdown).write_text(
                render_markdown(day, week, args.max_day_usd, args.max_day_calls),
                encoding="utf-8")
        if args.print_status:
            print(f"{'TRIPPED' if tripped else 'OK'} {total_usd(day):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
