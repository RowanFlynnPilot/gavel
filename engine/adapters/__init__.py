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
  rule_schedule    : project(...) nth-weekday recurring meetings
"""

from __future__ import annotations

from importlib import import_module

from ..errors import UnknownAdapterError

_ADAPTER_MODULES = {
    "youtube": "engine.adapters.youtube",
    "civicclerk": "engine.adapters.civicclerk",
    "boardbook": "engine.adapters.boardbook",
    "agendacenter": "engine.adapters.agendacenter",
    "municode": "engine.adapters.municode",
    "rule_schedule": "engine.adapters.rule_schedule",
}


def get_adapter(name: str):
    if name not in _ADAPTER_MODULES:
        raise UnknownAdapterError(
            f"unknown adapter '{name}' (known: {sorted(_ADAPTER_MODULES)})")
    return import_module(_ADAPTER_MODULES[name])
