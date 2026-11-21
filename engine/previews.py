"""Agenda previews — a plain-language look at an upcoming meeting's agenda.

The tracker's promise is the agenda before the meeting and the votes after.
Summaries cover "after"; this covers "before": for each upcoming meeting
whose agenda is posted, one Haiku call turns the agenda into a short preview
(what's on it, what's up for a decision, whether residents can speak). The
preview rides on the meeting's record in data/upcoming.json as `preview`, and
the frontend shows it in the Upcoming panel.

PREVIEW (on an upcoming record):
  {overview, items: [{item, why, action}], publicComment, closedSession, generated}
  action is "vote" | "hearing" | "discussion" | "report".

Cost control, since upcoming.json is rebuilt every run:
  - data/previews.json caches each preview by meeting and by the agenda
    text's hash; an unchanged agenda never costs a second call.
  - an agenda is re-read at most every PREVIEW_RECHECK_HOURS, and a meeting
    is previewed at most MAX_GENERATIONS times however often its agenda is
    revised.
  - at most PREVIEW_MAX_PER_RUN calls per run; only meetings in the next
    PREVIEW_DAYS days.
Jurisdictions on rule_schedule have no posted agenda to read and get none.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .adapters import for_jurisdiction, get_adapter
from .portal_cache import is_residential
from .claude import call_claude
from .config import CLAUDE_MODEL_AGENDA, InstanceConfig, UPCOMING_JSON
from .summarize import RESIDENT_PRIVACY_RULES

logger = logging.getLogger(__name__)

PREVIEW_DAYS = int(os.environ.get("PREVIEW_DAYS", "8"))
PREVIEW_MAX_PER_RUN = int(os.environ.get("PREVIEW_MAX_PER_RUN", "10"))
PREVIEW_RECHECK_HOURS = int(os.environ.get("PREVIEW_RECHECK_HOURS", "12"))
MAX_GENERATIONS = 3
MIN_AGENDA_CHARS = 200
# Agenda text sent to the model. Long agendas put new business and closed
# sessions last, so this is generous: 30,000 characters is about a cent.
MAX_AGENDA_CHARS = int(os.environ.get("PREVIEW_MAX_AGENDA_CHARS", "30000"))
# Part of every cached agenda hash: bump it when the prompt changes in a way
# that should rewrite previews already cached (still capped per meeting).
PROMPT_VERSION = "2"
ACTIONS = ("vote", "hearing", "discussion", "report")


def cache_path() -> Path:
    return UPCOMING_JSON.parent / "previews.json"


def preview_key(entry: dict) -> str:
    src = f"{entry.get('source')}|{entry.get('date')}|{(entry.get('name') or '').lower()}"
    return hashlib.sha1(src.encode()).hexdigest()[:20]


def _load_cache(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; starting the preview cache over", p)
        return {}


def _save_cache(cache: dict, p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                 encoding="utf-8")


# ── Agenda text for an upcoming record, per agenda platform ────────────────

_BB_ATTACHMENTS = re.compile(r"\s*Attachments:\s*\(\s*\d+\s*\).*$", re.DOTALL)

def _boardbook_text(agendas_cfg: dict, entry: dict) -> str | None:
    m = re.search(r"[?&]meeting=(\d+)", entry.get("url") or "")
    if not m:
        return None          # a rule-projected meeting: nothing posted yet
    agenda = get_adapter("boardbook").fetch_agenda(agendas_cfg, m.group(1))
    # Each item carries its attachment list (file names, upload times); on a
    # policy-heavy agenda those run to thousands of characters before the
    # first item of substance. The preview needs the items, not the files.
    items = [_BB_ATTACHMENTS.sub("", i).strip() for i in agenda.get("items") or []]
    return "\n".join(i for i in items if i and not i.startswith("[Detail:")) or None


def _municode_text(agendas_cfg: dict, entry: dict) -> str | None:
    # The upcoming record keeps the meeting's page, not the agenda's id; the
    # hub row for the same date and name has it once the agenda is posted.
    adapter = get_adapter("municode")
    compact = (entry.get("date") or "").replace("-", "")
    for m in adapter.list_meetings(agendas_cfg, max_pages=1):
        if m.get("guid") and m.get("date") == compact and m.get("name") == entry.get("name"):
            return adapter.fetch_agenda_text(agendas_cfg, m["guid"])
    return None


def _agendacenter_text(agendas_cfg: dict, entry: dict) -> str | None:
    url = entry.get("url") or ""
    if "/ViewFile/Agenda/" not in url:
        return None          # a rule-projected meeting: nothing posted yet
    from .pdftext import fetch_pdf_text
    return fetch_pdf_text(url, timeout=20)


def _portal_text(adapter_name: str):
    def fetch(agendas_cfg: dict, entry: dict) -> str | None:
        return get_adapter(adapter_name).fetch_agenda_text(agendas_cfg, entry.get("url") or "")
    return fetch


_AGENDA_TEXT = {
    "boardbook": _boardbook_text,
    "municode": _municode_text,
    "agendacenter": _agendacenter_text,
    "civicclerk": _portal_text("civicclerk"),
    "legistar": _portal_text("legistar"),
    "primegov": _portal_text("primegov"),
    "diligent": _portal_text("diligent"),
    "iqm2": _portal_text("iqm2"),
    "revize": _portal_text("revize"),
    "townweb": _portal_text("townweb"),
    "heygov": _portal_text("heygov"),
}


def agenda_text_for(cfg: InstanceConfig, entry: dict) -> str | None:
    """The posted agenda's text for an upcoming record, or None when the
    platform has nothing readable for it yet."""
    jur = cfg.jurisdiction(entry["source"])
    fetch = _AGENDA_TEXT.get(jur.agendas_adapter)
    if fetch is None:
        return None
    if is_residential(jur):
        # Fetched from a residential connection: read what that fetcher committed.
        text = for_jurisdiction(jur).fetch_agenda_text(jur.agendas, entry.get("url") or "")
    else:
        text = fetch(jur.agendas, entry)
    return text if text and len(text.strip()) >= MIN_AGENDA_CHARS else None


# ── The preview itself ──────────────────────────────────────────────────────

def build_prompt(cfg: InstanceConfig, entry: dict, agenda_text: str) -> str:
    jur = cfg.jurisdiction(entry["source"])
    when = entry["date"] + (f" at {entry['time']}" if entry.get("time") else "")
    return f"""You are a local government reporter covering {cfg.region} for {cfg.newsroom}.

