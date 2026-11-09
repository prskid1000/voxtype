"""One worker per modality, except both on CUDA (one shared CUDA context)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest import mock

from tests import _isolate  # noqa: F401

from voxtype import config, engine_host


class HostRouting(unittest.TestCase):
    def setUp(self):
        engine_host._HOSTS.clear()

    def tearDown(self):
        engine_host._HOSTS.clear()

    def _with(self, stt, tts):
        return mock.patch.object(config, "load", return_value=NS(stt_device=stt, tts_device=tts))

    def test_cpu_stt_and_gpu_tts_get_separate_workers(self):
        with self._with("cpu", "cuda"):
            self.assertIsNot(engine_host.get_host("stt"), engine_host.get_host("tts"))

    def test_both_on_cuda_share_one_worker(self):
        with self._with("cuda", "cuda"):
            self.assertIs(engine_host.get_host("stt"), engine_host.get_host("tts"))

    def test_same_modality_same_host_and_all_hosts(self):
        with self._with("cpu", "cpu"):
            self.assertIs(engine_host.get_host("stt"), engine_host.get_host("stt"))
            engine_host.get_host("tts")
            self.assertEqual(len(engine_host.all_hosts()), 2)


if __name__ == "__main__":
    unittest.main()
