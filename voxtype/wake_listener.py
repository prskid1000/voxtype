"""Always-on "start words" listener for hands-free voice activation.

When voice activation is enabled, this owns a single continuous 16 kHz
mono PCM input stream and segments it into short utterances using the
same energy-RMS voice-activity logic as `voxtype/audio.py:Recorder`.
Each completed *short* utterance (a candidate wake phrase) is handed to
`on_utterance` on a worker thread; the orchestrator transcribes it via
the existing STT engine and, if it matches a configured start word,
begins a normal dictation capture.

Only one input stream is ever actively capturing at a time. Before the
orchestrator starts the dictation `Recorder` it calls `pause()` (which
closes this stream); after the pipeline finishes it calls `resume()`.
This avoids two concurrent streams on the same device and stops the
listener re-triggering on the dictation audio.

Long utterances (longer than `max_phrase_sec`) are discarded without
transcription — wake phrases are 1-3 words. All log strings stay ASCII
(the Windows console is cp1252; see CLAUDE.md).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("voxtype.wake")

# Strip everything that isn't a lowercase letter/digit/space so STT
# punctuation ("computer," / "Computer.") matches a typed start word.
_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _normalize(s: str) -> str:
    return " ".join(_NORM_RE.sub(" ", (s or "").lower()).split())


def matches_start_word(text: str, words_csv: str, contains: bool = False) -> bool:
    """True if `text` triggers voice activation.

    `words_csv` is a comma-separated list of trigger phrases. By default a
    phrase matches when the transcript *starts with* it (on a word
    boundary), e.g. "computer take a note" matches "computer". With
    `contains=True` the phrase may appear anywhere in the transcript.
    Matching is case/punctuation-insensitive."""
    norm = _normalize(text)
    if not norm:
        return False
    phrases = [p for p in (_normalize(w) for w in words_csv.split(",")) if p]
    for p in phrases:
        if contains:
            if p == norm or f" {p} " in f" {norm} ":
                return True
        else:
            if norm == p or norm.startswith(p + " "):
                return True
    return False

SAMPLE_RATE = 16000
CHANNELS = 1

# Per-frame RMS below this is silence. Same threshold as audio.py / vad.py.
_SILENCE_RMS = 100.0

# Trailing quiet (seconds) that marks the end of an utterance. Kept short
# for responsiveness — a wake phrase is followed by a brief pause.
_END_SILENCE_SEC = 0.6

# Shortest utterance worth transcribing (below this it's a click/pop).
_MIN_PHRASE_SEC = 0.3


class WakeListener:
    def __init__(self, on_utterance: Callable[[bytes], None]) -> None:
        self._on_utterance = on_utterance
        self._stream: sd.InputStream | None = None
        self._enabled = False          # feature on (vs transiently paused)
        self._max_phrase_sec = 2.5
        self._lock = threading.Lock()

        # Per-utterance segmentation state (callback thread only).
        self._buf: list[bytes] = []
        self._had_speech = False
        self._too_long = False
        self._silence_start: float | None = None

    # ── Public API ───────────────────────────────────────────────────

    def start(self, max_phrase_sec: float = 2.5) -> None:
        """Enable voice activation and open the input stream."""
        self._max_phrase_sec = max(_MIN_PHRASE_SEC, float(max_phrase_sec))
        self._enabled = True
        self._open()

    def stop(self) -> None:
        """Disable voice activation and close the input stream."""
        self._enabled = False
        self._close()

    def pause(self) -> None:
        """Transiently close the stream (e.g. while a dictation capture
        runs) without disabling the feature. No-op if not enabled."""
        if self._enabled:
            self._close()

    def resume(self) -> None:
        """Reopen the stream after a pause, if the feature is enabled."""
        if self._enabled:
            self._open()

    @property
    def listening(self) -> bool:
        return self._stream is not None

    # ── Stream lifecycle ─────────────────────────────────────────────

    def _open(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._reset_segment()
            try:
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype="float32", callback=self._callback,
                )
                self._stream.start()
                log.info("wake listener started (max phrase %.1fs)",
                         self._max_phrase_sec)
            except Exception as exc:
                self._stream = None
                log.error("wake listener failed to open: %s", exc)

    def _close(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                log.warning("closing wake stream: %s", exc)
            self._stream = None
            self._reset_segment()
            log.info("wake listener stopped")

    # ── Segmentation ─────────────────────────────────────────────────

    def _reset_segment(self) -> None:
        self._buf = []
        self._had_speech = False
        self._too_long = False
        self._silence_start = None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("wake audio status: %s", status)
        pcm = (np.clip(indata[:, 0], -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        samples = np.frombuffer(pcm, dtype=np.int16)
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0

        now = time.monotonic()
        if rms > _SILENCE_RMS:
            # Speech frame.
            self._had_speech = True
            self._silence_start = None
            if not self._too_long:
                self._buf.append(pcm)
                if self._duration() > self._max_phrase_sec:
                    # Too long to be a wake phrase — stop buffering, wait
                    # for the trailing silence, then discard.
                    self._too_long = True
                    self._buf = []
            return

        # Silence frame.
        if not self._had_speech:
            return  # leading silence — nothing to segment yet
        if not self._too_long:
            self._buf.append(pcm)
        if self._silence_start is None:
            self._silence_start = now
        elif (now - self._silence_start) >= _END_SILENCE_SEC:
            self._finalize()

    def _duration(self) -> float:
        return sum(len(c) for c in self._buf) / (2.0 * SAMPLE_RATE)

    def _finalize(self) -> None:
        """End the current utterance: emit it as a candidate if it's a
        plausible wake phrase, otherwise discard. Resets for the next."""
        emit = None
        if not self._too_long and _MIN_PHRASE_SEC <= self._duration() <= self._max_phrase_sec:
            emit = b"".join(self._buf)
        self._reset_segment()
        if emit:
            threading.Thread(target=self._safe_emit, args=(emit,),
                             daemon=True, name="voxtype-wake-cb").start()

    def _safe_emit(self, pcm: bytes) -> None:
        try:
            self._on_utterance(pcm)
        except Exception as exc:
            log.error("wake on_utterance failed: %s", exc)