Below is the posted agenda for an UPCOMING public meeting. Nothing on it has happened yet. Write a short preview that tells residents what is on the agenda and what is up for a decision, so they can decide whether to attend or comment.

Meeting: {entry['name']}
Body: {jur.name}
When: {when}

Return a JSON object with this exact structure and nothing else - no markdown, no preamble:

{{
  "overview": "1-2 plain-language sentences naming the most consequential things on this agenda for residents. Use 'is scheduled to' or future tense.",
  "items": [
    {{"item": "the agenda item in plain language, 12 words or fewer", "why": "one sentence: what it is and what the body is being asked to do", "action": "vote | hearing | discussion | report"}}
  ],
  "publicComment": true,
  "closedSession": false
}}

Rules:
- items: at most 6, the most consequential first, drawn from the WHOLE agenda: new business, unfinished business and reports usually come after long consent and policy lists. Skip procedural items (call to order, roll call, approval of minutes, adjournment). Policy readings, budgets, contracts, hires, studies and course or program proposals are not procedural; group a long list of them into one item ("Second reading of 44 district policies"). An empty list is right only when the agenda has nothing but procedure; say so in the overview.
- action: "vote" when the agenda asks for a decision (approve, adopt, award, deny); "hearing" for a public hearing; "report" for updates and presentations; otherwise "discussion".
- publicComment: true only if the agenda lists a public comment period or a public hearing where residents can speak.
- closedSession: true only if the agenda lists a closed or executive session.
- Use ONLY what the agenda says. Do not predict outcomes. Do not invent dollar amounts, names, or background the agenda does not give.
- Write for a resident, not an insider: spell out what an ordinance, resolution or permit would do when the agenda says.
- {RESIDENT_PRIVACY_RULES}

--- AGENDA ---
{agenda_text[:MAX_AGENDA_CHARS]}
--- END{" (agenda cut off here for length; say the preview covers the first part only)" if len(agenda_text) > MAX_AGENDA_CHARS else ""} ---"""


def parse_preview(raw: str) -> dict | None:
    """Validate Claude's reply into a preview, or None when it isn't one."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("overview"), str) \
            or not data["overview"].strip():
        return None
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict) or not isinstance(it.get("item"), str) or not it["item"].strip():
            continue
        action = str(it.get("action") or "").strip().lower()
        items.append({
            "item": it["item"].strip(),
            "why": str(it.get("why") or "").strip(),
            "action": action if action in ACTIONS else "discussion",
        })
    return {
        "overview": data["overview"].strip(),
        "items": items[:6],
        "publicComment": data.get("publicComment") is True,
        "closedSession": data.get("closedSession") is True,
    }


