"""Settings + UI state types. Single source of truth.

STT and TTS both run in-process via PyTorch. An embedded HTTP server
(single port, default 6600) exposes them to external clients via
OpenAI-compatible endpoints — see voxtype/server.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

PillState = Literal["idle", "recording", "processing", "enhancing", "typing", "error"]
HotkeyMode = Literal["hold", "toggle"]
# torch device preference. Used by both STT and TTS engines.
TorchDevice = Literal["cpu", "cuda"]
# Pluggable engine backend names. Real list comes from
# voxtype.backends.{stt,tts}_backend_names() at runtime — these literals
# are just for type-hint clarity. New entries land here whenever
# additional backends are registered in voxtype/backends/__init__.py.
STTBackendName = Literal["whisper"]
TTSBackendName = Literal["kokoro"]
# Inference precision. `auto` = fp16 on CUDA, fp32 on CPU. `bf16` needs
# Ampere+ (RTX 30xx / A100+) — same speed as fp16, wider numeric range.
TorchDtype = Literal["auto", "fp32", "fp16", "bf16"]
# Whisper task. `translate` outputs English regardless of source language.
STTTask = Literal["transcribe", "translate"]


@dataclass
class HotkeyCombo:
    """One or two keys that must be held together to activate the hotkey.

    Keys use pynput-style string names (e.g. "ctrl", "cmd", "f9") so we don't
    carry the Windows/uiohook numeric keycode over. `label` is human-readable."""
    key1: str = "ctrl"
    key2: str | None = "cmd"
    label: str = "Ctrl + Win"


@dataclass
class AppSettings:
    # ── Recording behavior ───────────────────────────────────────────
    hotkey_mode: HotkeyMode = "hold"
    hotkey: HotkeyCombo = field(default_factory=HotkeyCombo)
    auto_stop_on_silence: bool = True
    silence_duration_sec: float = 1.5   # seconds of continuous silence
                                         # before the recorder auto-stops
    vad_enabled: bool = True
    append_mode: bool = False

    # ── Pill UI position (-1 = unset → center-bottom) ────────────────
    pill_x: int = -1
    pill_y: int = -1
    pill_hidden: bool = False

    # ── Embedded HTTP server (serves both STT + TTS) ─────────────────
    # Default 6600 — external clients reach VoxType through this port
    # via OpenAI-compatible routes.
    server_enabled: bool = True
    server_port: int = 6600

    # ── STT (in-process via transformers + torch) ───────────────────
    # `stt_model_path` accepts a HF repo ID (auto-downloaded) or a local
    # path. Default = `openai/whisper-base`: 99-language multilingual,
    # ~145 MB on disk. Any HF Whisper-family repo works (distilled,
    # large, fine-tunes). Empty field falls back to DEFAULT_MODEL in
    # stt_engine.
    stt_enabled: bool = True
    stt_auto_start: bool = True
    stt_idle_unload_sec: int = 300
    # Pluggable backend. "whisper" = HuggingFace transformers (default,
    # broadest feature set). "faster-whisper" = CTranslate2, ~4× faster
    # on GPU with int8 CPU mode.
    stt_backend: STTBackendName = "whisper"
    stt_model_path: str = "openai/whisper-base"
    stt_device: TorchDevice = "cpu"
    stt_language: str = "en"
    # Whisper task. `translate` → English; `transcribe` → source language.
    stt_task: STTTask = "transcribe"
    # Override torch dtype. `auto` picks fp16/CUDA, fp32/CPU.
    stt_dtype: TorchDtype = "auto"
    # Beam-search width. 1 = greedy (fastest). Higher = slower but lower WER.
    stt_num_beams: int = 1
    # Domain-bias prompt fed to the decoder (jargon, names, codes).
    stt_initial_prompt: str = ""
    # Run a dummy 1-sec inference right after load so the FIRST real
    # hotkey press isn't slow (CUDA kernel autotune, etc.).
    stt_warmup: bool = True
    # torch.compile(model) after load. ~20-40% steady-state speedup, but
    # adds ~30 s to the first inference (one-time JIT) and can break with
    # exotic models. Safe to leave off unless you transcribe constantly.
    stt_torch_compile: bool = False

    # ── TTS (in-process via kokoro PyPI package + torch) ────────────
    # `tts_model_path` accepts a HF repo ID. Default = `hexgrad/Kokoro-82M`:
    # 54 voices across 9 language families (American + British English,
    # Spanish, French, Hindi, Italian, Japanese, Brazilian Portuguese,
    # Mandarin Chinese), ~327 MB on disk.
    # `tts_speaker` is a voice NAME (string), not an index. Examples:
    #   af_heart, am_adam, bf_emma, bm_george, jm_kumo, zf_xiaobei.
    tts_enabled: bool = False
    tts_auto_start: bool = False
    tts_idle_unload_sec: int = 600
    # Pluggable backend. "kokoro" = official kokoro PyTorch (default,
    # 54 voices, 9 langs). "piper" = ONNX-based, ~150 voices in 30+ langs,
    # tiny memory footprint.
    tts_backend: TTSBackendName = "kokoro"
    tts_model_path: str = "hexgrad/Kokoro-82M"
    tts_device: TorchDevice = "cpu"
    tts_speaker: str = "af_heart"
    tts_length_scale: float = 1.0      # Kokoro `speed` arg (1.0 = normal)
    # First-call warmup — same idea as STT.
    tts_warmup: bool = True
    # torch.compile(model) — Kokoro is small so the win is smaller (~15%)
    # but the first-synth penalty is also lower.
    tts_torch_compile: bool = False
    # Stream WAV chunks back as Kokoro yields per-sentence audio.
    # Drops time-to-first-audio from ~full utterance to ~200 ms.
    tts_stream: bool = False

    # ── LLM enhancement (via telecode proxy) ─────────────────────────
    enhance_enabled: bool = True
    screen_context: bool = True
    proxy_url: str = "http://127.0.0.1:1235"
    proxy_model: str = "qwen3.5-35b"

    # ── History ──────────────────────────────────────────────────────
    save_history: bool = True

    # ── Serialization helpers ────────────────────────────────────────
    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "AppSettings":
        hk = d.get("hotkey") or {}
        settings = cls(
            hotkey=HotkeyCombo(
                key1=hk.get("key1", "ctrl"),
                key2=hk.get("key2"),
                label=hk.get("label", "Ctrl + Win"),
            ),
        )
        # Copy remaining fields (skip hotkey — handled above)
        for key, value in d.items():
            if key == "hotkey":
                continue
            if hasattr(settings, key):
                setattr(settings, key, value)
        return settings


def server_url(s: AppSettings) -> str:
    """Base URL for the embedded STT/TTS server."""
    return f"http://127.0.0.1:{s.server_port}"
