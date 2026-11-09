"""Fast, cache-free reading of .safetensors weights (Windows).

transformers reads a checkpoint through a memory map, so its pages land in Windows' file cache and stay
there (as "standby" memory) after the model is unloaded - and the read itself is one thread at the
drive's single-stream speed. This reads the file with several workers that each own an unbuffered handle
(FILE_FLAG_NO_BUFFERING) and read at explicit offsets straight into one aligned buffer; the tensors are
views into that buffer, so there is no second copy and nothing is left in the file cache: unloading the
model frees all of its RAM. Same approach as telecode's llama.cpp loader (patch 0007).

`read_state_dict(paths)` returns {name: tensor} or None when it cannot be used (other platforms, a file it
cannot open unbuffered, an unknown dtype); callers then load the ordinary way.

Environment: VOXTYPE_FAST_LOAD=0 disables it, VOXTYPE_FAST_LOAD_READERS=N (default 4), VOXTYPE_FAST_LOAD_CHUNK_MB=N (default 32).
"""
from __future__ import annotations

import json
import logging
import os
import struct
import sys
import threading
import time
from typing import Optional

log = logging.getLogger("voxtype.fast_load")

_DTYPES = {
    "F64": "float64", "F32": "float32", "F16": "float16", "BF16": "bfloat16",
    "I64": "int64", "I32": "int32", "I16": "int16", "I8": "int8", "U8": "uint8", "BOOL": "bool",
    "F8_E4M3": "float8_e4m3fn", "F8_E5M2": "float8_e5m2",
}


LAST_READ: dict = {}  # the last read_state_dict's numbers (the worker reports them with the load time)


def enabled() -> bool:
    return sys.platform == "win32" and os.environ.get("VOXTYPE_FAST_LOAD", "1") != "0"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def safetensors_files(model_dir: str) -> Optional[list[str]]:
    """The checkpoint files of a model folder: model.safetensors, or every shard of its index; None if neither."""
    single = os.path.join(model_dir, "model.safetensors")
    if os.path.isfile(single):
        return [single]
    index = os.path.join(model_dir, "model.safetensors.index.json")
    if os.path.isfile(index):
        with open(index, encoding="utf-8") as f:
            shards = sorted(set(json.load(f).get("weight_map", {}).values()))
        files = [os.path.join(model_dir, s) for s in shards]
        return files if files and all(os.path.isfile(p) for p in files) else None
    return None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _INVALID = wintypes.HANDLE(-1).value

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


_ALIGN = 4096  # sector size the unbuffered reads are aligned to (covers 512e and 4Kn drives)


def _read_unbuffered(path: str, begin: int, end: int, dst_addr: int, readers: int, chunk: int) -> None:
    """Read file bytes [begin, end) (both aligned to _ALIGN; end may pass EOF) to dst_addr (aligned) in parallel."""
    n_chunks = (end - begin + chunk - 1) // chunk
    nxt = [0]
    lock = threading.Lock()
    errors: list[str] = []

    def worker() -> None:
        h = _k32.CreateFileW(path, 0x80000000, 7, None, 3, 0x20000000, None)  # GENERIC_READ, share all, OPEN_EXISTING, NO_BUFFERING
        if h in (None, _INVALID):
            errors.append(f"cannot open unbuffered (error {ctypes.get_last_error()})")
            return
        try:
            got = wintypes.DWORD()
            while not errors:
                with lock:
                    i = nxt[0]
                    nxt[0] += 1
                if i >= n_chunks:
                    return
                off = begin + i * chunk
                size = min(chunk, end - off)
                ov = _OVERLAPPED()
                ov.Offset, ov.OffsetHigh = off & 0xFFFFFFFF, off >> 32
                if not _k32.ReadFile(h, ctypes.c_void_p(dst_addr + (off - begin)), size, ctypes.byref(got), ctypes.byref(ov)):
                    code = ctypes.get_last_error()
                    if code != 38:  # ERROR_HANDLE_EOF: the aligned tail past the end of the file
                        errors.append(f"read error {code} at {off}")
                    return
        finally:
            _k32.CloseHandle(h)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(readers, n_chunks))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise OSError(errors[0])


