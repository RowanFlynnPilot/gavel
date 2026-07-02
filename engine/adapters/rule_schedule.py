"""rule_schedule adapter — nth-weekday recurring meeting projection.

Rules come from instance.json as
    {"name": ..., "weekday": 0-6 (0=Mon), "nth": 1-5, "time": "5:00 PM",
     "url": optional link target}
Used directly for jurisdictions whose agendas adapter is rule_schedule
(e.g. Marathon County) and as the fallback_rules projection for scrape-based
adapters (AgendaCenter, BoardBook).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date | None:
    """Return the nth occurrence of weekday (0=Mon) in the given month."""
    first = date(year, month, 1)
    first_match = first + timedelta(days=(weekday - first.weekday()) % 7)
    result = first_match + timedelta(weeks=n - 1)
    return result if result.month == month else None


def last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday in the given month."""
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def months_between(start: date, end: date) -> list[tuple[int, int]]:
    d = start.replace(day=1)
    seen = set()
    while d <= end:
        seen.add((d.year, d.month))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)
    return sorted(seen)


def project(rules: list[dict], jurisdiction_key: str, days_ahead: int,
            default_url: str = "") -> list[dict]:
    """Project rule-based upcoming meetings over the window. Returns
    normalized upcoming records: {date, time, name, url, source}."""
    today = date.today()
    end_date = today + timedelta(days=days_ahead)
    results = []
    for yr, mo in months_between(today, end_date):
        for rule in rules:
            meeting_date = nth_weekday(yr, mo, rule["weekday"], rule["nth"])
            if meeting_date is None or not (today <= meeting_date <= end_date):
                continue
            results.append({
                "date": meeting_date.isoformat(),
                "time": rule["time"],
                "name": rule["name"],
                "url": rule.get("url") or default_url,
                "source": jurisdiction_key,
            })
    return sorted(results, key=lambda x: (x["date"], x["time"]))
