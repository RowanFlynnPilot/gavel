"""Anthropic call wrapper: retry with backoff + cost ledger + JSON parsing.

Every summarization call flows through call_claude() so the cost ledger in
data/costs.json is complete by construction.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time

import anthropic

from . import costs
from .config import ANTHROPIC_MAX_RETRIES, ANTHROPIC_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def call_claude(*, model: str, max_tokens: int, prompt: str,
                meeting_id: str, jurisdiction: str, purpose: str) -> str:
    """Call the Messages API with retry, log the cost, return the text.

    Retries on 429, connection/timeout errors, and transient 5xx/529.
    Non-retryable errors (auth, bad request) raise immediately.
    """
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "timeout": ANTHROPIC_TIMEOUT_SECONDS,
    }
    last_err: Exception | None = None
    msg = None
    for attempt in range(ANTHROPIC_MAX_RETRIES):
        try:
            msg = _get_client().messages.create(**kwargs)
            break
        except anthropic.RateLimitError as e:
            last_err = e
        except anthropic.APIConnectionError as e:
            last_err = e
        except anthropic.APITimeoutError as e:
            last_err = e
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status is not None and (status >= 500 or status == 529):
                last_err = e
            else:
                raise
        delay = (2 ** (attempt + 2)) + random.uniform(0, 1.5)
        logger.warning("Anthropic call failed (%s); retry %d/%d in %.1fs",
                       type(last_err).__name__, attempt + 1,
                       ANTHROPIC_MAX_RETRIES, delay)
        time.sleep(delay)
    if msg is None:
        raise last_err

    usage = getattr(msg, "usage", None)
    costs.record(
        meeting_id=meeting_id,
        jurisdiction=jurisdiction,
        purpose=purpose,
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    return msg.content[0].text.strip()


def parse_summary_json(raw: str, source_tag: str) -> dict:
    """Parse Claude's JSON reply; on failure fall back to a bare-overview dict.

    Tags the result with _source so publish and the upgrade passes can tell
    transcript/minutes summaries from agenda-only ones.
    """
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        result = json.loads(raw)
        result["_source"] = source_tag
        return result
    except json.JSONDecodeError:
        return {"overview": raw, "agenda": [], "discussions": [],
                "publicComment": "", "actionItems": [], "committee": "",
                "_source": source_tag}