def read_file(path: str, readers: int, chunk: int):
    """{name: tensor} of one .safetensors file; the tensors are views into one buffer read unbuffered."""
    import torch
    with open(path, "rb") as f:
        (hlen,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(hlen))
    header.pop("__metadata__", None)
    data_start = 8 + hlen
    file_size = os.path.getsize(path)
    begin = data_start & ~(_ALIGN - 1)
    end = (file_size + _ALIGN - 1) & ~(_ALIGN - 1)
    raw = torch.empty(end - begin + _ALIGN, dtype=torch.uint8)
    pad = (-raw.data_ptr()) % _ALIGN
    buf = raw[pad: pad + (end - begin)]
    _read_unbuffered(path, begin, end, buf.data_ptr(), readers, chunk)
    base = data_start - begin
    out = {}
    for name, info in header.items():
        dt = _DTYPES.get(info["dtype"])
        if dt is None or not hasattr(torch, dt):
            raise ValueError(f"dtype {info['dtype']} of {name} is not supported")
        a, b = info["data_offsets"]
        piece = buf[base + a: base + b]
        dtype = getattr(torch, dt)
        try:
            t = piece.view(dtype)
        except RuntimeError:  # an odd offset for the element size: copy this one
            t = piece.clone().view(dtype)
        out[name] = t.reshape(info["shape"])
    return out


def read_state_dict(paths: list[str]):
    """{name: tensor} of every file, or None when the fast path cannot be used here."""
    if not enabled():
        return None
    readers = _env_int("VOXTYPE_FAST_LOAD_READERS", 4)
    chunk = _env_int("VOXTYPE_FAST_LOAD_CHUNK_MB", 32) << 20
    t0 = time.perf_counter()
    sd: dict = {}
    total = 0
    try:
        for p in paths:
            sd.update(read_file(p, readers, chunk))
            total += os.path.getsize(p)
    except Exception as exc:  # noqa: BLE001 — the ordinary loader takes over
        log.info("fast load unavailable for %s: %s", paths[0] if paths else "?", exc)
        return None
    dt = time.perf_counter() - t0
    LAST_READ.clear()
    LAST_READ.update(gib=round(total / 2**30, 2), seconds=round(dt, 2), mib_s=round(total / 2**20 / max(dt, 1e-6)),
                     readers=readers, chunk_mb=chunk >> 20)
    log.info("fast load: %.2f GiB in %.2f s (%.0f MiB/s, %d readers x %d MiB, unbuffered)",
             total / 2**30, dt, total / 2**20 / max(dt, 1e-6), readers, chunk >> 20)
    return sd


def _model_dir(model_id: str) -> Optional[str]:
    if os.path.isdir(model_id):
        return model_id
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(model_id, local_files_only=True)  # the cached snapshot, never the network
    except Exception:  # noqa: BLE001 — not cached yet: the ordinary loader downloads it
        return None


def load_hf_model(loader, model_id: str, kw: dict, device: str, extra: dict):
    """`loader` (a bound `SomeModel.from_pretrained`) fed with weights from read_state_dict, or None to let the
    caller load the ordinary way. Config and generation config come from the same cached snapshot."""
    if not enabled() or extra or not hasattr(loader, "__self__"):
        return None  # subfolders / variants / vocoders with extra arguments: keep the ordinary path
    model_dir = _model_dir(model_id)
    files = safetensors_files(model_dir) if model_dir else None
    if not files:
        return None
    cls = loader.__self__
    try:
        config = cls.config_class.from_pretrained(model_dir)
    except Exception as exc:  # noqa: BLE001
        log.info("fast load: no config for %s (%s)", model_id, exc)
        return None
    sd = read_state_dict(files)
    if sd is None:
        return None
    t0 = time.perf_counter()
    args = {k: v for k, v in kw.items() if k != "low_cpu_mem_usage"}
    try:
        model = None
        if device == "cuda":
            try:
                model = loader(None, config=config, state_dict=sd, device_map="cuda", **args)
            except Exception as exc:  # noqa: BLE001
                log.info("fast load: device_map failed (%s); building on the CPU", exc)
        if model is None:
            model = loader(None, config=config, state_dict=sd, **args).to(device)
    except Exception as exc:  # noqa: BLE001
        log.warning("fast load: building %s from the weights failed (%s); ordinary load", model_id, exc)
        return None
    finally:
        del sd
    try:
        if model.can_generate():
            from transformers import GenerationConfig
            model.generation_config = GenerationConfig.from_pretrained(model_dir)
    except Exception:  # noqa: BLE001 — models without a generation_config.json keep the default one
        pass
    log.info("fast load: %s built on %s in %.2f s", model_id, device, time.perf_counter() - t0)
    return model
