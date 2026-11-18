"""Shared HTTP GET for adapters: retry, then a named "no answer" error.

Municipal sites are occasionally slow. One timed-out request used to read as
"this jurisdiction has no meetings" and blank it from the tracker, calendar
feed and newsletter image (the reference instance lost its CivicClerk
jurisdiction five times in five weeks at a 10s limit). get() retries, and
when the upstream still doesn't answer raises UpstreamUnavailableError so
the caller can tell "no answer" from "answered with nothing".
"""

from __future__ import annotations

import time

import requests

from .errors import UpstreamUnavailableError

ATTEMPTS = 3
TIMEOUT_SECONDS = 30


def get(url: str, *, timeout: int = TIMEOUT_SECONDS, attempts: int = ATTEMPTS,
        **kwargs) -> requests.Response:
    """GET with retries (2s, then 8s apart). Any HTTP error status counts as
    no answer. Raises UpstreamUnavailableError after the last attempt."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = requests.get(url, timeout=timeout, **kwargs)
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except requests.RequestException as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1) ** 2)
    raise UpstreamUnavailableError(
        f"{url}: no answer after {attempts} attempt(s): {last}") from last


def get_json(url: str, **kwargs):
    """get() + JSON decode; a body that isn't JSON is also no answer."""
    r = get(url, **kwargs)
    try:
        return r.json()
    except ValueError as e:
        raise UpstreamUnavailableError(f"{url}: response was not JSON: {e}") from e
