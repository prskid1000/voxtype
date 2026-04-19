"""Voice activity detection — energy-based RMS on raw 16-bit PCM.

Simpler than the TS version because we record PCM directly (via
sounddevice) rather than WebM/Opus, so we don't need to work around
codec headers/trailers. Pure numpy, zero model weights."""
from __future__ import annotations

import numpy as np


# RMS threshold below which we treat a frame as silence.
# Empirical: ~100 for typical desktop mics at 16 kHz mono 16-bit.
_SILENCE_RMS = 100.0

# Minimum audio duration in seconds to consider (below this it's noise).
_MIN_DURATION_SEC = 0.30


def has_speech(pcm: bytes, sample_rate: int = 16000) -> bool:
    """Return True if the buffer contains speech above the silence floor.
    Expects signed little-endian int16 samples (the sounddevice default)."""
    if len(pcm) < 2 * int(sample_rate * _MIN_DURATION_SEC):
        return False
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return False
    # Skip the first 20% and last 20% — ducks recorder-start clicks and
    # trailing mic pops.
    lo = samples.size // 5
    hi = samples.size - lo
    segment = samples[lo:hi]
    if segment.size == 0:
        return False
    rms = float(np.sqrt(np.mean(segment.astype(np.float64) ** 2)))
    return rms > _SILENCE_RMS


def estimate_duration(pcm: bytes, sample_rate: int = 16000) -> float:
    """Seconds of audio in a 16-bit PCM buffer."""
    return len(pcm) / (2.0 * sample_rate)
