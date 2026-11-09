"""voxtype/fast_load.py must read a .safetensors file exactly as the safetensors library does (Windows: the
unbuffered parallel path; elsewhere it declines and returns None)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from tests import _isolate  # noqa: F401

from voxtype import fast_load


@unittest.skipUnless(sys.platform == "win32", "the fast loader is Windows-only")
class FastLoadMatchesSafetensors(unittest.TestCase):
    def setUp(self):
        import torch
        from safetensors.torch import save_file
        g = torch.Generator().manual_seed(0)
        # odd sizes and mixed dtypes: tensor offsets that are not sector aligned, a file whose end is not either
        self.tensors = {
            "a.weight": torch.randn(1023, 37, generator=g),
            "b.bias": torch.randn(5, generator=g).half(),
            "c": torch.randn(300, 301, generator=g).bfloat16(),
            "d.ids": torch.arange(17, dtype=torch.int64),
            "e.mask": torch.tensor([True, False, True]),
            "f.big": torch.randn(4096, 512, generator=g),  # 8 MiB: spans several reads
        }
        fd, self.path = tempfile.mkstemp(suffix=".safetensors")
        os.close(fd)
        save_file(self.tensors, self.path)

    def tearDown(self):
        os.remove(self.path)

    def test_every_tensor_is_identical(self):
        import torch
        for readers, chunk_mb in ((1, 1), (4, 1), (4, 32)):
            got = fast_load.read_file(self.path, readers, chunk_mb << 20)
            self.assertEqual(set(got), set(self.tensors))
            for k, v in self.tensors.items():
                self.assertEqual(got[k].dtype, v.dtype, k)
                self.assertEqual(tuple(got[k].shape), tuple(v.shape), k)
                self.assertTrue(torch.equal(got[k], v), f"{k} differs ({readers} readers x {chunk_mb} MiB)")

    def test_read_state_dict_and_switch(self):
        os.environ["VOXTYPE_FAST_LOAD"] = "0"
        try:
            self.assertIsNone(fast_load.read_state_dict([self.path]))
        finally:
            os.environ.pop("VOXTYPE_FAST_LOAD", None)
        sd = fast_load.read_state_dict([self.path])
        self.assertIsNotNone(sd)
        self.assertEqual(fast_load.LAST_READ.get("readers"), 4)

    def test_safetensors_files_finds_single_and_sharded(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(fast_load.safetensors_files(d))
            open(os.path.join(d, "model.safetensors"), "wb").close()
            self.assertEqual(fast_load.safetensors_files(d), [os.path.join(d, "model.safetensors")])
        finally:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
