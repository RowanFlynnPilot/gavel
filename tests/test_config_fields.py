"""instance.json fields added with the watchdog/overdue/scrub/pages ports:
defaults, validation, and the reference instance's values. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from pathlib import Path

from engine.config import DEFAULT_EXPECTED_LAG_DAYS, InstanceConfig, load_instance
from engine.errors import ConfigError
from helpers import make_config, raw_instance


def _load(mutate) -> InstanceConfig:
    raw = raw_instance()
    mutate(raw)
    return InstanceConfig(raw, Path("instance.json"))


class DefaultsTest(unittest.TestCase):
    def test_optional_fields_default(self):
        cfg = make_config()
        self.assertEqual(cfg.ics_uid_domain, "gavel")
        self.assertEqual(cfg.corrections_email, "")
        self.assertEqual(cfg.place_names, [])
        self.assertEqual(cfg.by_key["village"].expected_lag_days, DEFAULT_EXPECTED_LAG_DAYS)
        self.assertEqual(DEFAULT_EXPECTED_LAG_DAYS, 7)
        self.assertEqual(cfg.by_key["schools"].expected_lag_days, 21)


class ValidationTest(unittest.TestCase):
    def _rejects(self, mutate, fragment: str):
        with self.assertRaises(ConfigError) as ctx:
            _load(mutate)
        self.assertIn(fragment, str(ctx.exception))

    def test_expected_lag_days_must_be_a_positive_integer(self):
        for bad in (0, -3, "5", 2.5, True, None):
            self._rejects(lambda r, bad=bad: r["jurisdictions"][0].update(expected_lag_days=bad),
                          "expected_lag_days")

    def test_ics_uid_domain_shape(self):
        for bad in ("", "has space", "a@b", 7):
            self._rejects(lambda r, bad=bad: r["instance"].update(ics_uid_domain=bad), "ics_uid_domain")
        self.assertEqual(make_config(ics_uid_domain="riverton-meetings").ics_uid_domain, "riverton-meetings")

    def test_corrections_email_must_be_an_address(self):
        for bad in ("editor", "editor@", "a b@c.org", 5):
            self._rejects(lambda r, bad=bad: r["instance"].update(corrections_email=bad), "corrections_email")
        self.assertEqual(make_config(corrections_email="editor@ledger.example").corrections_email,
                         "editor@ledger.example")

    def test_place_names_must_be_a_list_of_names(self):
        for bad in ("Riverton", ["Riverton", ""], ["Riverton", 3]):
            self._rejects(lambda r, bad=bad: r.update(place_names=bad), "place_names")

    def test_rules_are_checked_at_load(self):
        def rules(r):
            return r["jurisdictions"][1]["agendas"]["fallback_rules"]
        self._rejects(lambda r: rules(r)[0].update(weekday=7), "weekday")
        self._rejects(lambda r: rules(r)[0].update(nth=0), "nth")
        self._rejects(lambda r: rules(r)[0].pop("time"), "time")
        self._rejects(lambda r: r["jurisdictions"][0]["agendas"].update(rules=[{"name": "Board"}]), "time")
        self._rejects(lambda r: r["jurisdictions"][1]["agendas"].update(fallback_rules="monthly"), "fallback_rules")


class ReferenceInstanceTest(unittest.TestCase):
    def test_wpr_values(self):
        cfg = load_instance(Path(__file__).parent.parent / "instance.json")
        self.assertEqual({j.key: j.expected_lag_days for j in cfg.jurisdictions},
                         {"marathon": 3, "wausau": 3, "weston": 3, "school_board": 21,
                          "kronenwetter": 5, "dc_everest": 7})
        self.assertEqual(cfg.ics_uid_domain, "marathon-meetings")
        self.assertTrue(cfg.corrections_email)
        self.assertIn("Rib Mountain", cfg.place_names)


if __name__ == "__main__":
    unittest.main()
