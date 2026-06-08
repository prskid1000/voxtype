"""Voice-activation: start-word matching + settings round-trip."""
from __future__ import annotations

import unittest

from tests import _isolate  # noqa: F401

from voxtype.wake_listener import matches_start_word
from voxtype.types import AppSettings


class StartWordMatch(unittest.TestCase):
    def test_prefix_match(self):
        self.assertTrue(matches_start_word("computer take a note", "computer"))

    def test_exact_match(self):
        self.assertTrue(matches_start_word("Computer.", "computer"))

    def test_case_and_punctuation_insensitive(self):
        self.assertTrue(matches_start_word("  COMPUTER, hello", "computer"))

    def test_no_false_prefix_on_partial_word(self):
        # "computerized" must NOT trigger on the word "computer".
        self.assertFalse(matches_start_word("computerized systems", "computer"))

    def test_not_at_start_is_rejected_by_default(self):
        self.assertFalse(matches_start_word("ok computer play", "computer"))

    def test_contains_mode(self):
        self.assertTrue(
            matches_start_word("ok computer play", "computer", contains=True))

    def test_contains_requires_word_boundary(self):
        self.assertFalse(
            matches_start_word("supercomputers", "computer", contains=True))

    def test_multiple_comma_separated_phrases(self):
        words = "computer, hey vox"
        self.assertTrue(matches_start_word("hey vox what's up", words))
        self.assertTrue(matches_start_word("computer stop", words))

    def test_multiword_phrase(self):
        self.assertTrue(matches_start_word("hey vox start", "hey vox"))
        self.assertFalse(matches_start_word("hey there", "hey vox"))

    def test_empty_inputs(self):
        self.assertFalse(matches_start_word("", "computer"))
        self.assertFalse(matches_start_word("computer", ""))
        self.assertFalse(matches_start_word("computer", "   ,  "))


class VoiceSettingsMigration(unittest.TestCase):
    def test_defaults(self):
        s = AppSettings()
        self.assertFalse(s.voice_activation_enabled)
        self.assertEqual(s.voice_start_words, "computer")
        self.assertFalse(s.voice_match_contains)
        self.assertAlmostEqual(s.voice_max_phrase_sec, 2.5)

    def test_missing_keys_default(self):
        # A legacy settings.json with none of the voice fields must load.
        s = AppSettings.from_json({"stt_model_path": "openai/whisper-tiny"})
        self.assertFalse(s.voice_activation_enabled)
        self.assertEqual(s.voice_start_words, "computer")

    def test_round_trip(self):
        s = AppSettings()
        s.voice_activation_enabled = True
        s.voice_start_words = "jarvis, hey vox"
        s.voice_match_contains = True
        s.voice_max_phrase_sec = 3.0
        s2 = AppSettings.from_json(s.to_json())
        self.assertTrue(s2.voice_activation_enabled)
        self.assertEqual(s2.voice_start_words, "jarvis, hey vox")
        self.assertTrue(s2.voice_match_contains)
        self.assertAlmostEqual(s2.voice_max_phrase_sec, 3.0)


if __name__ == "__main__":
    unittest.main()
