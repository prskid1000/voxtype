"""Embedded OpenAI-compatible HTTP server.

Exposes the in-process STT + TTS engines so external clients (telecode,
MCP tools, anything that speaks OpenAI's audio API) can talk to VoxType
over standard HTTP.

Routes:
    POST /v1/audio/transcriptions   — STT (multipart upload)
    POST /v1/audio/speech           — TTS (JSON in, WAV out)
    GET  /v1/models                 — list loaded engines
    GET  /health                    — engine readiness snapshot
    GET  /                          — tiny "alive" probe

The hot path inside VoxType (`stt.py` → main pipeline) calls the engines
DIRECTLY — this server is for external clients only. So we don't pay
HTTP overhead on every dictation.

Single port (default 6600) serves both STT and TTS — same base URL,
different routes, identical to how OpenAI's API works.
"""
from __future__ import annotations

import asyncio
import io
import logging
import struct
import wave
from typing import Any

from aiohttp import web

from voxtype import stt_engine, tts_engine

log = logging.getLogger("voxtype.server")


# ── Helpers ──────────────────────────────────────────────────────────

def _wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Decode a WAV blob to raw int16 PCM at its native sample rate.
    Returns (pcm_bytes, sample_rate). The engine resamples to 16 kHz
    via the conversion below if needed."""
    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    # Convert to mono int16 if needed.
    if sw == 2 and nch == 1:
        pcm = frames
    else:
        import numpy as np
        if sw == 1:
            arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) << 8
        elif sw == 2:
            arr = np.frombuffer(frames, dtype=np.int16)
        elif sw == 4:
            arr = (np.frombuffer(frames, dtype=np.int32) >> 16).astype(np.int16)
        else:
            raise web.HTTPBadRequest(reason=f"unsupported sample width: {sw}")
        if nch > 1:
            arr = arr.reshape(-1, nch).mean(axis=1).astype(np.int16)
        pcm = arr.tobytes()
    return pcm, sr


def _resample_to_16k(pcm: bytes, src_sr: int) -> bytes:
    """Cheap linear resample to 16 kHz mono int16. The STT engine
    accepts 16 kHz audio directly without further preprocessing."""
    if src_sr == 16000:
        return pcm
    import numpy as np
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if arr.size == 0:
        return pcm
    n_out = int(round(arr.size * 16000 / src_sr))
    if n_out <= 0:
        return b""
    x_old = np.linspace(0.0, 1.0, num=arr.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    out = np.interp(x_new, x_old, arr).astype(np.int16)
    return out.tobytes()


def _decode_audio(blob: bytes, filename: str) -> bytes:
    """Best-effort decode of an uploaded audio file to 16 kHz mono int16 PCM.
    WAV is decoded natively; anything else falls through to soundfile."""
    name = (filename or "").lower()
    if name.endswith(".wav") or blob[:4] == b"RIFF":
        pcm, sr = _wav_to_pcm16(blob)
        return _resample_to_16k(pcm, sr)
    # Non-WAV: use soundfile (pulls in libsndfile via the bundled wheel).
    try:
        import numpy as np
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(blob), dtype="int16")
        if data.ndim > 1:
            data = data.mean(axis=1).astype("int16")
        return _resample_to_16k(data.tobytes(), int(sr))
    except Exception as exc:
        raise web.HTTPBadRequest(
            reason=f"could not decode audio (install soundfile for non-WAV uploads): {exc}"
        )


# ── Routes ───────────────────────────────────────────────────────────

async def handle_transcribe(request: web.Request) -> web.Response:
    """POST /v1/audio/transcriptions — OpenAI-compatible STT.

    Accepts multipart form with:
      file:             audio blob (WAV/MP3/OGG/M4A/...)
      model:            accepted but ignored. VoxType controls the model
                        through its own settings — external clients
                        only address the server by host:port.
      language:         ISO code, default "en"
      response_format:  "json" (default) or "text"
    """
    try:
        reader = await request.multipart()
    except Exception as exc:
        raise web.HTTPBadRequest(reason=f"invalid multipart: {exc}")
    audio_bytes = b""
    filename = "audio.wav"
    language = ""
    response_format = "json"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            filename = part.filename or filename
            audio_bytes = await part.read(decode=False)
        elif part.name == "language":
            language = (await part.text()).strip()
        elif part.name == "response_format":
            response_format = (await part.text()).strip() or "json"
        else:
            # Drain unknown parts so the reader advances. Includes
            # `model` — accepted for OpenAI API compatibility but
            # ignored (VoxType picks the model from its settings).
            await part.read(decode=False)
    if not audio_bytes:
        raise web.HTTPBadRequest(reason="missing 'file' field")
    try:
        pcm = _decode_audio(audio_bytes, filename)
    except web.HTTPException:
        raise
    except Exception as exc:
        log.error("transcribe: decode failed: %s", exc)
        raise web.HTTPBadRequest(reason=f"audio decode failed: {exc}")
    try:
        text = await stt_engine.get_engine().transcribe(pcm, language or None)
    except Exception as exc:
        log.error("transcribe: engine failed: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)
    if response_format == "text":
        return web.Response(text=text, content_type="text/plain")
    return web.json_response({"text": text})


def _wav_header(sample_rate: int, total_samples: int = 0) -> bytes:
    """Minimal RIFF/WAVE header for 16-bit mono PCM.
    `total_samples=0` is the streaming sentinel — the data and RIFF
    sizes are written as 0xFFFFFFFF so most players accept the stream
    even though length is unknown ahead of time."""
    n_ch = 1
    bps = 16
    byte_rate = sample_rate * n_ch * (bps // 8)
    block_align = n_ch * (bps // 8)
    data_bytes = total_samples * n_ch * (bps // 8)
    riff_size = 36 + data_bytes if total_samples else 0xFFFFFFFF
    data_size = data_bytes if total_samples else 0xFFFFFFFF
    return (
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, n_ch, sample_rate, byte_rate, block_align, bps)
        + b"data" + struct.pack("<I", data_size)
    )


async def handle_speech(request: web.Request) -> web.Response:
    """POST /v1/audio/speech — OpenAI-compatible TTS.

    Accepts JSON body with:
      model:           accepted but ignored. VoxType controls the model
                       through its own settings — external clients only
                       address the server by host:port.
      input:           text to synthesize
      voice:           accepted but ignored. The TTS voice is configured
                       in VoxType settings (`tts_speaker`).
      speed:           float, default 1.0 (>1 = faster)
      response_format: "wav" (default; we serve WAV natively)
      stream:          bool. If true (or settings.tts_stream is true),
                       reply with chunked WAV — first audio in ~200 ms.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(reason=f"invalid JSON: {exc}")
    text = str(body.get("input") or "").strip()
    if not text:
        raise web.HTTPBadRequest(reason="missing 'input'")
    speed_val: Any = body.get("speed")
    try:
        speed = float(speed_val) if speed_val is not None else None
    except (TypeError, ValueError):
        speed = None

    engine = tts_engine.get_engine()
    want_stream = bool(body.get("stream", engine.stream_default))

    if want_stream:
        try:
            await engine.ensure_loaded()
        except Exception as exc:
            log.error("speech: load failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "audio/wav", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        await resp.write(_wav_header(engine.sample_rate, total_samples=0))
        try:
            async for pcm in engine.synthesize_pcm_chunks(text, speed=speed):
                await resp.write(pcm)
        except Exception as exc:
            log.error("speech stream: engine failed: %s", exc)
        await resp.write_eof()
        return resp

    try:
        wav_bytes = await engine.synthesize(text, speed=speed)
    except Exception as exc:
        log.error("speech: engine failed: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)
    return web.Response(body=wav_bytes, content_type="audio/wav")


async def handle_models(_request: web.Request) -> web.Response:
    """GET /v1/models — minimal OpenAI-shape listing."""
    items: list[dict] = []
    stt = stt_engine.get_engine()
    items.append({
        "id": "whisper-1",
        "object": "model",
        "owned_by": "voxtype",
        "ready": stt.get_status().ready,
    })
    tts = tts_engine.get_engine()
    items.append({
        "id": "tts-1",
        "object": "model",
        "owned_by": "voxtype",
        "ready": tts.get_status().ready,
    })
    return web.json_response({"object": "list", "data": items})


async def handle_health(_request: web.Request) -> web.Response:
    stt = stt_engine.get_engine().get_status()
    tts = tts_engine.get_engine().get_status()
    return web.json_response({
        "status": "ok",
        "stt": {"ready": stt.ready, "running": stt.running, "error": stt.last_error},
        "tts": {"ready": tts.ready, "running": tts.running, "error": tts.last_error},
    })


async def handle_root(_request: web.Request) -> web.Response:
    return web.Response(text="VoxType — OpenAI-compatible STT/TTS\n",
                         content_type="text/plain")


# ── Lifecycle ────────────────────────────────────────────────────────

_RUNNER: web.AppRunner | None = None
_SITE: web.TCPSite | None = None


def build_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)  # 64 MB upload cap
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/audio/transcriptions", handle_transcribe)
    app.router.add_post("/v1/audio/speech", handle_speech)
    return app


async def start(host: str = "127.0.0.1", port: int = 6600) -> None:
    """Start the embedded server. Idempotent — calling twice is a no-op."""
    global _RUNNER, _SITE
    if _RUNNER is not None:
        return
    app = build_app()
    _RUNNER = web.AppRunner(app, access_log=None)
    await _RUNNER.setup()
    _SITE = web.TCPSite(_RUNNER, host=host, port=port)
    await _SITE.start()
    log.info("voxtype HTTP server listening on http://%s:%d", host, port)


async def stop() -> None:
    global _RUNNER, _SITE
    if _SITE is not None:
        try:
            await _SITE.stop()
        except Exception:
            pass
        _SITE = None
    if _RUNNER is not None:
        try:
            await _RUNNER.cleanup()
        except Exception:
            pass
        _RUNNER = None


def is_running() -> bool:
    return _RUNNER is not None
