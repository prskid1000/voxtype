"""OLED guard settings + helpers.

Covers the settings fields, dotted/flat patch round-trip, and the
refresh-rate helper's headless fallback. The OledGuard widget itself
needs a QApplication, so we don't instantiate it here — only the pure
pieces are tested (same convention as the rest of the suite)."""
from __future__ import annotations

import unittest

from tests import _isolate  # noqa: F401


class OledSettings(unittest.TestCase):
    def setUp(self) -> None:
        _isolate.fresh_data_dir()

    def test_defaults(self):
        from voxtype.types import AppSettings
        s = AppSettings()
        self.assertFalse(s.oled_guard_enabled)
        self.assertEqual(s.oled_flashes_per_sec, 2)
        self.assertEqual(s.oled_flash_opacity, 1.0)

    def test_patch_and_persist(self):
        from voxtype import config
        config.patch("oled_guard_enabled", True)
        config.patch("oled_flashes_per_sec", 4)
        config.patch("oled_flash_opacity", 0.5)
        s = config.reload()
        self.assertTrue(s.oled_guard_enabled)
        self.assertEqual(s.oled_flashes_per_sec, 4)
        self.assertEqual(s.oled_flash_opacity, 0.5)

    def test_from_json_round_trip(self):
        from voxtype.types import AppSettings
        s = AppSettings(oled_guard_enabled=True, oled_flashes_per_sec=6)
        s2 = AppSettings.from_json(s.to_json())
        self.assertTrue(s2.oled_guard_enabled)
        self.assertEqual(s2.oled_flashes_per_sec, 6)

    def test_legacy_json_without_oled_keys(self):
        """Settings files written before this feature must still load."""
        from voxtype.types import AppSettings
        s = AppSettings.from_json({"stt_model_path": "openai/whisper-tiny"})
        self.assertFalse(s.oled_guard_enabled)
        self.assertEqual(s.oled_flashes_per_sec, 2)


class RefreshRateHelper(unittest.TestCase):
    def test_fallback_is_sane(self):
        """primary_refresh_rate() must return a usable Hz even with no
        QApplication / no screen (headless CI)."""
        from voxtype.oled_guard import primary_refresh_rate
        rr = primary_refresh_rate()
        self.assertGreaterEqual(rr, 1.0)


if __name__ == "__main__":
    unittest.main()
