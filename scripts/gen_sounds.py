"""Generate three 1-second cue WAV files into voxtype/resources/sounds/.

Run once to (re)bake the bundled audio cues. They're shipped in the
repo so users never have to wait for runtime tone synthesis. Each
file is a 22050 Hz, mono, 16-bit PCM WAV.

  start.wav — ascending C-E-G arpeggio (records starts)
  stop.wav  — descending G-E-C arpeggio (record ends)
  done.wav  — two-note chime C6 + G5 (transcript pasted)
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 22050
OUT = Path(__file__).resolve().parent.parent / "voxtype" / "resources" / "sounds"


def _envelope(n: int, attack_ms: int = 8, release_ms: int = 60) -> np.ndarray:
    """Linear attack + exponential-ish release. Avoids clicks while
    keeping the tone bright on the attack."""
    env = np.ones(n, dtype=np.float32)
    a = max(1, int(SR * attack_ms / 1000))
    r = max(1, int(SR * release_ms / 1000))
    env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    # Exponential decay tail for a more chime-like feel
    tail = np.exp(-np.linspace(0.0, 5.0, r, dtype=np.float32))
    env[-r:] = np.minimum(env[-r:], tail)
    return env


def _tone(freq_hz: float, dur_sec: float, *,
           amplitude: float = 0.45,
           harmonics: tuple[float, ...] = (1.0, 0.35, 0.12),
           release_ms: int = 80) -> np.ndarray:
    """A note with harmonic richness, attack/release envelope."""
    n = int(SR * dur_sec)
    t = np.arange(n, dtype=np.float32) / SR
    wave_buf = np.zeros(n, dtype=np.float32)
    for k, gain in enumerate(harmonics, start=1):
        wave_buf += gain * np.sin(2 * np.pi * (freq_hz * k) * t).astype(np.float32)
    wave_buf /= max(harmonics)  # normalise so amplitude controls peak
    wave_buf *= amplitude * _envelope(n, release_ms=release_ms)
    return wave_buf


def _layout(notes: list[tuple[float, float]]) -> np.ndarray:
    """Concatenate (freq, duration) notes into a single 1-second buffer
    (or close to it — pad with silence to exactly 1 s)."""
    parts = [_tone(f, d) for f, d in notes]
    out = np.concatenate(parts)
    target = SR  # 1 second
    if len(out) < target:
        out = np.concatenate([out, np.zeros(target - len(out), dtype=np.float32)])
    else:
        out = out[:target]
    return out


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16.tobytes())


# Notes (just-intonation around C5 = 523.25 Hz so they sound musical)
C5, E5, G5, C6 = 523.25, 659.25, 783.99, 1046.50

PRESETS: dict[str, list[tuple[float, float]]] = {
    # Cheerful rising arpeggio — "we're listening"
    "start": [(C5, 0.16), (E5, 0.16), (G5, 0.55)],
    # Mirror — "we stopped listening"
    "stop":  [(G5, 0.16), (E5, 0.16), (C5, 0.55)],
    # Two-note ding — "all done"
    "done":  [(C6, 0.22), (G5, 0.70)],
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, notes in PRESETS.items():
        path = OUT / f"{name}.wav"
        _write_wav(path, _layout(notes))
        print(f"  wrote {path.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
