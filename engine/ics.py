"""Emit a subscribable iCalendar feed of upcoming meetings.

    python -m engine.ics [--out data/meetings.ics] [--days N]

Port of marathon-meetings generate_ics.py, instance-driven: calendar name
from instance.name, description from the jurisdiction list, event summaries
prefixed with each jurisdiction's display name, DESCRIPTION links to
instance.tracker_page_url (falls back to newsroom_url).

Deterministic output — UIDs hash from (source, date, name) and DTSTAMP
derives from the event date, so the file only changes when the data does.
Timezone: America/Chicago only for v1 (the VTIMEZONE block below); any
other instance.timezone is a named ConfigError rather than silently-wrong
event times.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import UPCOMING_JSON, InstanceConfig, load_instance, setup_logging
from .errors import ConfigError

DEFAULT_DURATION_MIN = 90

_SUPPORTED_TZ = "America/Chicago"
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:DAYLIGHT
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def _escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Fold lines >75 octets per RFC 5545 (char-based; keep content ASCII-safe
    where hand-written)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out, cur = [], line
    while len(cur.encode("utf-8")) > 75:
        cut = 75
        while len(cur[:cut].encode("utf-8")) > 75:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def build_events(cfg: InstanceConfig, days_ahead: int) -> list[dict]:
    data = json.loads(Path(UPCOMING_JSON).read_text(encoding="utf-8"))
    today = date.today()
    end = today + timedelta(days=days_ahead)
    events = []
    for src, evs in data.items():
        jur = cfg.by_key.get(src)
        label = jur.name if jur else src
        for e in evs:
            try:
                d = datetime.strptime(e.get("date", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (today <= d <= end):
                continue
            name = (e.get("name") or "Meeting").strip()
            if "CANCEL" in name.upper():
                continue
            uid_src = f"{src}|{e.get('date')}|{name.lower()}"
            uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20] + "@gavel"
            start_dt = None
            if e.get("time"):
                try:
                    t = datetime.strptime(e["time"].strip(), "%I:%M %p").time()
                    start_dt = datetime.combine(d, t)
                except ValueError:
                    pass
            events.append({
                "uid": uid, "date": d, "start": start_dt,
                "summary": f"{label}: {name}",
                "url": e.get("url") or "",
            })
    events.sort(key=lambda x: (x["date"], x["start"] or datetime.min))
    return events


def render(cfg: InstanceConfig, events: list[dict]) -> str:
    if cfg.timezone != _SUPPORTED_TZ:
        raise ConfigError(
            f"engine.ics supports only timezone '{_SUPPORTED_TZ}' in v1 "
            f"(instance.timezone is '{cfg.timezone}')")
    tracker = cfg.tracker_page_url or cfg.newsroom_url
    juris = "\\, ".join(j.name for j in cfg.jurisdictions)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{cfg.newsroom}//{cfg.name}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(cfg.name)}",
        f"X-WR-CALDESC:Upcoming public meetings for {juris} - from {cfg.newsroom}.",
        f"X-WR-TIMEZONE:{_SUPPORTED_TZ}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    lines.extend(VTIMEZONE.split("\n"))
    for ev in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev['uid']}")
        lines.append(f"DTSTAMP:{ev['date'].strftime('%Y%m%d')}T000000Z")
        if ev["start"]:
            dtend = ev["start"] + timedelta(minutes=DEFAULT_DURATION_MIN)
            lines.append(f"DTSTART;TZID={_SUPPORTED_TZ}:{ev['start'].strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND;TZID={_SUPPORTED_TZ}:{dtend.strftime('%Y%m%dT%H%M%S')}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{ev['date'].strftime('%Y%m%d')}")
        lines.append(f"SUMMARY:{_escape(ev['summary'])}")
        if ev["url"]:
            lines.append(f"URL:{_escape(ev['url'])}")
        lines.append(f"DESCRIPTION:Agendas\\, documents and meeting summaries: {_escape(tracker)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Render the meetings .ics feed.")
    ap.add_argument("--out", default="data/meetings.ics")
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    cfg = load_instance()
    events = build_events(cfg, args.days)
    Path(args.out).write_text(render(cfg, events), encoding="utf-8", newline="")
    print(f"[ics] wrote {args.out} - {len(events)} event(s), next {args.days} days")


if __name__ == "__main__":
    main()
