"""STT engine — thin proxy over the out-of-process torch worker.

Torch runs in `engine_worker` (a child process) so its CUDA context can be
freed by exiting that process on idle (see engine_host / engine_worker).
This module keeps the SAME public API the rest of VoxType expects
(`configure / ensure_loaded / transcribe / unload / get_status /
get_backend().detected_family() / idle_info / on_status_change`) but every
call forwards over IPC. Per-family options still live in `settings.stt_opts`
and are filtered worker-side against the live backend's `runtime_options()`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from voxtype.backends.shared import WHISPER_LANGUAGES
from voxtype.engine_host import get_host

log = logging.getLogger("voxtype.stt_engine")


DEFAULT_MODEL = "openai/whisper-base"
LANGUAGES = WHISPER_LANGUAGES


def all_language_codes() -> set[str]:
    return {c for c, _ in LANGUAGES}


def language_combo_options() -> list[tuple[str, str]]:
    return [
        (code, name if code == "auto" else f"{code} — {name}")
        for code, name in LANGUAGES
    ]


@dataclass
class EngineStatus:
    running: bool = False
    ready: bool = False
    pid: int | None = None
    last_error: str = ""
    family: str = ""

    @property
    def name(self) -> str:
        return "stt"


class _BackendView:
    """Stand-in for the in-process backend the settings UI used to poll.
    Reads the family the worker reported via the host status cache."""

    def detected_family(self) -> str:
        return (get_host().cached_status().get("stt") or {}).get("family", "")

    def runtime_info(self) -> dict:
        return get_host().cached_status().get("stt") or {}


class STTEngine:
    """Singleton — call `get_engine()`. Proxies to the shared worker."""

    def __init__(self) -> None:
        self._loading = False
        self._last_error = ""
        self._listeners: list[Callable[[EngineStatus], None]] = []
        self._backend_view = _BackendView()

        self._model_path = ""
        self._device = "cpu"
        self._language = "en"
        self._dtype_pref = "auto"
        self._warmup = True
        self._torch_compile = False
        self._attn_impl = "auto"
        self._idle_unload_sec = 0
        self._idle_exit_sec = 60
        self._opts: dict[str, Any] = {}

        get_host().on_status(self._on_host_status)

    # ── Listener wiring ──────────────────────────────────────────────

    def on_status_change(self, fn: Callable[[EngineStatus], None]) -> None:
        self._listeners.append(fn)

    def _on_host_status(self, _snap: dict) -> None:
        st = self.get_status()
        for fn in list(self._listeners):
            try:
                fn(st)
            except Exception:
                pass

    def get_status(self) -> EngineStatus:
        snap = get_host().cached_status().get("stt") or {}
        ready = bool(snap.get("loaded"))
        err = "" if ready else (snap.get("error") or self._last_error)
        return EngineStatus(
            running=ready or self._loading, ready=ready, pid=None,
            last_error=err, family=snap.get("family", ""),
        )

    def get_backend(self) -> _BackendView:
        return self._backend_view

    def idle_info(self) -> tuple[int, float]:
        snap = get_host().cached_status().get("stt") or {}
        limit = int(snap.get("idle_unload_sec", self._idle_unload_sec) or 0)
        if not snap.get("loaded"):
            return (limit, -1.0)
        return (limit, float(snap.get("remaining", -1.0)))

    # ── Configuration ────────────────────────────────────────────────

    def _cfg(self) -> dict[str, Any]:
        return {
            "model_id": self._model_path or "",
            "device": self._device, "dtype": self._dtype_pref,
            "warmup": self._warmup, "torch_compile": self._torch_compile,
            "attn_impl": self._attn_impl, "language": self._language,
            "opts": dict(self._opts), "idle_unload_sec": self._idle_unload_sec,
        }

    async def configure(self, s) -> None:
        self._model_path = str(getattr(s, "stt_model_path", "") or "")
        self._device = str(getattr(s, "stt_device", "cpu"))
        self._language = str(getattr(s, "stt_language", "en") or "en")
        self._dtype_pref = str(getattr(s, "stt_dtype", "auto") or "auto")
        self._warmup = bool(getattr(s, "stt_warmup", True))
        self._torch_compile = bool(getattr(s, "stt_torch_compile", False))
        self._attn_impl = str(getattr(s, "stt_attn_impl", "auto") or "auto")
        self._idle_unload_sec = int(getattr(s, "stt_idle_unload_sec", 0))
        self._idle_exit_sec = int(getattr(s, "engine_idle_exit_sec", 60))
        opts = getattr(s, "stt_opts", {}) or {}
        self._opts = dict(opts) if isinstance(opts, dict) else {}
        # Push to the worker if it is already up (spawn=False: configuring
        # at boot must not start torch). The worker also gets this cfg
        # inline on every load/transcribe, so a respawn is self-correcting.
        await self._send("configure", {"modality": "stt", "cfg": self._cfg(),
                                       "idle_exit_sec": self._idle_exit_sec},
                         spawn=False, swallow=True)

    # ── IPC helpers ──────────────────────────────────────────────────

    async def _send(self, op: str, header: dict, payload: bytes = b"", *,
                    spawn: bool = True, swallow: bool = False):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: get_host().request(op, header, payload, spawn=spawn))
        except Exception as exc:  # noqa: BLE001
            if swallow:
                return ({"ok": False, "error": str(exc)}, b"")
            raise

    # ── Lifecycle / inference ────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        self._loading = True
        self._notify()
        try:
            rhdr, _ = await self._send("load", {
                "modality": "stt", "cfg": self._cfg(),
                "idle_exit_sec": self._idle_exit_sec})
            if not rhdr.get("ok"):
                self._last_error = rhdr.get("error", "load failed")
                raise RuntimeError(self._last_error)
            self._last_error = ""
        finally:
            self._loading = False
            self._notify()

    async def transcribe(self, pcm: bytes, language: str | None = None) -> str:
        self._loading = True
        try:
            rhdr, _ = await self._send("transcribe", {
                "language": language or self._language, "cfg": self._cfg(),
                "idle_exit_sec": self._idle_exit_sec}, pcm)
        finally:
            self._loading = False
        if not rhdr.get("ok"):
            self._last_error = rhdr.get("error", "transcribe failed")
            raise RuntimeError(self._last_error)
        self._last_error = ""
        return rhdr.get("text", "")

    async def unload(self) -> None:
        # spawn=False: never resurrect an idle-exited worker just to unload.
        await self._send("unload", {"modality": "stt"}, spawn=False,
                         swallow=True)
        self._notify()

    def _notify(self) -> None:
        self._on_host_status({})


# ── Module singleton ─────────────────────────────────────────────────

_ENGINE: STTEngine | None = None


def get_engine() -> STTEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = STTEngine()
    return _ENGINE
