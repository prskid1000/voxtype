"""Direct in-process TTS inference via the `kokoro` PyPI package.

Kokoro-82M is an open-weight TTS model with 54 named voices spanning
nine language families: American + British English, Spanish, French,
Hindi, Italian, Japanese, Brazilian Portuguese, and Mandarin Chinese.
The package wraps PyTorch (so CUDA / CPU switching is just a torch
device move) and uses `misaki` + espeak-ng for phonemization.

Default model:  `hexgrad/Kokoro-82M` (~327 MB on disk)
Default voice:  `af_heart` — American female "Heart"

Voice names are strings prefixed by language + gender:
    a{f,m}_*  — American English (e.g. af_heart, am_adam)
    b{f,m}_*  — British English  (e.g. bf_emma, bm_george)
    e{f,m}_*  — Spanish
    f{f,m}_*  — French
    h{f,m}_*  — Hindi
    i{f,m}_*  — Italian
    j{f,m}_*  — Japanese (e.g. jf_alpha, jm_kumo)
    p{f,m}_*  — Brazilian Portuguese
    z{f,m}_*  — Mandarin Chinese (e.g. zf_xiaobei, zm_yunjian)

OpenAI-compatible `voice` request field accepts the same strings.
Empty falls back to `tts_speaker` from settings.
"""
from __future__ import annotations

import asyncio
import gc
import io
import logging
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("voxtype.tts_engine")


# ── Default model ────────────────────────────────────────────────────
# `hexgrad/Kokoro-82M` is the official upstream Kokoro release. The
# `kokoro` PyPI package auto-resolves repo + voice files from the HF
# cache (`~/.cache/huggingface/hub/`).
DEFAULT_MODEL = "hexgrad/Kokoro-82M"
DEFAULT_VOICE = "af_heart"


# ── Status type ──────────────────────────────────────────────────────

@dataclass
class TTSStatus:
    running: bool = False
    ready: bool = False
    pid: int | None = None
    last_error: str = ""

    @property
    def name(self) -> str:
        return "tts"


