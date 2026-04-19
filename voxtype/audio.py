"""Raw 16 kHz mono int16 PCM recorder built on sounddevice.

Start recording when the hotkey activates, stop + return the buffer
when it releases. Runs on sounddevice's own callback thread; caller
polls `stop()` from any thread.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("voxtype.audio")

SAMPLE_RATE = 16000
CHANNELS = 1


class Recorder:
    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        """Open the input stream. No-op if already recording."""
        if self._stream is not None:
            return
        self._chunks.clear()

        def _callback(indata, frames, time_info, status):
            if status:
                log.debug("audio status: %s", status)
            # indata is float32 by default — we convert to int16 PCM
            pcm = (np.clip(indata[:, 0], -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            with self._lock:
                self._chunks.append(pcm)

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", callback=_callback,
        )
        self._stream.start()

    def stop(self) -> bytes:
        """Close the stream and return the captured PCM. Empty bytes
        if nothing was recorded."""
        if self._stream is None:
            return b""
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:
            log.warning("stopping stream: %s", exc)
        self._stream = None
        with self._lock:
            data = b"".join(self._chunks)
            self._chunks.clear()
        return data

    @property
    def recording(self) -> bool:
        return self._stream is not None
