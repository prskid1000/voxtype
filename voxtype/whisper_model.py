"""Whisper model catalog — values go straight into the
faster-whisper-server --model argument."""
from __future__ import annotations

WHISPER_MODELS = [
    ("Systran/faster-whisper-tiny",       "Tiny (fastest)"),
    ("Systran/faster-whisper-base",       "Base"),
    ("Systran/faster-whisper-small",      "Small (default)"),
    ("Systran/faster-whisper-medium",     "Medium"),
    ("Systran/faster-whisper-large-v3",   "Large v3 (best)"),
]
