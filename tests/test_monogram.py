"""A jurisdiction with no avatar image gets a monogram. No network.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import unittest
from pathlib import Path

from PIL import ImageFont

from engine import digest
from engine.config import InstanceConfig, derive_monogram
from engine.errors import ConfigError
from helpers import raw_instance

FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "BebasNeue-Regular.ttf"


class MonogramTest(unittest.TestCase):
    def test_derived_from_the_label(self):
        for label, want in (("Village of Rib Mountain", "RM"), ("Mosinee", "M"), ("Town of Weston", "W"),
                            ("Mosinee Schools", "MS"), ("county of Riverton", "R"), ("", "?")):
            self.assertEqual(derive_monogram(label), want, label)

    def test_config_uses_the_given_monogram_or_derives_one(self):
        raw = raw_instance()
        raw["jurisdictions"][0]["monogram"] = "RC"
        raw["jurisdictions"][1]["filter_label"] = "Riverton Schools"
        cfg = InstanceConfig(raw, Path("instance.json"))
        self.assertEqual(cfg.jurisdiction("county").monogram, "RC")
        self.assertEqual(cfg.jurisdiction("schools").monogram, "RS")
        self.assertEqual(cfg.jurisdiction("village").monogram, "E", "Village of Elm")

    def test_a_long_monogram_is_refused(self):
        raw = raw_instance()
        raw["jurisdictions"][0]["monogram"] = "RIVR"
        with self.assertRaises(ConfigError):
            InstanceConfig(raw, Path("instance.json"))

    def test_digest_draws_the_monogram_on_the_accent_when_there_is_no_image(self):
        accent = (122, 59, 30)
        font = ImageFont.truetype(str(FONT), 17 * digest.SCALE)
        plain = digest._circle_avatar(None, 38, accent)
        marked = digest._circle_avatar(None, 38, accent, "MO", font)
        centre = plain.size[0] // 2
        self.assertEqual(plain.getpixel((centre, centre))[:3], accent)
        self.assertEqual(plain.getpixel((0, 0))[3], 0, "outside the circle is transparent")
        whites = sum(1 for p in marked.getdata() if p[:3] == (255, 255, 255) and p[3] == 255)
        self.assertGreater(whites, 50, "the initials are drawn in white")
        # Without an accent (an avatar file that failed to load, as before): neutral grey, no text.
        grey = digest._circle_avatar(Path("missing.jpg"), 38)
        self.assertEqual(grey.getpixel((centre, centre))[:3], (230, 226, 218))


if __name__ == "__main__":
    unittest.main()
