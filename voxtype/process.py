"""Process / engine status facade.

VoxType runs STT and TTS in-process. This module exists as:
  1. A thin facade over `stt_engine` / `tts_engine` so the tray menu
     and settings window can use `process.get_status("stt")` style calls.
  2. Surviving low-level Windows utilities (Job Object, kill_process_tree,
     port sweep) that may be useful for future subprocesses but aren't
     wired to anything right now.

The actual model lifecycle (load / unload / idle watcher) lives in the
engine modules.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

log = logging.getLogger("voxtype.process")

ServiceName = Literal["stt", "tts"]


@dataclass
class ServiceStatus:
    name: ServiceName
    pid: int | None
    running: bool
    ready: bool
    last_error: str = ""


# ── Engine facade ────────────────────────────────────────────────────

def _engine_for(name: ServiceName):
    if name == "stt":
        from voxtype import stt_engine
        return stt_engine.get_engine()
    elif name == "tts":
        from voxtype import tts_engine
        return tts_engine.get_engine()
    raise ValueError(f"unknown service: {name}")


def get_status(name: ServiceName) -> ServiceStatus:
    eng = _engine_for(name)
    s = eng.get_status()
    return ServiceStatus(
        name=name,
        pid=None,  # no subprocess
        running=bool(s.running),
        ready=bool(s.ready),
        last_error=str(s.last_error or ""),
    )


def is_running(name: ServiceName) -> bool:
    return get_status(name).ready


def on_status_change(fn: Callable[[ServiceStatus], None]) -> None:
    """Subscribe to status events from BOTH engines."""
    from voxtype import stt_engine, tts_engine

    def _stt_proxy(es) -> None:
        fn(ServiceStatus("stt", None, es.running, es.ready, es.last_error))

    def _tts_proxy(es) -> None:
        fn(ServiceStatus("tts", None, es.running, es.ready, es.last_error))

    stt_engine.get_engine().on_status_change(_stt_proxy)
    tts_engine.get_engine().on_status_change(_tts_proxy)


def mark_used(_name: ServiceName) -> None:
    """No-op kept for call-site compatibility. The engines update their
    own `last_used` timestamps inside transcribe/synthesize."""
    return


# ── Lifecycle wrappers ───────────────────────────────────────────────

async def start_stt(s) -> None:
    """Load the STT model."""
    from voxtype import stt_engine
    eng = stt_engine.get_engine()
    await eng.configure(s)
    await eng.ensure_loaded()


async def start_tts(s) -> None:
    from voxtype import tts_engine
    eng = tts_engine.get_engine()
    await eng.configure(s)
    await eng.ensure_loaded()


async def stop_service(name: ServiceName) -> None:
    await _engine_for(name).unload()


async def restart_service(name: ServiceName, s) -> None:
    eng = _engine_for(name)
    await eng.unload()
    await eng.configure(s)
    await eng.ensure_loaded()


async def stop_all() -> None:
    from voxtype import stt_engine, tts_engine, server
    from voxtype.engine_host import get_host
    await asyncio.gather(
        stt_engine.get_engine().unload(),
        tts_engine.get_engine().unload(),
        server.stop(),
        return_exceptions=True,
    )
    # Terminate the torch worker subprocess so it doesn't outlive the GUI.
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, get_host().stop)
    except Exception:
        pass


# ── Kill-on-close Job Object (preserved for future subprocess use) ──

_JOB_HANDLE = None
_TRACKED_PIDS: set[int] = set()
_TRACKED_PROCS: list[subprocess.Popen] = []


def _create_kill_on_close_job():
    """Idempotent: create the process-wide Job Object on first call.

    Kept around so any future subprocess can inherit a kill-on-close
    lifetime bind on Windows."""
    global _JOB_HANDLE
    if os.name != "nt" or _JOB_HANDLE is not None:
        return _JOB_HANDLE
    try:
        import win32job
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        _JOB_HANDLE = job
        log.info("created kill-on-close Job Object")
    except Exception as exc:
        log.warning("could not create Job Object: %s", exc)
    return _JOB_HANDLE


def bind_to_lifetime_job(proc: subprocess.Popen) -> bool:
    """Bind `proc` to the kill-on-close Job Object so Windows reaps it
    if this interpreter dies unexpectedly. Currently unused — kept for
    future subprocess additions."""
    if proc not in _TRACKED_PROCS:
        _TRACKED_PROCS.append(proc)
    if os.name != "nt" or proc.pid <= 0 or proc.pid in _TRACKED_PIDS:
        return True
    job = _create_kill_on_close_job()
    if job is None:
        return False
    try:
        import win32api
        import win32con
        import win32job
        ph = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, proc.pid)
        try:
            win32job.AssignProcessToJobObject(job, ph)
        finally:
            win32api.CloseHandle(ph)
        _TRACKED_PIDS.add(proc.pid)
        return True
    except Exception as exc:
        log.warning("could not assign PID %d to Job Object: %s", proc.pid, exc)
        return False


def _atexit_kill_tracked() -> None:
    for proc in list(_TRACKED_PROCS):
        try:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass


atexit.register(_atexit_kill_tracked)


def kill_process_tree(pid: int, force: bool = True) -> None:
    """Recursively kill `pid` and its descendants on Windows. Kept as a
    utility — none of the current code paths reach it."""
    args = ["taskkill.exe", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(
            args, capture_output=True, timeout=5.0, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        pass


def port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
