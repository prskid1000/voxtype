"""TTS engine orchestrator — owns lifecycle, delegates to the generic
backend.

Synthesis is done by `GenericTTSBackend`, which auto-dispatches by
model family. This module handles:
  - load / unload locking
  - idle-unload watcher
  - status listeners
  - per-call streaming bridge (sync generator → async queue → caller)
  - rebuild-on-config-change via `_key()`

Per-family options live in `settings.tts_opts` (a free-form dict).
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from voxtype.backends import get_tts_backend
from voxtype.backends.tts_base import TTSBackend, TTSLoadConfig

log = logging.getLogger("voxtype.tts_engine")


DEFAULT_MODEL = "hexgrad/Kokoro-82M"
DEFAULT_VOICE = "af_heart"


@dataclass
class TTSStatus:
    running: bool = False
    ready: bool = False
    pid: int | None = None
    last_error: str = ""
    family: str = ""

    @property
    def name(self) -> str:
        return "tts"


class TTSEngine:
    """Singleton — call `get_engine()`. Thread-safe."""

    def __init__(self) -> None:
        self._backend: TTSBackend | None = None
        self._model_lock = asyncio.Lock()
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxtype-tts")
        self._loaded_key: tuple | None = None
        self._status = TTSStatus()
        self._listeners: list[Callable[[TTSStatus], None]] = []
        self._last_used = 0.0
        self._idle_unload_sec = 0
        self._idle_watch_started = False
        self._loop: asyncio.AbstractEventLoop | None = None

        self._model_path = ""
        self._device = "cpu"
        self._voice = ""
        self._speed = 1.0
        self._warmup = True
        self._torch_compile = False
        self._attn_impl = "auto"
        self._stream_default = False
        self._seed = -1
        self._opts: dict[str, Any] = {}

    # ── Listener wiring ──────────────────────────────────────────────

    def on_status_change(self, fn: Callable[[TTSStatus], None]) -> None:
        self._listeners.append(fn)

    def get_status(self) -> TTSStatus:
        family = ""
        if self._backend is not None:
            try:
                family = self._backend.detected_family() or ""
            except Exception:
                family = ""
        return TTSStatus(
            running=self._status.running,
            ready=self._status.ready,
            pid=None,
            last_error=self._status.last_error,
            family=family,
        )

    def get_backend(self) -> TTSBackend | None:
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

    def _effective_voice(self) -> str:
        """Validate voice against the loaded backend's catalog. If the
        catalog is empty (pre-load), accept the user value as-is — the
        family handler falls back to its default when the catalog
        appears."""
        v = (self._voice or "").strip()
        be = self._backend
        if be is None:
            return v or DEFAULT_VOICE
        ids = be.voice_ids()
        if not ids:
            return v or (be.default_voice or DEFAULT_VOICE)
        if v in ids:
            return v
        return be.default_voice or next(iter(ids), DEFAULT_VOICE)

    def _key(self) -> tuple:
        return (
            self._effective_model(), self._device,
            bool(self._torch_compile), self._attn_impl,
        )

    async def configure(self, s) -> None:
        self._model_path = str(getattr(s, "tts_model_path", "") or "")
        self._device = str(getattr(s, "tts_device", "cpu"))
        self._voice = str(getattr(s, "tts_voice", "") or "")
        self._speed = float(getattr(s, "tts_speed", 1.0) or 1.0)
        self._warmup = bool(getattr(s, "tts_warmup", True))
        self._torch_compile = bool(getattr(s, "tts_torch_compile", False))
        self._attn_impl = str(getattr(s, "tts_attn_impl", "auto") or "auto")
        self._stream_default = bool(getattr(s, "tts_stream", False))
        self._seed = int(getattr(s, "tts_seed", -1))
        self._idle_unload_sec = int(getattr(s, "tts_idle_unload_sec", 0))
        opts = getattr(s, "tts_opts", {}) or {}
        self._opts = dict(opts) if isinstance(opts, dict) else {}

        if self._loaded_key is not None and self._loaded_key != self._key():
            log.info("tts config changed — unloading current backend")
            await self.unload()

    @property
    def sample_rate(self) -> int:
        return self._backend.sample_rate if self._backend is not None else 24000

    @property
    def stream_default(self) -> bool:
        return self._stream_default

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
        backend = get_tts_backend()
        log.info("tts loading model=%s device=%s", model_id, self._device)
        self._status.last_error = ""
        self._status.running = False
        self._status.ready = False
        self._notify()

        cfg = TTSLoadConfig(
            model_id=model_id,
            device=self._device,
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
            log.info("tts ready (%s)", backend.runtime_info())
            self._notify()
            self._ensure_idle_watcher()
        except Exception as exc:  # noqa: BLE001
            log.error("tts load failed: %s", exc)
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
        log.info("tts unloading")
        be = self._backend
        self._backend = None
        self._loaded_key = None
        self._status.running = False
        self._status.ready = False
        self._notify()
        try:
            be.unload_sync()
        except Exception as exc:  # noqa: BLE001
            log.debug("tts unload exc (%s)", exc)

    # ── Synthesis ────────────────────────────────────────────────────

    def _build_opts(self, voice: str | None,
                     speed: float | None) -> tuple[str, dict[str, Any]]:
        """Resolve the per-call voice + return the family opts dict."""
        v = (voice or "").strip() if isinstance(voice, str) else ""
        if not v:
            v = self._effective_voice()
        else:
            be = self._backend
            if be is not None:
                ids = be.voice_ids()
                if ids and v not in ids:
                    log.debug("tts: per-call voice %r unknown — using default", v)
                    v = self._effective_voice()
        be = self._backend
        supports_speed = be.supports("speed") if be is not None else True
        spd = (float(speed) if (speed and speed > 0)
               else float(self._speed or 1.0))
        if not supports_speed:
            spd = 1.0
        opts = dict(self._opts)
        opts["speed"] = spd
        # Universal seed: only forwarded to families that consume it.
        # Per-family `seed` (e.g. VITS) overrides the universal value.
        if self._seed != -1 and "seed" not in opts:
            opts["seed"] = int(self._seed)
        # Filter against backend's runtime spec when available, so
        # stale keys from a different family don't leak through.
        if be is not None:
            specs = be.runtime_options()
            if specs:
                allowed = {"speed", "seed"} | {s.key for s in specs}
                opts = {k: opts[k] for k in opts if k in allowed}
        return v, opts

    async def synthesize(self, text: str,
                          voice: str | None = None,
                          speed: float | None = None) -> bytes:
        """Return WAV bytes (16-bit mono, backend's native sample rate)."""
        await self.ensure_loaded()
        assert self._backend is not None
        self._last_used = time.monotonic()
        v, opts = self._build_opts(voice, speed)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._exec, self._collect_wav, text, v, opts)

    def _collect_wav(self, text: str, voice: str, opts: dict[str, Any]) -> bytes:
        """Drain the backend's sync chunk generator into a single WAV."""
        assert self._backend is not None
        parts: list[bytes] = []
        for chunk in self._backend.synth_chunks_sync(text, voice, opts):
            if chunk:
                parts.append(chunk)
        pcm = b"".join(parts)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._backend.sample_rate)
            wf.writeframes(pcm)
        return buf.getvalue()

    async def synthesize_pcm_chunks(
        self, text: str,
        voice: str | None = None,
        speed: float | None = None,
    ):
        """Async generator yielding raw int16 PCM chunks (mono).
        Server side wraps them in a chunked WAV response."""
        await self.ensure_loaded()
        assert self._backend is not None
        self._last_used = time.monotonic()
        v, opts = self._build_opts(voice, speed)

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)

        def _producer() -> None:
            try:
                assert self._backend is not None
                for chunk in self._backend.synth_chunks_sync(text, v, opts):
                    if not chunk:
                        continue
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        loop.run_in_executor(self._exec, _producer)
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk

    # ── Idle unload watcher ──────────────────────────────────────────

    def _ensure_idle_watcher(self) -> None:
        if self._idle_watch_started:
            return
        self._idle_watch_started = True

        def _loop_thread() -> None:
            INTERVAL = 2.0
            while True:
                time.sleep(INTERVAL)
                if self._backend is None:
                    continue
                if self._idle_unload_sec <= 0:
                    continue
                idle = time.monotonic() - (self._last_used or 0.0)
                if idle < self._idle_unload_sec:
                    continue
                log.info("tts idle for %.0fs ≥ %ds — unloading",
                         idle, self._idle_unload_sec)
                self._request_unload()

        threading.Thread(target=_loop_thread, daemon=True,
                         name="voxtype-tts-idle").start()

    def _request_unload(self) -> None:
        """Schedule unload() on the worker loop the model was loaded on.

        The idle watcher runs on its own thread; `unload()` acquires an
        asyncio.Lock bound to the worker loop, so it MUST run there.
        Using asyncio.run() would spin up a fresh loop and raise
        'bound to a different event loop' — which is why auto-unload
        silently never fired before."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.unload(), loop)
                return
            except Exception as exc:  # noqa: BLE001
                log.debug("tts idle-unload schedule failed: %s", exc)
        # Fallback: no live worker loop (shouldn't happen in-app).
        threading.Thread(
            target=lambda: asyncio.run(self.unload()), daemon=True,
        ).start()


# ── Module singleton ─────────────────────────────────────────────────

_ENGINE: TTSEngine | None = None


def get_engine() -> TTSEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = TTSEngine()
    return _ENGINE