def _due(entry: dict, today: date) -> bool:
    try:
        d = date.fromisoformat(entry.get("date", ""))
    except ValueError:
        return False
    return today <= d <= today + timedelta(days=PREVIEW_DAYS)


def _write_one(cfg: InstanceConfig, entry: dict, key: str, text: str) -> dict | None:
    raw = call_claude(model=CLAUDE_MODEL_AGENDA, max_tokens=1500,
                      prompt=build_prompt(cfg, entry, text), meeting_id=key,
                      jurisdiction=entry["source"], purpose="agenda_preview")
    return parse_preview(raw)


def attach(cfg: InstanceConfig, payload: dict[str, list], generate: bool = True,
           now: datetime | None = None, cache_file: Path | None = None) -> dict:
    """Put each upcoming record's preview on it, generating the ones that
    are due when `generate` is set. Returns counts for the run log.

    A failed agenda fetch or Claude call costs that meeting its preview for
    this run only; the upcoming list itself is never held up by a preview.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    today = date.today()
    cache_file = cache_file or cache_path()
    cache = _load_cache(cache_file)
    stats = {"attached": 0, "generated": 0, "deferred": 0, "failed": 0}
    recheck = timedelta(hours=PREVIEW_RECHECK_HOURS)
    claude_down = False

    for entries in payload.values():
        for entry in entries:
            entry.pop("preview", None)          # a last-known-good record may carry an old one
            if not _due(entry, today):
                continue
            key = preview_key(entry)
            rec = cache.get(key) or {}
            checked = datetime.fromisoformat(rec["checked"]) if rec.get("checked") else None
            # A preview written under an older prompt is redone at the next
            # run instead of waiting out the recheck window.
            old_prompt = bool(rec.get("preview")) and rec.get("prompt") != PROMPT_VERSION
            if generate and not claude_down and (
                    checked is None or old_prompt or now - checked >= recheck):
                try:
                    text = agenda_text_for(cfg, entry)
                except Exception as e:          # one board's portal being down
                    logger.warning("preview: agenda fetch failed for %s %s: %s",
                                   entry["source"], entry["name"], e)
                    text = None
                if text:
                    digest = hashlib.sha1(f"{PROMPT_VERSION}\n{text}".encode("utf-8")).hexdigest()
                    rec.update(source=entry["source"], date=entry["date"], name=entry["name"])
                    if digest == rec.get("hash") or rec.get("generations", 0) >= MAX_GENERATIONS:
                        rec["checked"] = stamp
                        rec["prompt"] = PROMPT_VERSION      # settled under this prompt
                    elif stats["generated"] >= PREVIEW_MAX_PER_RUN:
                        stats["deferred"] += 1  # not marked checked: first in line next run
                    else:
                        try:
                            preview = _write_one(cfg, entry, key, text)
                        except Exception as e:
                            # An auth error or outage repeats for every meeting:
                            # stop asking for the rest of this run, charge nothing
                            # against this meeting, and try again next run.
                            logger.error("preview: Claude call failed (%s); no more "
                                         "previews this run", e)
                            claude_down = True
                        else:
                            rec["checked"] = stamp
                            rec["prompt"] = PROMPT_VERSION
                            rec["generations"] = rec.get("generations", 0) + 1
                            if preview:
                                preview["generated"] = stamp
                                rec.update(hash=digest, preview=preview)
                                stats["generated"] += 1
                            else:
                                stats["failed"] += 1
                    cache[key] = rec
            if rec.get("preview"):
                entry["preview"] = rec["preview"]
                stats["attached"] += 1

    if generate:
        # Meetings that have passed don't need their cache entry any more.
        cutoff = (today - timedelta(days=2)).isoformat()
        _save_cache({k: v for k, v in cache.items() if v.get("date", "9999") >= cutoff},
                    cache_file)
    print(f"[previews]  {stats['attached']} upcoming meeting(s) carry a preview; "
          f"{stats['generated']} written this run, {stats['deferred']} deferred, "
          f"{stats['failed']} failed")
    return stats
