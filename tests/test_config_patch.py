"""config.patch should support both flat keys and dotted writes into
the per-family opts bags (stt_opts.*, tts_opts.*, hotkey.*)."""
from __future__ import annotations

import unittest

from tests import _isolate  # noqa: F401


class ConfigPatch(unittest.TestCase):
    def setUp(self) -> None:
        # Fresh data dir per test so settings.json never carries over.
        _isolate.fresh_data_dir()

    def test_flat_key(self):
        from voxtype import config
        config.patch("stt_model_path", "openai/whisper-tiny")
        self.assertEqual(config.load().stt_model_path, "openai/whisper-tiny")

    def test_sound_duration_sec_patch(self):
        from voxtype import config
        config.patch("sound_duration_sec", 0.25)
        self.assertEqual(config.load().sound_duration_sec, 0.25)

    def test_individual_sound_toggles(self):
        from voxtype import config
        config.patch("sound_start_enabled", False)
        config.patch("sound_stop_enabled", False)
        config.patch("sound_done_enabled", False)
        self.assertFalse(config.load().sound_start_enabled)
        self.assertFalse(config.load().sound_stop_enabled)
        self.assertFalse(config.load().sound_done_enabled)

    def test_stt_opts_dotted_write(self):
        from voxtype import config
        config.patch("stt_opts.task", "translate")
        config.patch("stt_opts.num_beams", 5)
        config.patch("stt_opts.initial_prompt", "Hello")
        s = config.load()
        self.assertEqual(s.stt_opts["task"], "translate")
        self.assertEqual(s.stt_opts["num_beams"], 5)
        self.assertEqual(s.stt_opts["initial_prompt"], "Hello")

    def test_tts_opts_dotted_write(self):
        from voxtype import config
        config.patch("tts_opts.style", "Calm warm voice")
        config.patch("tts_opts.temperature", 0.7)
        s = config.load()
        self.assertEqual(s.tts_opts["style"], "Calm warm voice")
        self.assertEqual(s.tts_opts["temperature"], 0.7)

    def test_patch_persists_to_disk(self):
        """A subsequent reload() must see the patched value."""
        from voxtype import config
        config.patch("stt_opts.task", "translate")
        # Force a re-read from disk.
        s = config.reload()
        self.assertEqual(s.stt_opts["task"], "translate")

    def test_hotkey_subfield_still_works(self):
        from voxtype import config
        config.patch("hotkey.key1", "alt")
        self.assertEqual(config.load().hotkey.key1, "alt")

    def test_unknown_top_level_path_ignored(self):
        from voxtype import config
        before = config.load().stt_model_path
        config.patch("nonexistent_field", "value")  # must not raise
        self.assertEqual(config.load().stt_model_path, before)

    def test_nested_dotted_write(self):
        """`stt_opts.a.b` should build nested dicts (defensive — not
        used today, but the patch logic supports arbitrary depth)."""
        from voxtype import config
        config.patch("stt_opts.advanced.foo", "bar")
        s = config.load()
        self.assertEqual(s.stt_opts.get("advanced", {}).get("foo"), "bar")


if __name__ == "__main__":
    unittest.main()
