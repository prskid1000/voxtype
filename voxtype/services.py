"""Subprocess supervisor for Whisper and Kokoro sidecars.

Ported from voxtype/src/main/services.ts. Spawns faster-whisper-server
and Kokoro-FastAPI as child processes, probes their /health endpoints,
auto-restarts on crash with exponential backoff. Graceful `taskkill /T`
first, then force kill if needed.

Windows-specific: CREATE_NO_WINDOW so children don't pop a console."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

import aiohttp

log = logging.getLogger("voxtype.services")

ServiceName = Literal["whisper", "kokoro"]
DeviceMode = Literal["gpu", "cpu"]


INSTALL_DIR = Path(os.path.expanduser("~")) / ".voxtype"
STT_VENV    = INSTALL_DIR / "stt-venv"
TTS_VENV    = INSTALL_DIR / "tts-venv"
KOKORO_REPO = INSTALL_DIR / "Kokoro-FastAPI"


@dataclass
class WhisperConfig:
    model: str
    port: int
    device: DeviceMode = "gpu"


@dataclass
class KokoroConfig:
    port: int
    device: DeviceMode = "gpu"


@dataclass
class ServiceStatus:
    name: ServiceName
    pid: int | None
    running: bool
    ready: bool
    last_error: str = ""


@dataclass
class _Managed:
    name: ServiceName
    proc: subprocess.Popen | None = None
    ready: bool = False
    last_error: str = ""
    config: WhisperConfig | KokoroConfig | None = None
    stopping: bool = False
    restart_count: int = 0
    restart_task: asyncio.Task | None = None


_services: dict[ServiceName, _Managed] = {}
_status_listeners: list[Callable[[ServiceStatus], None]] = []
_lock = threading.Lock()


def on_status_change(fn: Callable[[ServiceStatus], None]) -> None:
    _status_listeners.append(fn)


def _health_url(m: _Managed) -> str:
    port = m.config.port if m.config else 0  # type: ignore[union-attr]
    return f"http://127.0.0.1:{port}/health"


def _notify(name: ServiceName) -> None:
    m = _services.get(name)
    if m is None:
        return
    s = ServiceStatus(
        name=name,
        pid=(m.proc.pid if m.proc and m.proc.poll() is None else None),
        running=bool(m.proc and m.proc.poll() is None),
        ready=m.ready,
        last_error=m.last_error,
    )
    for fn in _status_listeners:
        try:
            fn(s)
        except Exception:
            pass


# ── Binary paths ─────────────────────────────────────────────────────

def _whisper_exe() -> Path:
    return STT_VENV / "Scripts" / "faster-whisper-server.exe"


def _uvicorn_exe() -> Path:
    return TTS_VENV / "Scripts" / "uvicorn.exe"


# ── Spawn helpers ────────────────────────────────────────────────────

def _spawn_whisper(cfg: WhisperConfig) -> subprocess.Popen:
    env = os.environ.copy()
    if cfg.device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    args = [str(_whisper_exe()), cfg.model, "--host", "127.0.0.1",
            "--port", str(cfg.port)]
    return subprocess.Popen(
        args, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _spawn_kokoro(cfg: KokoroConfig) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8":      "1",
        "USE_GPU":         "true" if cfg.device == "gpu" else "false",
        "USE_ONNX":        "false",
        "PROJECT_ROOT":    str(KOKORO_REPO),
        "PYTHONPATH":      f"{KOKORO_REPO};{KOKORO_REPO / 'api'}",
        "MODEL_DIR":       "src/models",
        "VOICES_DIR":      "src/voices/v1_0",
        "WEB_PLAYER_PATH": str(KOKORO_REPO / "web"),
    })
    args = [str(_uvicorn_exe()), "api.src.main:app",
            "--host", "127.0.0.1", "--port", str(cfg.port)]
    return subprocess.Popen(
        args, env=env, cwd=str(KOKORO_REPO),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


# ── Health probe ─────────────────────────────────────────────────────

async def _ping_once(url: str, timeout: float = 1.5) -> bool:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(url) as resp:
                return resp.status < 500
    except Exception:
        return False


async def _wait_ready(name: ServiceName, url: str,
                      total_timeout: float = 60.0) -> bool:
    start = time.monotonic()
    attempts = 0
    while (time.monotonic() - start) < total_timeout:
        attempts += 1
        if await _ping_once(url):
            log.info("%s ready after %.1fs (%d attempts)",
                     name, time.monotonic() - start, attempts)
            return True
        await asyncio.sleep(0.5)
    log.warning("%s did not become ready in %.0fs", name, total_timeout)
    return False


# ── stdout/stderr drain ──────────────────────────────────────────────

def _drain(name: ServiceName, proc: subprocess.Popen) -> None:
    def _reader():
        try:
            if proc.stdout is None:
                return
            for line in iter(proc.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.info("[%s] %s", name, text)
        except Exception:
            pass
    t = threading.Thread(target=_reader, daemon=True,
                         name=f"voxtype-{name}-reader")
    t.start()


# ── Lifecycle ────────────────────────────────────────────────────────

async def _start_internal(m: _Managed) -> None:
    assert m.config is not None
    exe = _whisper_exe() if m.name == "whisper" else _uvicorn_exe()
    if not exe.exists():
        m.last_error = f"executable missing: {exe}"
        log.error("%s not installed (%s missing) — skipping start", m.name, exe)
        _notify(m.name)
        return
    if m.name == "kokoro" and not KOKORO_REPO.exists():
        m.last_error = f"Kokoro repo missing: {KOKORO_REPO}"
        log.error("kokoro repo missing (%s) — skipping start", KOKORO_REPO)
        _notify(m.name)
        return

    log.info("starting %s...", m.name)
    m.proc = (_spawn_whisper(m.config)  # type: ignore[arg-type]
              if m.name == "whisper"
              else _spawn_kokoro(m.config))  # type: ignore[arg-type]
    m.ready = False
    m.last_error = ""
    _drain(m.name, m.proc)
    _notify(m.name)
    log.info("%s spawned (PID %d)", m.name, m.proc.pid)

    # Watch for exit on a thread so we can trigger auto-restart.
    threading.Thread(
        target=_watch_exit, args=(m,), daemon=True,
        name=f"voxtype-{m.name}-watcher",
    ).start()

    ready = await _wait_ready(m.name, _health_url(m))
    m.ready = ready
    if not ready:
        m.last_error = "service did not become ready"
    _notify(m.name)


def _watch_exit(m: _Managed) -> None:
    """Block on proc.wait() and schedule auto-restart on unexpected exit."""
    if m.proc is None:
        return
    try:
        code = m.proc.wait()
    except Exception:
        code = -1
    log.info("%s exited (code=%s)", m.name, code)
    m.ready = False
    m.proc = None
    _notify(m.name)
    if m.stopping:
        m.stopping = False
        return

    m.restart_count += 1
    delay = min(30.0, 1.0 * (2 ** min(m.restart_count, 5)))
    log.info("%s crashed — restart #%d in %.1fs", m.name, m.restart_count, delay)

    def _later():
        time.sleep(delay)
        try:
            asyncio.run(_start_internal(m))
        except Exception as exc:
            log.error("%s restart failed: %s", m.name, exc)

    threading.Thread(target=_later, daemon=True,
                     name=f"voxtype-{m.name}-restart").start()


async def start_whisper(cfg: WhisperConfig) -> None:
    with _lock:
        m = _services.setdefault("whisper", _Managed(name="whisper"))
    if m.proc and m.proc.poll() is None:
        log.info("whisper already running")
        return
    m.config = cfg
    await _start_internal(m)


async def start_kokoro(cfg: KokoroConfig) -> None:
    with _lock:
        m = _services.setdefault("kokoro", _Managed(name="kokoro"))
    if m.proc and m.proc.poll() is None:
        log.info("kokoro already running")
        return
    m.config = cfg
    await _start_internal(m)


def _kill_tree(pid: int, force: bool) -> None:
    args = ["taskkill.exe", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(args, capture_output=True, timeout=5.0, check=False)
    except Exception:
        pass


async def _wait_exit(proc: subprocess.Popen, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return True
        await asyncio.sleep(0.1)
    return proc.poll() is not None


async def stop_service(name: ServiceName) -> None:
    m = _services.get(name)
    if m is None:
        return
    m.stopping = True
    m.restart_count = 0
    if m.proc is None or m.proc.poll() is not None:
        m.proc = None
        m.ready = False
        _notify(name)
        return
    pid = m.proc.pid
    log.info("stopping %s (PID %d)...", name, pid)
    _kill_tree(pid, force=False)
    if not await _wait_exit(m.proc, 3.0):
        log.info("%s did not exit gracefully — forceful kill", name)
        _kill_tree(pid, force=True)
        await _wait_exit(m.proc, 2.0)
    m.proc = None
    m.ready = False
    _notify(name)


async def restart_service(name: ServiceName,
                           new_cfg: WhisperConfig | KokoroConfig | None = None) -> None:
    m = _services.get(name)
    if new_cfg is not None and m is not None:
        m.config = new_cfg
    await stop_service(name)
    cfg = (m.config if m is not None else new_cfg)
    if cfg is None:
        log.warning("restart_service(%s): no config available", name)
        return
    if name == "whisper":
        await start_whisper(cfg)  # type: ignore[arg-type]
    else:
        await start_kokoro(cfg)  # type: ignore[arg-type]


async def stop_all() -> None:
    await asyncio.gather(*(stop_service(n) for n in list(_services.keys())))


def get_status(name: ServiceName) -> ServiceStatus:
    m = _services.get(name)
    return ServiceStatus(
        name=name,
        pid=(m.proc.pid if m and m.proc and m.proc.poll() is None else None),
        running=bool(m and m.proc and m.proc.poll() is None),
        ready=bool(m and m.ready),
        last_error=(m.last_error if m else ""),
    )


def is_running(name: ServiceName) -> bool:
    m = _services.get(name)
    return bool(m and m.proc and m.proc.poll() is None)
