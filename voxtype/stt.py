"""Whisper STT — multipart POST to faster-whisper-server.

We record raw 16 kHz mono int16 PCM via sounddevice, wrap in a WAV
header, and upload to /v1/audio/transcriptions. The server accepts
any audio mime — we declare audio/wav for correctness. Response is
JSON ({'text': ...})."""
from __future__ import annotations

import io
import logging
import struct
from typing import Optional

import aiohttp

log = logging.getLogger("voxtype.stt")

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2  # int16


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE,
               channels: int = CHANNELS) -> bytes:
    """Wrap raw 16-bit PCM in a minimal WAV header."""
    data_size = len(pcm)
    byte_rate = sample_rate * channels * BYTES_PER_SAMPLE
    block_align = channels * BYTES_PER_SAMPLE
    header = b"".join([
        b"RIFF",
        struct.pack("<I", 36 + data_size),
        b"WAVE",
        b"fmt ",
        struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                    byte_rate, block_align, BYTES_PER_SAMPLE * 8),
        b"data",
        struct.pack("<I", data_size),
    ])
    return header + pcm


def silent_wav() -> bytes:
    """100 ms of silence. Used to preload the Whisper model."""
    n = SAMPLE_RATE // 10
    return pcm_to_wav(b"\x00\x00" * n)


async def transcribe(pcm: bytes, whisper_url: str,
                     language: str = "en",
                     timeout: float = 60.0) -> str:
    """POST the audio to /v1/audio/transcriptions. Returns the text;
    raises on non-200."""
    wav = pcm_to_wav(pcm)
    url = whisper_url.rstrip("/") + "/v1/audio/transcriptions"

    form = aiohttp.FormData()
    form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", "whisper-1")
    form.add_field("language", language)
    form.add_field("response_format", "json")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as session:
        async with session.post(url, data=form) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Whisper STT error {resp.status}: {body[:400]}")
            # Server may return JSON {"text": "..."} or plain text
            try:
                import json
                return str(json.loads(body).get("text", "")).strip()
            except Exception:
                return body.strip()


async def preload(whisper_url: str) -> None:
    """Warm up the Whisper model. Swallows errors — preload is best-effort."""
    try:
        await transcribe(b"\x00\x00" * (SAMPLE_RATE // 10), whisper_url)
        log.info("whisper preloaded")
    except Exception as exc:
        log.info("whisper preload failed (non-fatal): %s", exc)
