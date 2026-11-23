"""Portal vote records for recorded meetings — who voted how, by name.

A council that votes on an electronic board never says who voted which way:
the chair reads "Motion failed. 5 to six." and the recording holds nothing
more, so a transcript summary can't name the six. The city's meeting portal
does record each member's vote, usually days after the meeting.

For a jurisdiction with a video channel whose agendas adapter keeps vote
records (VOTE_ADAPTERS) and can list one day's events (`events_on`), this
pass finds the portal event behind each recent recording and, once the portal
has recorded votes for it, saves them beside the summary as `_votes.json`.
publish then puts them on the meeting record as `civicItems`, which the
Votes tab shows with each member's name. No Claude call.

Finding the event. A recording that already carries its portal link
(`doc_url` with /event/<id>) uses it. Otherwise the recording is matched to
an event held the same day by the words that name the body, with the
jurisdiction's own name and filler ("Meeting", "Committee", "Common") left
out: "Wausau City Council Meeting" and "Common Council Meeting" are both
{council}. A recording titled for a committee of the council ("Wausau City
Council Public Health & Safety Committee") also matches the event named for
the committee alone. Exactly one event must match; two, or none, and the
recording is left alone. A wrong match would put one body's votes under
another's name, so there is no fuzzy fallback.

Each recording is looked up at most once a day (`portal_votes_checked` in the
state file) for `votes_upgrade_window_days` after it is held (default 30).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from .adapters import get_adapter
from .adapters.rule_schedule import name_tokens
from .config import VOTE_ADAPTERS, InstanceConfig, Jurisdiction
from .errors import UpstreamUnavailableError
from .publish import _is_synthetic
from .state import save_state

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
# Words that don't tell one body from another. "council" is not here: it is
# the one word "City Council" and "Common Council" share.
_GENERIC = {"committee", "commission", "common", "special", "regular", "joint", "session"}
_PARENT = {"council"}           # "City Council <Committee Name>": a committee of the council


def has_votes(items: list) -> bool:
    """True when the portal has recorded a vote on any item or sub-item."""
    return any(i.get("votes") or has_votes(i.get("children") or []) for i in items)


def body_words(name: str, jur: Jurisdiction) -> frozenset[str]:
    """The words that name a body, for matching a recording to a portal event."""
    return frozenset(name_tokens(name) - _GENERIC - name_tokens(jur.name))


def match_event(title: str, events: list[dict], jur: Jurisdiction) -> dict | None:
    """The one event these recording-title words name, or None."""
    want = body_words(title, jur)
    if not want:
        return None
    named = [(body_words(e["name"], jur), e) for e in events]
    for target in (want, want - _PARENT):
        hits = [e for words, e in named if words and words == target]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None             # two events under the same body that day: don't guess
    return None


def _day(info: dict) -> date | None:
    raw = str(info.get("meeting_date") or info.get("upload_date") or "")
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8])) if len(raw) == 8 else None
    except ValueError:
        return None


def _votes_path(info: dict) -> Path | None:
    sf = info.get("summary_file")
    return Path(sf.replace(".md", "_votes.json")) if sf else None


def _recorded(path: Path) -> bool:
    try:
        return has_votes(json.loads(path.read_text(encoding="utf-8")).get("items", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def attach(cfg: InstanceConfig, source_key: str, state: dict, today: date | None = None) -> list[str]:
    """Save the portal's vote records beside each recent recording of this
    jurisdiction that doesn't have them yet. Returns the meeting IDs that
    gained votes, for re-publication."""
    jur = cfg.jurisdiction(source_key)
    if jur.video is None or jur.agendas_adapter not in VOTE_ADAPTERS:
        return []
    adapter = get_adapter(jur.agendas_adapter)
    if not hasattr(adapter, "events_on"):
        return []
    today = today or date.today()
    window = int(jur.agendas.get("votes_upgrade_window_days", WINDOW_DAYS))
    since = today - timedelta(days=window)

    due = []
    for vid, info in state["processed"].items():
        day = _day(info)
        path = _votes_path(info)
        if info.get("source") != source_key or _is_synthetic(vid) or not day or not path \
                or not since <= day <= today or info.get("portal_votes_checked") == today.isoformat() \
                or _recorded(path):
            continue
        due.append((vid, info, day, path))
    if not due:
        return []
    print(f"\n[votes]  {len(due)} {source_key} recording(s) within {window} days without "
          f"the portal's vote records; checking...")

    events_by_day: dict[date, list[dict]] = {}
    gained = []
    for vid, info, day, path in due:
        try:
            url = info.get("doc_url") if re.search(r"/event/\d+", info.get("doc_url") or "") else None
            if not url:
                if day not in events_by_day:
                    events_by_day[day] = adapter.events_on(jur.agendas, day)
                event = match_event(info.get("title", ""), events_by_day[day], jur)
                url = event["url"] if event else None
            civic = adapter.fetch_meeting_data(jur.agendas, url) if url else None
        except UpstreamUnavailableError as e:
            # An outage is not an answer: try this recording again next run.
            logger.warning("portal votes: %s did not answer (%s); will retry", source_key, e)
            break
        info["portal_votes_checked"] = today.isoformat()
        if not (civic and has_votes(civic.get("items", []))):
            continue
        path.write_text(json.dumps(civic, indent=2), encoding="utf-8")
        if not info.get("doc_url"):
            info["doc_url"] = url
        gained.append(vid)
        print(f"  [ok]  Votes by member recorded for: {info.get('title', vid)}")
    save_state(state)
    return gained
