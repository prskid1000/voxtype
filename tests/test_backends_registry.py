"""Backend factory should return the generic backend with no
indirection layer."""
from __future__ import annotations

import unittest

from tests import _isolate  # noqa: F401


class Factory(unittest.TestCase):
    def test_stt_factory(self):
        from voxtype.backends import get_stt_backend
        from voxtype.backends.generic_stt import GenericSTTBackend
        self.assertIsInstance(get_stt_backend(), GenericSTTBackend)

    def test_tts_factory(self):
        from voxtype.backends import get_tts_backend
        from voxtype.backends.generic_tts import GenericTTSBackend
        self.assertIsInstance(get_tts_backend(), GenericTTSBackend)


class GenericBackendShape(unittest.TestCase):
    """Pre-load, the generic backends advertise no family / no options."""

    def test_stt_pre_load(self):
        from voxtype.backends import get_stt_backend
        be = get_stt_backend()
        self.assertEqual(be.detected_family(), "")
        self.assertEqual(be.runtime_options(), [])
        self.assertEqual(be.name, "generic")
        self.assertFalse(be.supports("task_translate"))

    def test_tts_pre_load(self):
        from voxtype.backends import get_tts_backend
        be = get_tts_backend()
        self.assertEqual(be.detected_family(), "")
        self.assertEqual(be.runtime_options(), [])
        self.assertEqual(be.voices(), [])


class SettingsDefaults(unittest.TestCase):
    """AppSettings now ships only universal fields + opts bags — no
    legacy stt_backend / tts_backend / stt_task / tts_speaker."""

    def test_no_legacy_fields(self):
        from voxtype.types import AppSettings
        s = AppSettings()
        for legacy in ("stt_backend", "tts_backend", "stt_task",
                        "stt_num_beams", "stt_initial_prompt",
                        "tts_speaker", "tts_length_scale",
                        "stt_chunk_length_s", "stt_stride_length_s"):
            self.assertFalse(hasattr(s, legacy),
                              f"legacy field {legacy!r} should be gone")

    def test_universal_fields_present(self):
        from voxtype.types import AppSettings
        s = AppSettings()
        for needed in ("stt_model_path", "stt_device", "stt_language",
                        "stt_dtype", "stt_attn_impl",
                        "stt_opts",
                        "tts_model_path", "tts_device", "tts_voice",
                        "tts_speed", "tts_attn_impl", "tts_seed",
                        "tts_opts"):
            self.assertTrue(hasattr(s, needed),
                             f"universal field {needed!r} missing")

    def test_attn_impl_default(self):
        from voxtype.types import AppSettings
        s = AppSettings()
        self.assertEqual(s.stt_attn_impl, "auto")
        self.assertEqual(s.tts_attn_impl, "auto")
        self.assertEqual(s.tts_seed, -1)

    def test_load_config_carries_attn_impl(self):
        from voxtype.backends.stt_base import LoadConfig
        from voxtype.backends.tts_base import TTSLoadConfig
        c = LoadConfig(model_id="x", device="cpu", dtype="auto",
                        warmup=False, torch_compile=False,
                        attn_impl="flash_attention_2")
        self.assertEqual(c.attn_impl, "flash_attention_2")
        t = TTSLoadConfig(model_id="x", device="cpu",
                           warmup=False, torch_compile=False,
                           attn_impl="sdpa")
        self.assertEqual(t.attn_impl, "sdpa")

    def test_opts_bags_default_empty(self):
        from voxtype.types import AppSettings
        s = AppSettings()
        self.assertEqual(s.stt_opts, {})
        self.assertEqual(s.tts_opts, {})


if __name__ == "__main__":
    unittest.main()