class TTSEngine:
    """Singleton — call `get_engine()`. Thread-safe."""

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._model_lock = asyncio.Lock()
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxtype-tts")
        self._loaded_key: tuple | None = None
        self._status = TTSStatus()
        self._listeners: list[Callable[[TTSStatus], None]] = []
        self._last_used = 0.0
        self._idle_unload_sec = 0
        self._idle_watch_started = False
        self._sample_rate = 24000   # kokoro default
        self._torch_device = "cpu"

        # Current settings.
        self._model_path = ""
        self._device = "cpu"
        self._speaker = DEFAULT_VOICE
        self._length_scale = 1.0
        self._lang_code = "a"
        self._warmup = True
        self._torch_compile = False
        self._stream_default = False

    # ── Listener wiring ──────────────────────────────────────────────

    def on_status_change(self, fn: Callable[[TTSStatus], None]) -> None:
        self._listeners.append(fn)

    def get_status(self) -> TTSStatus:
        return TTSStatus(
            running=self._status.running,
            ready=self._status.ready,
            pid=None,
            last_error=self._status.last_error,
        )

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self.get_status())
            except Exception:
                pass

    # ── Configuration ────────────────────────────────────────────────

    def _effective_model(self) -> str:
        """Empty setting → use the built-in default."""
        return self._model_path or DEFAULT_MODEL

    def _effective_voice(self) -> str:
        return (self._speaker or "").strip() or DEFAULT_VOICE

    def _key(self) -> tuple:
        # Only fields that require a pipeline rebuild belong here.
        # speaker / length_scale / stream_default are per-call.
        return (
            self._effective_model(), self._device,
            self._lang_code, bool(self._torch_compile),
        )

    async def configure(self, s) -> None:
        self._model_path = str(getattr(s, "tts_model_path", "") or "")
        self._device = str(getattr(s, "tts_device", "cpu"))
        self._speaker = str(getattr(s, "tts_speaker", DEFAULT_VOICE) or DEFAULT_VOICE)
        self._length_scale = float(getattr(s, "tts_length_scale", 1.0) or 1.0)
        self._lang_code = str(getattr(s, "tts_lang_code", "a") or "a")
        self._warmup = bool(getattr(s, "tts_warmup", True))
        self._torch_compile = bool(getattr(s, "tts_torch_compile", False))
        self._stream_default = bool(getattr(s, "tts_stream", False))
        self._idle_unload_sec = int(getattr(s, "tts_idle_unload_sec", 0))

        if self._loaded_key is not None and self._loaded_key != self._key():
            log.info("tts config changed — unloading current model")
            await self.unload()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def stream_default(self) -> bool:
        return self._stream_default

    # ── Load / unload ────────────────────────────────────────────────

    async def ensure_loaded(self) -> None:
        if self._pipeline is not None and self._loaded_key == self._key():
            return
        async with self._model_lock:
            if self._pipeline is not None and self._loaded_key == self._key():
                return
            if self._pipeline is not None:
                await self._do_unload_locked()
            await self._do_load_locked()

    async def _do_load_locked(self) -> None:
        model = self._effective_model()
        log.info("tts loading model=%s device=%s", model, self._device)
        self._status.last_error = ""
        self._status.running = False
        self._status.ready = False
        self._notify()
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self._exec, self._build_pipeline, model)
            self._loaded_key = self._key()
            self._status.running = True
            self._status.ready = True
            self._last_used = time.monotonic()
            log.info("tts ready (device=%s sample_rate=%d Hz)",
                     self._torch_device, self._sample_rate)
            self._notify()
            self._ensure_idle_watcher()
        except Exception as exc:
            log.error("tts load failed: %s", exc)
            self._pipeline = None
            self._loaded_key = None
            self._status.running = False
            self._status.ready = False
            self._status.last_error = str(exc)
            self._notify()
            raise

    def _build_pipeline(self, model_repo: str) -> None:
        """Sync — runs in the executor. Builds a `kokoro.KPipeline` on
        the resolved torch device. The pipeline lazy-loads voice tensors
        from the HF cache on first synthesise per voice."""
        import torch
        from kokoro import KPipeline

        if self._device == "cuda" and torch.cuda.is_available():
            self._torch_device = "cuda"
        else:
            if self._device == "cuda":
                log.warning("tts: device=cuda requested but torch.cuda.is_available()=False — using CPU")
            self._torch_device = "cpu"

        # KPipeline picks the language family from the voice prefix at
        # synthesise time. lang_code is the fallback used when the voice
        # prefix doesn't match a known family — default "a" (American
        # English) keeps misaki quiet for af_*/am_* voices.
        self._pipeline = KPipeline(
            lang_code=self._lang_code or "a",
            repo_id=model_repo,
            device=self._torch_device,
        )

        if self._torch_compile:
            try:
                inner = getattr(self._pipeline, "model", None)
                if inner is not None:
                    log.info("tts torch.compile() — first synth will pause for JIT")
                    self._pipeline.model = torch.compile(inner, mode="reduce-overhead")
            except Exception as exc:
                log.warning("tts: torch.compile failed (%s) — running uncompiled", exc)

        if self._warmup:
            try:
                _ = self._do_synthesize(
                    "Voxtype ready.", self._effective_voice(), self._length_scale,
                )
                log.info("tts warmup ok")
            except Exception as exc:
                log.warning("tts: warmup failed (%s) — first real call may be slow", exc)

    async def unload(self) -> None:
        async with self._model_lock:
            await self._do_unload_locked()

    async def _do_unload_locked(self) -> None:
        if self._pipeline is None:
            return
        log.info("tts unloading")
        self._pipeline = None
        self._loaded_key = None
        self._status.running = False
        self._status.ready = False
        self._notify()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()

    # ── Synthesis ────────────────────────────────────────────────────

    async def synthesize(self, text: str,
                          voice: str | None = None,
                          speed: float | None = None) -> bytes:
        """Return WAV bytes (16-bit mono, 24 kHz).

        `voice`: Kokoro voice name (e.g. `af_heart`, `jm_kumo`). Empty
            or None → falls back to settings.tts_speaker.
        `speed`: OpenAI-shape (1.0 = normal). Maps to Kokoro pipeline's
            `speed` arg directly.
        """
        await self.ensure_loaded()
        self._last_used = time.monotonic()
        v = (voice or "").strip() if isinstance(voice, str) else ""
        if not v:
            v = self._effective_voice()
        spd = float(speed) if (speed and speed > 0) else float(self._length_scale or 1.0)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._exec, self._do_synthesize, text, v, spd,
        )

    async def synthesize_pcm_chunks(
        self, text: str,
        voice: str | None = None,
        speed: float | None = None,
    ):
        """Async generator yielding raw int16 PCM chunks (mono, sample_rate Hz).

        Each yielded chunk is one Kokoro sentence's worth of audio. The
        HTTP layer wraps this into a streaming WAV response so external
        clients hear the first sentence in ~200 ms instead of waiting
        for the whole utterance.
        """
        await self.ensure_loaded()
        self._last_used = time.monotonic()
        v = (voice or "").strip() if isinstance(voice, str) else ""
        if not v:
            v = self._effective_voice()
        spd = float(speed) if (speed and speed > 0) else float(self._length_scale or 1.0)

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)

        def _producer() -> None:
            import numpy as np
            import torch
            try:
                for _, _, audio in self._pipeline(text, voice=v, speed=spd):
                    if audio is None:
                        continue
                    if isinstance(audio, torch.Tensor):
                        arr = audio.detach().cpu().to(torch.float32).numpy()
                    else:
                        arr = np.asarray(audio, dtype=np.float32)
                    arr = arr.reshape(-1)
                    np.clip(arr, -1.0, 1.0, out=arr)
                    pcm = (arr * 32767.0).astype(np.int16).tobytes()
                    asyncio.run_coroutine_threadsafe(queue.put(pcm), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        loop.run_in_executor(self._exec, _producer)
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk

    def _do_synthesize(self, text: str, voice: str, speed: float) -> bytes:
        """Sync — runs in the executor. Returns WAV bytes.

        KPipeline yields per-sentence chunks; we concatenate the audio
        tensors into one waveform and write a single WAV.
        """
        import numpy as np
        import torch

        chunks: list[np.ndarray] = []
        for _, _, audio in self._pipeline(text, voice=voice, speed=speed):
            if audio is None:
                continue
            if isinstance(audio, torch.Tensor):
                arr = audio.detach().cpu().to(torch.float32).numpy()
            else:
                arr = np.asarray(audio, dtype=np.float32)
            chunks.append(arr.reshape(-1))

        if not chunks:
            samples = np.zeros(0, dtype=np.float32)
        else:
            samples = np.concatenate(chunks)

        np.clip(samples, -1.0, 1.0, out=samples)
        int16 = (samples * 32767.0).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(int16.tobytes())
        return buf.getvalue()

    # ── Idle unload watcher ──────────────────────────────────────────

    def _ensure_idle_watcher(self) -> None:
        if self._idle_watch_started:
            return
        self._idle_watch_started = True

        def _loop_thread() -> None:
            INTERVAL = 30.0
            while True:
                time.sleep(INTERVAL)
                if self._pipeline is None:
                    continue
                if self._idle_unload_sec <= 0:
                    continue
                idle = time.monotonic() - (self._last_used or 0.0)
                if idle < self._idle_unload_sec:
                    continue
                log.info("tts idle for %.0fs ≥ %ds — unloading",
                         idle, self._idle_unload_sec)
                threading.Thread(
                    target=lambda: asyncio.run(self.unload()),
                    daemon=True,
                ).start()

        threading.Thread(target=_loop_thread, daemon=True,
                         name="voxtype-tts-idle").start()


# ── Module singleton ─────────────────────────────────────────────────

_ENGINE: TTSEngine | None = None


def get_engine() -> TTSEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = TTSEngine()
    return _ENGINE
