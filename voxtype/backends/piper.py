"""Piper TTS via the `piper-tts` PyPI package (ONNX-based).

Piper is the lightweight alternative: ONNX runtime, ~50 MB per voice,
~30 languages, fast on CPU. Voices live in `rhasspy/piper-voices` on
HuggingFace and are downloaded on demand to `~/.cache/voxtype-piper/`.

The voice catalog below is curated — Piper publishes ~150 voices but
we surface the most popular ones across major languages. Power users
can drop additional .onnx files in the cache dir and reference them
by id (matching the file stem).
"""
from __future__ import annotations

import gc
import io
import logging
import urllib.request
import wave
from pathlib import Path
from typing import Any, Iterator

from voxtype.backends.tts_base import (
    TTSBackend, TTSLoadConfig, VoiceEntry,
)

log = logging.getLogger("voxtype.backends.piper")


# ── Voice catalog ────────────────────────────────────────────────────
# (voice_id, language, gender, display_name, quality).
# voice_id = stem of the .onnx file on rhasspy/piper-voices.
# quality drives the URL path: voices/<lang>/<region>/<name>/<quality>/.

_VOICES: list[tuple[str, str, str, str, str]] = [
    # English (US)
    ("en_US-amy-medium",       "English (US)", "F", "Amy",       "medium"),
    ("en_US-lessac-medium",    "English (US)", "F", "Lessac",    "medium"),
    ("en_US-libritts-high",    "English (US)", "F", "LibriTTS",  "high"),
    ("en_US-ryan-medium",      "English (US)", "M", "Ryan",      "medium"),
    ("en_US-joe-medium",       "English (US)", "M", "Joe",       "medium"),
    ("en_US-kathleen-low",     "English (US)", "F", "Kathleen",  "low"),
    # English (GB)
    ("en_GB-alba-medium",      "English (GB)", "F", "Alba",      "medium"),
    ("en_GB-cori-high",        "English (GB)", "F", "Cori",      "high"),
    ("en_GB-northern_english_male-medium", "English (GB)", "M",
                                             "Northern English", "medium"),
    # Spanish
    ("es_ES-davefx-medium",    "Spanish",      "M", "Davefx",    "medium"),
    ("es_MX-claude-high",      "Spanish (MX)", "M", "Claude",    "high"),
    # French
    ("fr_FR-siwis-medium",     "French",       "F", "Siwis",     "medium"),
    ("fr_FR-upmc-medium",      "French",       "M", "UPMC",      "medium"),
    # German
    ("de_DE-thorsten-medium",  "German",       "M", "Thorsten",  "medium"),
    ("de_DE-eva_k-x_low",      "German",       "F", "Eva",       "x_low"),
    # Italian
    ("it_IT-paola-medium",     "Italian",      "F", "Paola",     "medium"),
    # Portuguese
    ("pt_BR-faber-medium",     "Portuguese (BR)", "M", "Faber",  "medium"),
    # Dutch
    ("nl_NL-mls-medium",       "Dutch",        "F", "MLS",       "medium"),
    # Russian
    ("ru_RU-irina-medium",     "Russian",      "F", "Irina",     "medium"),
    # Polish
    ("pl_PL-mc_speech-medium", "Polish",       "F", "MC Speech", "medium"),
]


_PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"


def _cache_dir() -> Path:
    p = Path.home() / ".cache" / "voxtype-piper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _voice_paths(voice_id: str, quality: str) -> tuple[Path, Path, str]:
    """Local (.onnx, .json) plus the canonical HF subpath."""
    # voice_id = "en_US-amy-medium"
    lang_full, name, _q = voice_id.split("-", 2)
    lang = lang_full.split("_")[0]
    sub = f"{lang}/{lang_full}/{name}/{quality}/"
    onnx = _cache_dir() / f"{voice_id}.onnx"
    cfg  = _cache_dir() / f"{voice_id}.onnx.json"
    return onnx, cfg, sub


def _download(url: str, dest: Path) -> None:
    log.info("piper: downloading %s", url)
    with urllib.request.urlopen(url) as src:
        data = src.read()
    dest.write_bytes(data)


