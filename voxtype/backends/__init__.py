"""Pluggable STT / TTS backends.

Each engine (`voxtype.stt_engine`, `voxtype.tts_engine`) is a thin
orchestrator: it owns lifecycle (load / unload / idle-watcher /
listener notifications) and delegates the heavy lifting to a
swappable backend selected by `settings.{stt,tts}_backend`.

A backend is a subclass of `STTBackend` / `TTSBackend` (see
`stt_base.py` / `tts_base.py`). It owns:
  - which Python library to import on `load_sync()`
  - model resolution rules (HF repo / local path / curated id)
  - inference (sync, run from the engine's single-thread executor)
  - catalog of supported voices / languages
  - whether `torch.compile`, fp16, streaming, etc. are honored

Backends are registered in the modality registries below and looked
up by name. Adding a new backend is one new module + one line in
the registry.

A backend module that fails to import (missing optional dep) is
skipped silently — the registry still works, the missing backend
is just not selectable until the user runs setup.ps1 with the
right extras.
"""
from __future__ import annotations

import logging
from typing import Type

from voxtype.backends.stt_base import STTBackend
from voxtype.backends.tts_base import TTSBackend

log = logging.getLogger("voxtype.backends")


# ── STT registry ─────────────────────────────────────────────────────

_STT: dict[str, Type[STTBackend]] = {}


def _register_stt(name: str, module_path: str, cls_name: str) -> None:
    try:
        mod = __import__(module_path, fromlist=[cls_name])
        _STT[name] = getattr(mod, cls_name)
    except Exception as exc:  # noqa: BLE001
        log.info("stt backend %r unavailable: %s", name, exc)


_register_stt("whisper", "voxtype.backends.whisper", "WhisperBackend")
# Add new STT backends here (faster-whisper, parakeet, …) — one line each.


def stt_backend_names() -> list[str]:
    return list(_STT.keys())


def get_stt_backend(name: str) -> STTBackend:
    cls = _STT.get(name) or _STT.get("whisper")
    if cls is None:
        raise RuntimeError("no STT backend available (transformers not installed?)")
    return cls()


# ── TTS registry ─────────────────────────────────────────────────────

_TTS: dict[str, Type[TTSBackend]] = {}


def _register_tts(name: str, module_path: str, cls_name: str) -> None:
    try:
        mod = __import__(module_path, fromlist=[cls_name])
        _TTS[name] = getattr(mod, cls_name)
    except Exception as exc:  # noqa: BLE001
        log.info("tts backend %r unavailable: %s", name, exc)


_register_tts("kokoro", "voxtype.backends.kokoro", "KokoroBackend")
# Add new TTS backends here (piper, coqui, parler, …) — one line each.


def tts_backend_names() -> list[str]:
    return list(_TTS.keys())


def get_tts_backend(name: str) -> TTSBackend:
    cls = _TTS.get(name) or _TTS.get("kokoro")
    if cls is None:
        raise RuntimeError("no TTS backend available")
    return cls()
