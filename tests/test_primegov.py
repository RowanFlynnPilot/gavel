"""PrimeGov adapter against saved portal responses (tests/fixtures/primegov/: Oviedo's Oct. 19, 2026 City Council
HTML agenda, and upcoming meetings from Oviedo's and Santa Ana's portals). No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from engine.adapters import primegov

FIXTURES = Path(__file__).parent / "fixtures" / "primegov"
OVIEDO = {"client": "cityofoviedo"}


class _Response:
    def __init__(self, status_code=200, text="", data=None):
        self.status_code, self.text, self._data = status_code, text, data

    def json(self):
        return self._data


class PrimeGovTest(unittest.TestCase):
    def test_agenda_text_is_the_agenda_without_portal_or_attachments(self):
        page = (FIXTURES / "oviedo_city_council_2026-10-19.html").read_text(encoding="utf-8")
        url = "https://cityofoviedo.primegov.com/Portal/Meeting?compiledMeetingDocumentFileId=8847"
        with mock.patch("engine.adapters.primegov.requests.get", return_value=_Response(text=page)) as get:
            text = primegov.fetch_agenda_text(OVIEDO, url)
        get.assert_called_once()
        self.assertIn("CITY OF OVIEDO FLORIDA", text)
        self.assertIn("Resolution No. 4769-26, Award of Invitation to Bid", text)
        self.assertIn("Ordinance No. 1776, Bicycle and Micromobility Device Operation", text)
        self.assertNotIn("Subscribe", text, "the portal around the agenda is left out")
        self.assertNotIn("Res4769-26 Memo Atch1", text, "attachment links are left out")

    def test_upcoming_skips_spanish_copies(self):
        meetings = json.loads((FIXTURES / "upcoming.json").read_text(encoding="utf-8"))
        with mock.patch("engine.adapters.primegov.requests.get", return_value=_Response(data=meetings)):
            rows = primegov.fetch_upcoming({"client": "santa-ana"}, "santa_ana", 3650)
        self.assertEqual(len(rows), len([m for m in meetings if "Español" not in m["title"]]))
        self.assertTrue(all("Español" not in r["name"] for r in rows))
        self.assertEqual(set(rows[0]), {"date", "time", "name", "url", "source"})
        council = next(r for r in rows if r["name"].startswith("Regular City Council"))
        self.assertIn("compiledMeetingDocumentFileId=", council["url"], "a meeting with an HTML agenda links to it")

    def test_not_a_primegov_agenda_url(self):
        with mock.patch("engine.adapters.primegov.requests.get") as get:
            self.assertIsNone(primegov.fetch_agenda_text(OVIEDO, "https://example.com/agenda.pdf"))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