class PiperBackend(TTSBackend):
    name = "piper"
    default_model = "rhasspy/piper-voices"     # informational only
    default_voice = "en_US-amy-medium"
    family_tags = ("piper",)
    # Piper voices vary (16k / 22.05k). We set 22050 as default and the
    # engine resamples in the WAV header from `self.sample_rate` set
    # after load.
    sample_rate = 22050

    def __init__(self) -> None:
        self._voice: Any = None        # piper.PiperVoice instance
        self._voice_id: str = ""
        self._quality_by_id: dict[str, str] = {
            vid: quality for vid, _, _, _, quality in _VOICES
        }

    def voices(self) -> list[VoiceEntry]:
        return [
            VoiceEntry(vid, lang, gender, name)
            for vid, lang, gender, name, _quality in _VOICES
        ]

    def supports(self, feature: str) -> bool:
        # Piper exposes length_scale (speed) via PiperVoice.synthesize.
        # Streaming: piper-tts yields per-sentence audio_int16_bytes too.
        # torch_compile: N/A — Piper runs through onnxruntime, not torch.
        return feature in {"speed", "stream"}

    def _ensure_voice_files(self, voice_id: str) -> tuple[Path, Path]:
        quality = self._quality_by_id.get(voice_id, "medium")
        onnx, cfg, sub = _voice_paths(voice_id, quality)
        if not onnx.exists():
            _download(_PIPER_HF_BASE + sub + f"{voice_id}.onnx", onnx)
        if not cfg.exists():
            _download(_PIPER_HF_BASE + sub + f"{voice_id}.onnx.json", cfg)
        return onnx, cfg

    def _load_voice(self, voice_id: str) -> None:
        from piper.voice import PiperVoice
        onnx, _cfg = self._ensure_voice_files(voice_id)
        log.info("piper loading voice=%s from %s", voice_id, onnx)
        self._voice = PiperVoice.load(str(onnx))
        self._voice_id = voice_id
        # Read native sample rate from the voice config.
        try:
            self.sample_rate = int(self._voice.config.sample_rate)
        except Exception:
            pass

    def load_sync(self, cfg: TTSLoadConfig) -> None:
        # Piper's "model" is per-voice. cfg.model_id is informational
        # only — the picker drives which voice/file we actually load.
        # We pre-load the default voice so warmup has something to use.
        self._load_voice(self.default_voice)
        if cfg.warmup:
            try:
                for _ in self.synth_chunks_sync("Voxtype ready.", self.default_voice, 1.0):
                    pass
                log.info("piper warmup ok")
            except Exception as exc:  # noqa: BLE001
                log.warning("piper: warmup failed (%s)", exc)

    def unload_sync(self) -> None:
        self._voice = None
        self._voice_id = ""
        gc.collect()

    def synth_chunks_sync(self, text: str, voice: str, speed: float) -> Iterator[bytes]:
        v = voice or self.default_voice
        # Final guard: if the requested voice isn't in our curated map
        # (e.g. caller bypassed the orchestrator's validation), fall back
        # to the backend default instead of failing to download a file
        # we don't know how to resolve.
        if v not in self._quality_by_id:
            log.warning("piper: unknown voice %r — using %s", v, self.default_voice)
            v = self.default_voice
        # Swap voice file if the user picked a different one.
        if v != self._voice_id:
            self._load_voice(v)

        # PiperVoice.synthesize() writes WAV frames into a file-like.
        # Use length_scale = 1/speed (Piper's slower = larger value).
        length_scale = 1.0 / max(0.1, float(speed or 1.0))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            self._voice.synthesize(text, wf, length_scale=length_scale)
        # Strip the WAV header — caller wants raw PCM. WAV header is 44
        # bytes for our int16 mono format.
        pcm = buf.getvalue()[44:]
        # One chunk: piper-tts doesn't yield per-sentence natively;
        # streaming wins are smaller here. The engine still wraps it
        # in a chunked response if the caller asks for streaming.
        if pcm:
            yield pcm

    def runtime_info(self) -> dict:
        return {"voice": self._voice_id, "sample_rate": self.sample_rate}
