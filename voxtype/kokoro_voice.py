"""Kokoro TTS voice catalog + warmup ping.

Same curated list as voxtype/src/main/kokoro-voice.ts. `preload()` sends
a one-word TTS request so the first user-triggered speak isn't cold."""
from __future__ import annotations

import json
import logging

import aiohttp

log = logging.getLogger("voxtype.kokoro")

FEATURED_VOICES = [
    ("af_sky",     "Sky (F, American)"),
    ("af_heart",   "Heart (F, American)"),
    ("af_bella",   "Bella (F, American)"),
    ("af_nova",    "Nova (F, American)"),
    ("af_sarah",   "Sarah (F, American)"),
    ("af_nicole",  "Nicole (F, American)"),
    ("af_jessica", "Jessica (F, American)"),
    ("am_adam",    "Adam (M, American)"),
    ("am_michael", "Michael (M, American)"),
    ("am_eric",    "Eric (M, American)"),
    ("am_liam",    "Liam (M, American)"),
    ("bf_emma",    "Emma (F, British)"),
    ("bf_alice",   "Alice (F, British)"),
    ("bm_george",  "George (M, British)"),
    ("bm_daniel",  "Daniel (M, British)"),
]


async def preload(port: int, voice: str, timeout: float = 30.0) -> None:
    """Warm up the Kokoro model by synthesising a tiny utterance.
    Swallows all errors — preload is best-effort."""
    url = f"http://127.0.0.1:{port}/v1/audio/speech"
    payload = {"model": "kokoro", "input": "ok", "voice": voice}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.post(url, json=payload) as resp:
                await resp.read()
                if resp.status == 200:
                    log.info("kokoro preloaded")
                else:
                    log.info("kokoro preload returned %d", resp.status)
    except Exception as exc:
        log.info("kokoro preload failed: %s", exc)
