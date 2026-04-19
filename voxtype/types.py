"""Settings + UI state types. Single source of truth — ported from
voxtype/src/shared/types.ts with LM Studio fields replaced by telecode
proxy endpoints."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

PillState = Literal["idle", "recording", "processing", "enhancing", "typing", "error"]
HotkeyMode = Literal["hold", "toggle"]
DeviceMode = Literal["gpu", "cpu"]


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
    vad_enabled: bool = True
    append_mode: bool = False

    # ── Pill UI position (-1 = unset → center-bottom) ────────────────
    pill_x: int = -1
    pill_y: int = -1

    # ── Whisper STT (child process) ──────────────────────────────────
    whisper_enabled: bool = True
    whisper_port: int = 6600
    whisper_model: str = "Systran/faster-whisper-small"
    whisper_device: DeviceMode = "gpu"

    # ── Kokoro TTS (child process, off by default) ───────────────────
    kokoro_enabled: bool = False
    kokoro_port: int = 6500
    kokoro_voice: str = "af_sky"
    kokoro_device: DeviceMode = "gpu"

    # ── LLM enhancement (via telecode proxy) ─────────────────────────
    # telecode proxy accepts OpenAI-shape POST /v1/chat/completions and
    # routes to whichever local model it supervises. VoxType no longer
    # manages the model itself.
    enhance_enabled: bool = True
    screen_context: bool = True
    proxy_url: str = "http://127.0.0.1:1235"
    proxy_model: str = "qwen3.5-35b"

    # ── History ──────────────────────────────────────────────────────
    save_history: bool = True

    # ── Serialization helpers ────────────────────────────────────────
    def to_json(self) -> dict:
        d = asdict(self)
        return d

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


def whisper_url(s: AppSettings) -> str:
    return f"http://127.0.0.1:{s.whisper_port}"


def kokoro_url(s: AppSettings) -> str:
    return f"http://127.0.0.1:{s.kokoro_port}"
