"""Adapter registry.

Adapters are looked up by the name given in instance.json. An unknown name
fails loud at config-validation time (engine.config) AND here — both layers
raise, so a typo can never silently skip a jurisdiction.

Adapter responsibilities (duck-typed per module):
  video adapters   : list_videos(video_cfg, jurisdiction_key, dateafter="")
  agenda adapters  : some combination of
                     - list_meetings(...)      meeting-creating (boardbook, municode)
                     - fetch_agenda_text(...)  summarization fallback text
                     - fetch_upcoming(...)     posted future meetings
  portal adapters  : (config.PORTAL_ADAPTERS) create meetings from the portal
                     when the jurisdiction has no video channel:
                     - list_recent(agendas_cfg, since: date) -> [{id, name,
                       date "YYYY-MM-DD", url}], past meetings with a posted
                       agenda, newest first; id is digits only; url is the
                       meeting's public page and the doc_url the two fetch
                       functions below accept. Raises on a failed request.
                     - fetch_agenda_text(agendas_cfg, doc_url) -> str | None
                     - fetch_upcoming(agendas_cfg, key, days_ahead)
                     - fetch_minutes_text(agendas_cfg, doc_url) -> str | None
                       (config.MINUTES_ADAPTERS; None until minutes are posted)
  rule_schedule    : project(...) nth-weekday recurring meetings
"""

from __future__ import annotations

from importlib import import_module

from ..errors import UnknownAdapterError

_ADAPTER_MODULES = {
    "youtube": "engine.adapters.youtube",
    "civicclerk": "engine.adapters.civicclerk",
    "legistar": "engine.adapters.legistar",
    "primegov": "engine.adapters.primegov",
    "boardbook": "engine.adapters.boardbook",
    "agendacenter": "engine.adapters.agendacenter",
    "municode": "engine.adapters.municode",
    "rule_schedule": "engine.adapters.rule_schedule",
    "diligent": "engine.adapters.diligent",
    "iqm2": "engine.adapters.iqm2",
    "revize": "engine.adapters.revize",
    "townweb": "engine.adapters.townweb",
    "heygov": "engine.adapters.heygov",
}


def for_jurisdiction(jur):
    """The agendas adapter to use for a jurisdiction in the pipeline: its
    module, or, for one whose site is fetched from a residential connection
    (agendas.fetch_from), a reader of the documents that fetcher committed.
    engine.fetch_portals itself always uses the real module."""
    from ..portal_cache import CachedPortal, is_residential
    if is_residential(jur):
        return CachedPortal(jur.key)
    return get_adapter(jur.agendas_adapter)


def get_adapter(name: str):
    if name not in _ADAPTER_MODULES:
        raise UnknownAdapterError(
            f"unknown adapter '{name}' (known: {sorted(_ADAPTER_MODULES)})")
    return import_module(_ADAPTER_MODULES[name])
