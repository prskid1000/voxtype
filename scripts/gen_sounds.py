"""Generate three 1-second cue WAV files into voxtype/resources/sounds/.

Run once to (re)bake the bundled audio cues. They're shipped in the
repo so users never have to wait for runtime tone synthesis. Each
file is a 22050 Hz, mono, 16-bit PCM WAV.

  start.wav — bright "ting"  (high C6 bell, fast decay)
  stop.wav  — mid    "tong"  (G5 bell, fast decay)
  done.wav  — warm   "tung"  (E5 bell, slightly longer tail)
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 22050
OUT = Path(__file__).resolve().parent.parent / "voxtype" / "resources" / "sounds"


def _bell(freq_hz: float, *,
           dur_sec: float = 1.0,
           amplitude: float = 0.55,
           harmonics: tuple[float, ...] = (1.0, 0.55, 0.22, 0.08),
           decay_tau: float = 0.22) -> np.ndarray:
    """A single percussive bell tone: instant attack, exponential decay
    over the full `dur_sec` so the WAV reads as exactly one second
    even though most of the energy is in the first ~300 ms.

    `decay_tau` (seconds) controls how fast the note dies away — small
    values give a tight "ting", larger values give a longer "tung".
    """
    n = int(SR * dur_sec)
    t = np.arange(n, dtype=np.float32) / SR

    # Inharmonic partials produce bell-like character (each harmonic gets
    # its own envelope so high harmonics die faster than the fundamental
    # — exactly how real bells decay).
    wave_buf = np.zeros(n, dtype=np.float32)
    for k, gain in enumerate(harmonics, start=1):
        partial_tau = decay_tau / (1 + 0.6 * (k - 1))
        envelope_k = np.exp(-t / partial_tau)
        wave_buf += gain * envelope_k * np.sin(
            2 * np.pi * (freq_hz * k) * t,
        ).astype(np.float32)

    # Tiny attack ramp (3 ms) — prevents an audible click on the leading
    # edge but keeps the percussive feel.
    attack = max(1, int(SR * 0.003))
    wave_buf[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)

    # Normalise to target amplitude (the harmonic stack can sum > 1).
    peak = float(np.max(np.abs(wave_buf))) or 1.0
    wave_buf *= amplitude / peak
    return wave_buf


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16.tobytes())


# Single notes — bell-like, decay over ~1 s, no arpeggios.
C6 = 1046.50  # high  → "ting"
G5 =  783.99  # mid   → "tong"
E5 =  659.25  # warm  → "tung"

PRESETS: dict[str, dict] = {
    "start": {"freq_hz": C6, "decay_tau": 0.18},   # tight ting
    "stop":  {"freq_hz": G5, "decay_tau": 0.22},   # mid tong
    "done":  {"freq_hz": E5, "decay_tau": 0.32},   # longer tung
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, kwargs in PRESETS.items():
        path = OUT / f"{name}.wav"
        _write_wav(path, _bell(**kwargs))
        print(f"  wrote {path.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
