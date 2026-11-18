"""Shared fixtures for the unit tests: a small made-up instance. No network."""

from __future__ import annotations

from pathlib import Path

from engine.config import InstanceConfig


def raw_instance(**instance_overrides) -> dict:
    """A valid instance.json as a dict: one jurisdiction per upgrade path."""
    return {
        "instance": {"name": "Riverton Meetings", "newsroom": "Riverton Ledger",
                     "newsroom_url": "https://ledger.example", "region": "Riverton",
                     "timezone": "America/Chicago", "max_meetings": 50,
                     **instance_overrides},
        "theme": {},
        "jurisdictions": [
            {"key": "county", "name": "Riverton County", "short": "COUNTY", "accent": "#111111",
             "video": {"adapter": "youtube", "channel_url": "https://youtube.example/@county"},
             "agendas": {"adapter": "rule_schedule", "rules": [
                 {"name": "County Board", "weekday": 1, "nth": 4, "time": "7:00 PM"}]},
             "expected_lag_days": 3},
            {"key": "schools", "name": "Riverton Schools", "short": "SCHOOL", "accent": "#222222",
             "video": {"adapter": "youtube", "channel_url": "https://youtube.example/@schools"},
             "agendas": {"adapter": "boardbook", "base": "https://bb.example", "org_id": 7,
                         "label": "Riverton School Board",
                         "fallback_rules": [{"name": "Regular School Board Meeting",
                                             "weekday": 2, "nth": 3, "time": "6:30 PM"}]},
             "expected_lag_days": 21},
            {"key": "village", "name": "Village of Elm", "short": "VILLAGE", "accent": "#333333",
             "video": None, "audio_url": "https://audio.example/elm",
             "agendas": {"adapter": "municode", "base": "https://elm.example",
                         "ada_url": "https://ada.example/{guid}", "blob_base": "https://blob.example",
                         "id_prefix": "el"}},
            {"key": "city", "name": "City of Oak", "short": "CITY", "accent": "#444444",
             "video": None,
             "agendas": {"adapter": "civicclerk", "api_base": "https://oak.api.example/v1",
                         "portal": "https://oak.portal.example"}},
            {"key": "town", "name": "Town of Ash", "short": "TOWN", "accent": "#555555",
             "video": None, "agendas": {"adapter": "primegov", "client": "ash"}},
        ],
    }


def make_config(**instance_overrides) -> InstanceConfig:
    return InstanceConfig(raw_instance(**instance_overrides), Path("instance.json"))


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code, self._data, self.text = status_code, data, text

    def json(self):
        if self._data is None:
            raise ValueError("not JSON")
        return self._data
