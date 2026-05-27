"""GUI-side manager for the single shared torch worker subprocess.

The GUI process never imports torch. This module owns the one
`engine_worker` child, talks to it over a localhost socket, and respawns
it transparently after it self-exits on idle (the mechanism that frees the
CUDA context). Both `stt_engine` and `tts_engine` proxy through here.

Design notes:
  - One worker, one CUDA context. Requests open a fresh short-lived socket
    each (no head-of-line blocking between a streaming synth and a status
    poll). The worker serves connections concurrently.
  - `request(..., spawn=True)` lazily (re)spawns the worker; `spawn=False`
    (used by the status poller) never resurrects an idle-exited worker, so
    polling can't defeat idle-exit.
  - A background poller caches the worker's status dict so the engines'
    synchronous get_status()/idle_info() never block on IPC.
  - All log strings ASCII (cp1252 console caveat - see debug_log).
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator

from voxtype import config, engine_ipc

log = logging.getLogger("voxtype.engine_host")

_CONNECT_TIMEOUT = 2.0
_REQUEST_TIMEOUT = 300.0   # generous: a cold load + warmup can be slow
_SPAWN_WAIT = 30.0


class _Down(Exception):
    """Worker is not reachable and we were asked not to spawn it."""


class EngineHost:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._port: int = 0
        self._spawn_lock = threading.Lock()
        self._status: dict[str, Any] = {}
        self._status_lock = threading.Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._poller_started = False

    # ── Subprocess lifecycle ─────────────────────────────────────────

    def _alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and self._port > 0)

    def _spawn(self) -> bool:
        """Spawn the worker and read its PORT handshake. Caller holds
        _spawn_lock. Returns True once a port is known."""
        if self._alive():
            return True
        idle_exit = int(getattr(config.load(), "engine_idle_exit_sec", 60) or 60)
        cmd = [sys.executable, "-m", "voxtype.engine_worker",
               "--idle-exit-sec", str(idle_exit)]
        env = dict(os.environ)
        env["VOXTYPE_DATA_DIR"] = str(config.data_dir())
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, creationflags=creationflags, close_fds=True,
                text=True, bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("engine worker spawn failed: %s", exc)
            return False

        # Bind to the kill-on-close Job Object so the worker dies with us.
        try:
            from voxtype import process as _proc_mod
            _proc_mod.bind_to_lifetime_job(proc)
        except Exception:
            pass

        port = 0
        deadline = time.monotonic() + _SPAWN_WAIT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.error("engine worker exited during startup (rc=%s)",
                          proc.returncode)
                return False
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                continue
            line = line.strip()
            if line.startswith("PORT "):
                try:
                    port = int(line.split()[1])
                except (ValueError, IndexError):
                    port = 0
                break
            if line.startswith("BIND_FAILED"):
                log.error("engine worker: %s", line)
                return False
        if port <= 0:
            log.error("engine worker: no PORT handshake within %.0fs", _SPAWN_WAIT)
            try:
                proc.kill()
            except Exception:
                pass
            return False
        self._proc = proc
        self._port = port
        log.info("engine worker started pid=%s port=%d", proc.pid, port)
        # Drain any further stdout so the pipe never fills.
        threading.Thread(target=self._drain_stdout, args=(proc,),
                         daemon=True).start()
        self._ensure_poller()
        return True

    def _drain_stdout(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdout:
                for _ in proc.stdout:
                    pass
        except Exception:
            pass

    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(_CONNECT_TIMEOUT)
        s.connect(("127.0.0.1", self._port))
        s.settimeout(_REQUEST_TIMEOUT)
        return s

    # ── Request / stream ─────────────────────────────────────────────

    def request(self, op: str, header: dict | None = None,
                payload: bytes = b"", *, spawn: bool = True
                ) -> tuple[dict[str, Any], bytes]:
        """One round-trip. Respawns + retries once on a dead worker when
        spawn=True. Raises _Down if spawn=False and the worker is gone."""
        hdr = dict(header or {}); hdr["op"] = op
        last_exc: Exception | None = None
        for attempt in range(2):
            if not self._alive():
                if not spawn:
                    raise _Down(op)
                with self._spawn_lock:
                    if not self._spawn():
                        raise RuntimeError("engine worker unavailable")
            try:
                s = self._connect()
            except OSError as exc:
                last_exc = exc
                self._port = 0  # force respawn on next attempt
                if not spawn:
                    raise _Down(op)
                continue
            try:
                engine_ipc.send_frame(s, hdr, payload)
                rhdr, rpayload = engine_ipc.recv_frame(s)
                if rhdr is None:
                    raise RuntimeError("worker closed connection")
                return rhdr, rpayload
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        raise RuntimeError(f"engine worker request failed: {last_exc}")

    def stream(self, op: str, header: dict | None = None
               ) -> Iterator[tuple[dict[str, Any], bytes]]:
        """Generator over a streaming response (synth_stream). Yields
        (header, payload) frames until an `end`/`error` frame."""
        hdr = dict(header or {}); hdr["op"] = op
        if not self._alive():
            with self._spawn_lock:
                if not self._spawn():
                    raise RuntimeError("engine worker unavailable")
        s = self._connect()
        try:
            engine_ipc.send_frame(s, hdr)
            first, _ = engine_ipc.recv_frame(s)
            if first is None:
                raise RuntimeError("worker closed connection")
            if not first.get("ok", False):
                raise RuntimeError(first.get("error", "stream start failed"))
            yield first, b""
            while True:
                fhdr, fpayload = engine_ipc.recv_frame(s)
                if fhdr is None or fhdr.get("end"):
                    return
                if "error" in fhdr:
                    raise RuntimeError(fhdr["error"])
                yield fhdr, fpayload
        finally:
            try:
                s.close()
            except Exception:
                pass

    # ── Status poller ────────────────────────────────────────────────

    def _ensure_poller(self) -> None:
        if self._poller_started:
            return
        self._poller_started = True
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="engine-host-poller").start()

    def _poll_loop(self) -> None:
        while True:
            time.sleep(1.5)
            try:
                rhdr, _ = self.request("status", spawn=False)
                snap = rhdr
            except _Down:
                snap = {"ok": False, "down": True}
            except Exception:
                snap = {"ok": False, "down": True}
            with self._status_lock:
                self._status = snap
            for fn in list(self._listeners):
                try:
                    fn(snap)
                except Exception:
                    pass

    def cached_status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def on_status(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def stop(self) -> None:
        """Best-effort: ask the worker to exit, then kill if needed."""
        try:
            self.request("shutdown", spawn=False)
        except Exception:
            pass
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._port = 0


_HOST: EngineHost | None = None


def get_host() -> EngineHost:
    global _HOST
    if _HOST is None:
        _HOST = EngineHost()
    return _HOST
