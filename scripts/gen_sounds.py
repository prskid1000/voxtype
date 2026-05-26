"""Generate three snappy digital/mouse-click cue WAV files into voxtype/resources/sounds/.

Run once to (re)bake the bundled audio cues. Each file is a 22050 Hz, mono, 16-bit PCM WAV.
- start.wav — snappy high digital click (1500 Hz, fast decay)
- stop.wav  — snappy mid digital click (1000 Hz, fast decay)
- done.wav  — premium double digital click (1800 Hz + 2200 Hz, 60ms delay)
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 22050
OUT = Path(__file__).resolve().parent.parent / "voxtype" / "resources" / "sounds"


def _digital_click(freq_hz: float, decay_tau: float, dur_sec: float = 1.0, amplitude: float = 0.5) -> np.ndarray:
    """A single snappy digital click: instant attack, extremely fast exponential decay."""
    n = int(SR * dur_sec)
    t = np.arange(n, dtype=np.float32) / SR

    # Fast exponential decay
    envelope = np.exp(-t / decay_tau)
    wave_buf = envelope * np.sin(2 * np.pi * freq_hz * t)

    # Add a high-frequency snap transient at the very beginning (first 5ms)
    transient_len = int(SR * 0.005)
    if transient_len < n:
        snap = np.sin(2 * np.pi * 3500.0 * t[:transient_len]) * np.exp(-t[:transient_len] / 0.0015)
        wave_buf[:transient_len] += 0.4 * snap

    # Snappy 1ms attack ramp to avoid harsh pop but keep the percussive attack
    attack = max(1, int(SR * 0.001))
    wave_buf[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)

    # Normalise
    peak = float(np.max(np.abs(wave_buf))) or 1.0
    wave_buf *= amplitude / peak
    return wave_buf


def _double_click(freq1: float, freq2: float, delay_sec: float = 0.06, decay_tau: float = 0.012, dur_sec: float = 1.0, amplitude: float = 0.5) -> np.ndarray:
    """Double digital click with a short delay in between to sound like a clean double-tap chime."""
    n = int(SR * dur_sec)
    samples = np.zeros(n, dtype=np.float32)

    # First click
    click1 = _digital_click(freq1, decay_tau, dur_sec=dur_sec, amplitude=amplitude)
    samples += click1

    # Second click
    delay_samples = int(SR * delay_sec)
    if delay_samples < n:
        click2 = _digital_click(freq2, decay_tau, dur_sec=dur_sec - delay_sec, amplitude=amplitude)
        samples[delay_samples:delay_samples + len(click2)] += click2

    # Normalise
    peak = float(np.max(np.abs(samples))) or 1.0
    samples *= amplitude / peak
    return samples


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16.tobytes())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. Start: Snappy digital high click
    start_samples = _digital_click(1500.0, decay_tau=0.012)
    _write_wav(OUT / "start.wav", start_samples)
    print(f"  wrote {OUT / 'start.wav'}")

    # 2. Stop: Snappy digital mid click
    stop_samples = _digital_click(1000.0, decay_tau=0.015)
    _write_wav(OUT / "stop.wav", stop_samples)
    print(f"  wrote {OUT / 'stop.wav'}")

    # 3. Done: Snappy double digital click
    done_samples = _double_click(1800.0, 2200.0, delay_sec=0.06, decay_tau=0.012)
    _write_wav(OUT / "done.wav", done_samples)
    print(f"  wrote {OUT / 'done.wav'}")


if __name__ == "__main__":
    main()
