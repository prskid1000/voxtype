"""STT engine orchestrator — owns lifecycle, delegates to the generic
backend.

The actual transcription work is done by `GenericSTTBackend`, which
auto-dispatches by model family. This module handles:
  - load / unload locking
  - idle-unload watcher
  - status listeners
  - rebuild-on-config-change via `_key()`

Per-family options live in `settings.stt_opts` (a free-form dict).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable

from voxtype.backends import get_stt_backend
from voxtype.backends.shared import WHISPER_LANGUAGES
from voxtype.backends.stt_base import LoadConfig, STTBackend

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


class STTEngine:
    """Singleton — call `get_engine()`. Thread-safe."""

    def __init__(self) -> None:
        self._backend: STTBackend | None = None
        self._model_lock = asyncio.Lock()
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxtype-stt")
        self._loaded_key: tuple | None = None
        self._status = EngineStatus()
        self._listeners: list[Callable[[EngineStatus], None]] = []
        self._last_used = 0.0
        self._idle_unload_sec = 0
        self._idle_watch_started = False
        self._loop: asyncio.AbstractEventLoop | None = None

        self._model_path = ""
        self._device = "cpu"
        self._language = "en"
        self._dtype_pref = "auto"
        self._warmup = True
        self._torch_compile = False
        self._attn_impl = "auto"
        self._opts: dict[str, Any] = {}

    # ── Listener wiring ──────────────────────────────────────────────

    def on_status_change(self, fn: Callable[[EngineStatus], None]) -> None:
        self._listeners.append(fn)

    def get_status(self) -> EngineStatus:
        family = ""
        if self._backend is not None:
            try:
                family = self._backend.detected_family() or ""
            except Exception:
                family = ""
        return EngineStatus(
            running=self._status.running,
            ready=self._status.ready,
            pid=None,
            last_error=self._status.last_error,
            family=family,
        )

    def get_backend(self) -> STTBackend | None:
        return self._backend

    def idle_info(self) -> tuple[int, float]:
        """Live auto-unload telemetry for the UI.

        Returns (idle_unload_sec, remaining_sec). remaining_sec is -1
        when the model isn't loaded or auto-unload is disabled; otherwise
        the seconds left before the idle watcher unloads."""
        if self._backend is None or self._idle_unload_sec <= 0:
            return (self._idle_unload_sec, -1.0)
        idle = time.monotonic() - (self._last_used or 0.0)
        return (self._idle_unload_sec, max(0.0, self._idle_unload_sec - idle))

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self.get_status())
            except Exception:
                pass

    # ── Configuration ────────────────────────────────────────────────

    def _effective_model(self) -> str:
        return self._model_path or DEFAULT_MODEL

    def _key(self) -> tuple:
        # Fields that require a model rebuild. Per-call kwargs (language,
        # task, beams, prompt) are NOT in here.
        return (
            self._effective_model(), self._device,
            self._dtype_pref, bool(self._torch_compile),
            self._attn_impl,
        )

    async def configure(self, s) -> None:
        self._model_path = str(getattr(s, "stt_model_path", "") or "")
        self._device = str(getattr(s, "stt_device", "cpu"))
        self._language = str(getattr(s, "stt_language", "en") or "en")
        self._dtype_pref = str(getattr(s, "stt_dtype", "auto") or "auto")
        self._warmup = bool(getattr(s, "stt_warmup", True))
        self._torch_compile = bool(getattr(s, "stt_torch_compile", False))
        self._attn_impl = str(getattr(s, "stt_attn_impl", "auto") or "auto")
        self._idle_unload_sec = int(getattr(s, "stt_idle_unload_sec", 0))
        opts = getattr(s, "stt_opts", {}) or {}
        self._opts = dict(opts) if isinstance(opts, dict) else {}

        if self._loaded_key is not None and self._loaded_key != self._key():
            log.info("stt config changed — unloading current backend")
            await self.unload()

    # ── Load / unload ────────────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        if self._backend is not None and self._loaded_key == self._key():
            return
        async with self._model_lock:
            if self._backend is not None and self._loaded_key == self._key():
                return
            if self._backend is not None:
                await self._do_unload_locked()
            await self._do_load_locked()

    async def _do_load_locked(self) -> None:
        model_id = self._effective_model()
        self._loop = asyncio.get_running_loop()
        backend = get_stt_backend()
        log.info("stt loading model=%s device=%s", model_id, self._device)
        self._status.last_error = ""
        self._status.running = False
        self._status.ready = False
        self._notify()

        cfg = LoadConfig(
            model_id=model_id,
            device=self._device,
            dtype=self._dtype_pref,
            warmup=self._warmup,
            torch_compile=self._torch_compile,
            attn_impl=self._attn_impl,
        )
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self._exec, backend.load_sync, cfg)
            self._backend = backend
            self._loaded_key = self._key()
            self._status.running = True
            self._status.ready = True
            self._last_used = time.monotonic()
            log.info("stt ready (%s)", backend.runtime_info())
            self._notify()
            self._ensure_idle_watcher()
        except Exception as exc:  # noqa: BLE001
            log.error("stt load failed: %s", exc)
            self._backend = None
            self._loaded_key = None
            self._status.running = False
            self._status.ready = False
            self._status.last_error = str(exc)
            self._notify()
            raise

    async def unload(self) -> None:
        async with self._model_lock:
            await self._do_unload_locked()

    async def _do_unload_locked(self) -> None:
        if self._backend is None:
            return
        log.info("stt unloading")
        be = self._backend
        self._backend = None
        self._loaded_key = None
        self._status.running = False
        self._status.ready = False
        self._notify()
        try:
            # Free weights off the event loop — GPU teardown can be slow
            # and must not stall the worker loop (or shutdown).
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._exec, be.unload_sync)
        except Exception as exc:  # noqa: BLE001
            log.debug("stt unload exc (%s)", exc)

    # ── Transcription ────────────────────────────────────────────────

    def _build_opts(self, language: str | None) -> dict[str, Any]:
        """Per-call opts dict assembled from settings + filtered against
        the backend's `supports()` flags. The universal `language` is
        always passed through; family-specific opts are filtered out
        for backends that don't honour them so a stale entry from a
        different family can't sneak through."""
        backend = self._backend
        lang = (language or self._language or "en").strip() or "en"
        out: dict[str, Any] = {"language": lang}
        if not backend:
            return out
        specs = backend.runtime_options() if hasattr(backend, "runtime_options") else []
        allowed = {s.key for s in specs}
        for k, v in self._opts.items():
            if k in allowed:
                out[k] = v
        return out

    async def transcribe(self, pcm: bytes, language: str | None = None) -> str:
        """Run STT on raw 16 kHz mono int16 PCM. Returns the text."""
        await self.ensure_loaded()
        self._last_used = time.monotonic()
        assert self._backend is not None
        opts = self._build_opts(language)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._exec, self._backend.transcribe_sync, pcm, opts,
        )

    # ── Idle unload watcher ──────────────────────────────────────────

    def _ensure_idle_watcher(self) -> None:
        if self._idle_watch_started:
            return
        self._idle_watch_started = True

        def _loop_thread() -> None:
            INTERVAL = 2.0
            while True:
                time.sleep(INTERVAL)
                try:
                    if self._backend is None:
                        continue
                    if self._idle_unload_sec <= 0:
                        continue
                    idle = time.monotonic() - (self._last_used or 0.0)
                    if idle < self._idle_unload_sec:
                        continue
                    log.info("stt idle for %.0fs ≥ %ds — unloading",
                             idle, self._idle_unload_sec)
                    self._request_unload()
                except Exception as exc:  # noqa: BLE001 — never let the watcher die
                    log.error("stt idle watcher iteration failed: %s", exc)

        threading.Thread(target=_loop_thread, daemon=True,
                         name="voxtype-stt-idle").start()

    def _request_unload(self) -> None:
        """Run unload() on the worker loop the model was loaded on, and
        BLOCK this (watcher) thread until it finishes.

        `unload()` acquires an asyncio.Lock bound to the worker loop, so
        it must run there — asyncio.run() would spin up a fresh loop and
        raise 'bound to a different event loop'. We wait on the Future so
        the watcher only re-fires after the unload actually settles
        (instead of queuing a new unload every 2 s) and so failures or
        stalls are logged instead of vanishing."""
        loop = self._loop
        if loop is None or not loop.is_running():
            log.warning("stt idle-unload: worker loop unavailable "
                        "(loop=%r) — skipping", loop)
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.unload(), loop)
            fut.result(timeout=60)
            log.info("stt idle-unload complete")
        except FuturesTimeout:
            log.error("stt idle-unload timed out — worker loop busy; "
                      "will retry")
        except Exception as exc:  # noqa: BLE001
            log.error("stt idle-unload failed: %s", exc)

        threading.Thread(target=_loop_thread, daemon=True,
                         name="voxtype-stt-idle").start()


# ── Module singleton ─────────────────────────────────────────────────

_ENGINE: STTEngine | None = None


def get_engine() -> STTEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = STTEngine()
    return _ENGINE
