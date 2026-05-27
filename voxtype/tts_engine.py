"""TTS engine — thin proxy over the out-of-process torch worker.

Torch runs in `engine_worker`; this module forwards over IPC while keeping
the public API (`configure / ensure_loaded / synthesize /
synthesize_pcm_chunks / unload / get_status / get_backend / idle_info /
sample_rate / stream_default / on_status_change`). The worker returns raw
PCM; we wrap it into WAV here (so server.py's WAV response is unchanged).
"""
from __future__ import annotations

import asyncio
import io
import logging
import wave
from dataclasses import dataclass
from typing import Any, Callable

from voxtype.engine_host import get_host

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


class _BackendView:
    def detected_family(self) -> str:
        return (get_host().cached_status().get("tts") or {}).get("family", "")

    def voices(self) -> list:
        # Dynamic (non-catalog) voices live in the worker; the static
        # family_detect catalog covers the common families, so this
        # fallback degrades to empty rather than a cross-process query.
        return []


class TTSEngine:
    """Singleton — call `get_engine()`. Proxies to the shared worker."""

    def __init__(self) -> None:
        self._loading = False
        self._last_error = ""
        self._sample_rate = 24000
        self._listeners: list[Callable[[TTSStatus], None]] = []
        self._backend_view = _BackendView()

        self._model_path = ""
        self._device = "cpu"
        self._voice = ""
        self._speed = 1.0
        self._warmup = True
        self._torch_compile = False
        self._attn_impl = "auto"
        self._stream_default = False
        self._seed = -1
        self._idle_unload_sec = 0
        self._idle_exit_sec = 60
        self._opts: dict[str, Any] = {}

        get_host().on_status(self._on_host_status)

    # ── Listener wiring ──────────────────────────────────────────────

    def on_status_change(self, fn: Callable[[TTSStatus], None]) -> None:
        self._listeners.append(fn)

    def _on_host_status(self, _snap: dict) -> None:
        snap = get_host().cached_status().get("tts") or {}
        if snap.get("loaded") and snap.get("sample_rate"):
            self._sample_rate = int(snap["sample_rate"])
        st = self.get_status()
        for fn in list(self._listeners):
            try:
                fn(st)
            except Exception:
                pass

    def get_status(self) -> TTSStatus:
        snap = get_host().cached_status().get("tts") or {}
        ready = bool(snap.get("loaded"))
        err = "" if ready else (snap.get("error") or self._last_error)
        return TTSStatus(
            running=ready or self._loading, ready=ready, pid=None,
            last_error=err, family=snap.get("family", ""),
        )

    def get_backend(self) -> _BackendView:
        return self._backend_view

    def idle_info(self) -> tuple[int, float]:
        snap = get_host().cached_status().get("tts") or {}
        limit = int(snap.get("idle_unload_sec", self._idle_unload_sec) or 0)
        if not snap.get("loaded"):
            return (limit, -1.0)
        return (limit, float(snap.get("remaining", -1.0)))

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def stream_default(self) -> bool:
        return self._stream_default

    # ── Configuration ────────────────────────────────────────────────

    def _cfg(self) -> dict[str, Any]:
        return {
            "model_id": self._model_path or "",
            "device": self._device, "voice": self._voice,
            "speed": self._speed, "warmup": self._warmup,
            "torch_compile": self._torch_compile, "attn_impl": self._attn_impl,
            "seed": self._seed, "opts": dict(self._opts),
            "idle_unload_sec": self._idle_unload_sec,
        }

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
        self._idle_exit_sec = int(getattr(s, "engine_idle_exit_sec", 60))
        opts = getattr(s, "tts_opts", {}) or {}
        self._opts = dict(opts) if isinstance(opts, dict) else {}
        await self._send("configure", {"modality": "tts", "cfg": self._cfg(),
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

    # ── Lifecycle / synthesis ────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        self._loading = True
        self._notify()
        try:
            rhdr, _ = await self._send("load", {
                "modality": "tts", "cfg": self._cfg(),
                "idle_exit_sec": self._idle_exit_sec})
            if not rhdr.get("ok"):
                self._last_error = rhdr.get("error", "load failed")
                raise RuntimeError(self._last_error)
            if rhdr.get("sample_rate"):
                self._sample_rate = int(rhdr["sample_rate"])
            self._last_error = ""
        finally:
            self._loading = False
            self._notify()

    async def synthesize(self, text: str, voice: str | None = None,
                         speed: float | None = None) -> bytes:
        """Return WAV bytes (16-bit mono, the backend's native rate)."""
        rhdr, pcm = await self._send("synthesize", {
            "text": text, "voice": voice, "speed": speed,
            "cfg": self._cfg(), "idle_exit_sec": self._idle_exit_sec})
        if not rhdr.get("ok"):
            self._last_error = rhdr.get("error", "synthesize failed")
            raise RuntimeError(self._last_error)
        self._last_error = ""
        sr = int(rhdr.get("sample_rate", self._sample_rate))
        self._sample_rate = sr
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm)
        return buf.getvalue()

    async def synthesize_pcm_chunks(self, text: str, voice: str | None = None,
                                    speed: float | None = None):
        """Async generator of raw int16 PCM chunks. Bridges the worker's
        blocking stream (run in an executor) into an asyncio queue."""
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        header = {"text": text, "voice": voice, "speed": speed,
                  "cfg": self._cfg(), "idle_exit_sec": self._idle_exit_sec}

        def _producer() -> None:
            try:
                for fhdr, payload in get_host().stream("synth_stream", header):
                    if fhdr.get("sample_rate"):
                        self._sample_rate = int(fhdr["sample_rate"])
                    if payload:
                        asyncio.run_coroutine_threadsafe(
                            queue.put(payload), loop).result()
            except Exception as exc:  # noqa: BLE001
                log.error("tts stream failed: %s", exc)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        loop.run_in_executor(None, _producer)
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk

    async def unload(self) -> None:
        await self._send("unload", {"modality": "tts"}, spawn=False,
                         swallow=True)
        self._notify()

    def _notify(self) -> None:
        self._on_host_status({})


# ── Module singleton ─────────────────────────────────────────────────

_ENGINE: TTSEngine | None = None


def get_engine() -> TTSEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = TTSEngine()
    return _ENGINE
