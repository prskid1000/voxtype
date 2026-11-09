"""Out-of-process torch engine worker (single shared host for STT + TTS).

Why this exists: `torch.cuda.empty_cache()` frees the allocator's cached
blocks, but the CUDA *context* (~300-600 MB of context + kernels + cuBLAS /
cuDNN workspaces) stays resident for the whole lifetime of the process that
first touched CUDA. The only way to give it back is to exit that process.

So the GUI never imports torch. It spawns THIS process, talks to it over a
localhost socket (see engine_ipc), and the worker runs a docgraph-style
two-stage idle monitor:

  1. per-modality `idle_unload_sec` -> drop that model's weights (big VRAM).
  2. `idle_exit_sec` once BOTH models are unloaded -> exit the process,
     releasing the CUDA context. The GUI respawns us on the next request.

Handshake: we bind 127.0.0.1:0 and print "PORT <n>" on stdout so the parent
learns the OS-assigned port, then serve. Logs go to data/voxtype-worker.log
(a separate file so we don't fight the GUI over voxtype.log). All log
strings stay ASCII (cp1252 console caveat - see debug_log)."""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
from typing import Any

from voxtype import config, engine_ipc

log = logging.getLogger("voxtype.engine_worker")


# ── Per-modality model holder ────────────────────────────────────────

def _record_load(modality: str, model_id: str, device: str, seconds: float, read: dict) -> None:
    """data/load-stats.json: what the last load of each modality took (the settings window shows it)."""
    import json
    from pathlib import Path
    try:
        p = Path(config.data_dir()) / "load-stats.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[modality] = {"at": time.strftime("%Y-%m-%d %H:%M"), "model": model_id, "device": device,
                       "seconds": round(seconds, 1), "read": read}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:  # noqa: BLE001 — a statistic, never a reason to fail a load
        log.debug("load stats not written: %s", exc)


def _triton_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("triton") is not None


class _Model:
    """Owns one lazily-loaded backend (stt or tts) plus its config + idle
    bookkeeping. All torch work runs under the shared host lock."""

    def __init__(self, modality: str) -> None:
        self.modality = modality
        self.backend: Any = None
        self.cfg: dict[str, Any] = {}
        self.loaded_key: tuple | None = None
        self.last_used = time.monotonic()
        self.last_error = ""

    # The subset of cfg that forces a model rebuild (mirrors the old
    # engine _key()). Per-call kwargs (language/voice/opts) are excluded.
    def _key(self) -> tuple:
        c = self.cfg
        return (
            c.get("model_id", ""), c.get("device", "cpu"),
            c.get("dtype", "auto"), bool(c.get("torch_compile", False)),
            c.get("attn_impl", "auto"),
        )

    @property
    def idle_unload_sec(self) -> int:
        return int(self.cfg.get("idle_unload_sec", 0) or 0)

    def is_loaded(self) -> bool:
        return self.backend is not None

    def family(self) -> str:
        if self.backend is None:
            return ""
        try:
            return self.backend.detected_family() or ""
        except Exception:
            return ""

    def sample_rate(self) -> int:
        return int(getattr(self.backend, "sample_rate", 24000)) if self.backend else 24000

    def ensure_loaded(self) -> None:
        if self.backend is not None and self.loaded_key == self._key():
            return
        if self.backend is not None:
            self.unload()
        self._load()

    def _load(self) -> None:
        c = self.cfg
        model_id = c.get("model_id") or _default_model(self.modality)
        # torch.compile on CPU initialises CUDA (dynamo/inductor probe it), pinning a
        # context in the worker even though no weights are on the GPU. CPU compile
        # is also near-useless here, so only honour the flag for cuda.
        compile_ok = bool(c.get("torch_compile", False)) and c.get("device") == "cuda"
        if compile_ok and not _triton_available():
            # torch.compile's default backend (inductor) generates Triton kernels; without Triton (no
            # official Windows build) it fails on the first call - after ~20 s - and the model runs
            # eager anyway. Skip the attempt instead of paying for it on every load.
            log.info("%s: torch_compile skipped (Triton is not installed)", self.modality)
            compile_ok = False
        self.last_error = ""
        if self.modality == "stt":
            from voxtype.backends import get_stt_backend
            from voxtype.backends.stt_base import LoadConfig
            be = get_stt_backend()
            cfg = LoadConfig(
                model_id=model_id, device=c.get("device", "cpu"),
                dtype=c.get("dtype", "auto"), warmup=bool(c.get("warmup", True)),
                torch_compile=compile_ok,
                attn_impl=c.get("attn_impl", "auto"),
            )
        else:
            from voxtype.backends import get_tts_backend
            from voxtype.backends.tts_base import TTSLoadConfig
            be = get_tts_backend()
            cfg = TTSLoadConfig(
                model_id=model_id, device=c.get("device", "cpu"),
                warmup=bool(c.get("warmup", True)),
                torch_compile=compile_ok,
                attn_impl=c.get("attn_impl", "auto"),
            )
        log.info("%s loading model=%s device=%s", self.modality, model_id,
                 c.get("device", "cpu"))
        from voxtype import fast_load
        fast_load.LAST_READ.clear()
        t0 = time.monotonic()
        be.load_sync(cfg)
        self.backend = be
        self.loaded_key = self._key()
        self.last_used = time.monotonic()
        took = self.last_used - t0
        log.info("%s ready in %.1f s (%s)", self.modality, took, be.runtime_info())
        _record_load(self.modality, model_id, c.get("device", "cpu"), took, dict(fast_load.LAST_READ))

    def unload(self) -> bool:
        if self.backend is None:
            return False
        log.info("%s unloading", self.modality)
        be = self.backend
        self.backend = None
        self.loaded_key = None
        try:
            be.unload_sync()
        except Exception as exc:  # noqa: BLE001
            log.debug("%s unload exc (%s)", self.modality, exc)
        return True


def _default_model(modality: str) -> str:
    if modality == "stt":
        return "openai/whisper-large-v3"
    return "hexgrad/Kokoro-82M"


# ── STT / TTS per-call opts (needs the live backend) ─────────────────

def _stt_opts(model: _Model, language: str | None) -> dict[str, Any]:
    lang = (language or model.cfg.get("language") or "en").strip() or "en"
    out: dict[str, Any] = {"language": lang}
    be = model.backend
    if be is None:
        return out
    specs = be.runtime_options() if hasattr(be, "runtime_options") else []
    allowed = {s.key for s in specs}
    for k, v in (model.cfg.get("opts") or {}).items():
        if k in allowed:
            out[k] = v
    return out


def _tts_opts(model: _Model, voice: str | None,
              speed: float | None) -> tuple[str, dict[str, Any]]:
    be = model.backend
    c = model.cfg
    v = (voice or "").strip() if isinstance(voice, str) else ""
    ids = be.voice_ids() if be is not None else set()
    if not v:
        v = (c.get("voice") or "").strip()
    if be is not None and ids and v not in ids:
        v = be.default_voice or next(iter(ids), "")
    if not v and be is not None:
        v = be.default_voice or next(iter(ids), "")
    supports_speed = be.supports("speed") if be is not None else True
    spd = (float(speed) if (speed and speed > 0)
           else float(c.get("speed", 1.0) or 1.0))
    if not supports_speed:
        spd = 1.0
    opts = dict(c.get("opts") or {})
    opts["speed"] = spd
    seed = int(c.get("seed", -1))
    if seed != -1 and "seed" not in opts:
        opts["seed"] = seed
    if be is not None:
        specs = be.runtime_options()
        if specs:
            allowed = {"speed", "seed"} | {s.key for s in specs}
            opts = {k: opts[k] for k in opts if k in allowed}
    return v, opts


# ── Worker host ──────────────────────────────────────────────────────

class _Host:
    def __init__(self, idle_exit_sec: float) -> None:
        self.stt = _Model("stt")
        self.tts = _Model("tts")
        self.idle_exit_sec = float(idle_exit_sec)
        self.lock = threading.Lock()        # serializes all torch work
        self.shutdown = threading.Event()
        self.last_activity = time.monotonic()
        # Every load and unload runs on this one long-lived thread. PyTorch's CPU allocator (mimalloc) keeps a
        # block that is freed on another thread on its owner's list until the owner allocates again; requests
        # arrive on short-lived connection threads and the idle monitor unloads from its own, so an unloaded
        # model's RAM used to stay held. Same owner + MIMALLOC_PURGE_DELAY=0 (engine_host) = it goes back.
        from concurrent.futures import ThreadPoolExecutor
        self._torch = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxtype-torch")

    def run(self, fn, *args):
        """Run `fn(*args)` on the torch thread and return its result (exceptions propagate)."""
        return self._torch.submit(fn, *args).result()

    def model(self, modality: str) -> _Model:
        return self.stt if modality == "stt" else self.tts

    def status(self) -> dict[str, Any]:
        def one(m: _Model) -> dict[str, Any]:
            idle = time.monotonic() - m.last_used
            rem = max(0.0, m.idle_unload_sec - idle) if (
                m.is_loaded() and m.idle_unload_sec > 0) else -1.0
            return {
                "loaded": m.is_loaded(), "family": m.family(),
                "sample_rate": m.sample_rate(), "error": m.last_error,
                "idle_unload_sec": m.idle_unload_sec, "remaining": rem,
            }
        # Stage-2 (process exit -> CUDA context release) countdown: only
        # meaningful once BOTH models are unloaded.
        exit_rem = -1.0
        if (self.idle_exit_sec > 0 and not self.stt.is_loaded()
                and not self.tts.is_loaded()):
            exit_rem = max(0.0, self.idle_exit_sec
                           - (time.monotonic() - self.last_activity))
        return {"ok": True, "alive": True, "stt": one(self.stt),
                "tts": one(self.tts), "idle_exit_sec": self.idle_exit_sec,
                "exit_remaining": exit_rem, "pid": _pid()}

    def idle_monitor(self) -> None:
        """Two-stage idle: unload each model after its own idle_unload_sec;
        exit the process after idle_exit_sec once BOTH are unloaded."""
        while not self.shutdown.wait(2.0):
            try:
                now = time.monotonic()
                with self.lock:
                    for m in (self.stt, self.tts):
                        if (m.is_loaded() and m.idle_unload_sec > 0
                                and now - m.last_used >= m.idle_unload_sec):
                            log.info("%s idle for %.0fs >= %ds - unloading",
                                     m.modality, now - m.last_used,
                                     m.idle_unload_sec)
                            self.run(m.unload)
                    if (self.idle_exit_sec > 0 and not self.stt.is_loaded()
                            and not self.tts.is_loaded()):
                        idle_for = now - self.last_activity
                        if idle_for >= self.idle_exit_sec:
                            log.info("worker idle %.0fs with models unloaded "
                                     "- exiting to free CUDA context", idle_for)
                            self.shutdown.set()
            except Exception as exc:  # noqa: BLE001 - never let the monitor die
                log.error("idle monitor iteration failed: %s", exc)


def _pid() -> int:
    import os
    return os.getpid()


# ── Request handling ─────────────────────────────────────────────────

def _handle(host: _Host, header: dict, payload: bytes,
            conn: socket.socket) -> bool:
    """Dispatch one request. Returns False if the worker should exit."""
    op = header.get("op")

    if op == "ping":
        engine_ipc.send_frame(conn, {"ok": True})
        return True
    if op == "status":
        engine_ipc.send_frame(conn, host.status())
        return True
    if op == "shutdown":
        engine_ipc.send_frame(conn, {"ok": True})
        return False
    if op == "configure":
        m = host.model(header["modality"])
        m.cfg = header.get("cfg") or {}
        host.idle_exit_sec = float(header.get("idle_exit_sec", host.idle_exit_sec))
        engine_ipc.send_frame(conn, {"ok": True})
        return True
    if op in ("load", "unload"):
        m = host.model(header["modality"])
        host.last_activity = time.monotonic()
        if header.get("cfg") is not None:
            m.cfg = header["cfg"]
        if header.get("idle_exit_sec") is not None:
            host.idle_exit_sec = float(header["idle_exit_sec"])
        try:
            with host.lock:
                if op == "load":
                    host.run(m.ensure_loaded)
                else:
                    host.run(m.unload)
            engine_ipc.send_frame(conn, {
                "ok": True, "family": m.family(),
                "sample_rate": m.sample_rate()})
        except Exception as exc:  # noqa: BLE001
            m.last_error = str(exc)
            log.error("%s %s failed: %s", m.modality, op, exc)
            engine_ipc.send_frame(conn, {"ok": False, "error": str(exc)})
        return True
    if op == "transcribe":
        m = host.stt
        host.last_activity = time.monotonic()
        if header.get("cfg") is not None:
            m.cfg = header["cfg"]
        if header.get("idle_exit_sec") is not None:
            host.idle_exit_sec = float(header["idle_exit_sec"])
        try:
            with host.lock:
                host.run(m.ensure_loaded)
                m.last_used = time.monotonic()
                opts = _stt_opts(m, header.get("language"))
                text = host.run(m.backend.transcribe_sync, payload, opts)
            engine_ipc.send_frame(conn, {"ok": True, "text": text})
        except Exception as exc:  # noqa: BLE001
            m.last_error = str(exc)
            log.error("transcribe failed: %s", exc)
            engine_ipc.send_frame(conn, {"ok": False, "error": str(exc)})
        return True
    if op in ("synthesize", "synth_stream"):
        m = host.tts
        host.last_activity = time.monotonic()
        if header.get("cfg") is not None:
            m.cfg = header["cfg"]
        if header.get("idle_exit_sec") is not None:
            host.idle_exit_sec = float(header["idle_exit_sec"])
        try:
            with host.lock:
                host.run(m.ensure_loaded)
                m.last_used = time.monotonic()
                voice, opts = _tts_opts(m, header.get("voice"),
                                        header.get("speed"))
                sr = m.sample_rate()
                if op == "synth_stream":
                    engine_ipc.send_frame(conn, {"ok": True, "sample_rate": sr,
                                                 "stream": True})
                    # each chunk is computed on the torch thread (see _Host.run); sending stays here
                    it = m.backend.synth_chunks_sync(header.get("text", ""), voice, opts)
                    while (chunk := host.run(next, it, None)) is not None:
                        if chunk:
                            engine_ipc.send_frame(conn, {"chunk": True}, chunk)
                    engine_ipc.send_frame(conn, {"end": True})
                else:
                    parts = [c for c in host.run(lambda: list(m.backend.synth_chunks_sync(
                        header.get("text", ""), voice, opts))) if c]
                    engine_ipc.send_frame(
                        conn, {"ok": True, "sample_rate": sr},
                        b"".join(parts))
        except Exception as exc:  # noqa: BLE001
            m.last_error = str(exc)
            log.error("%s failed: %s", op, exc)
            engine_ipc.send_frame(conn, {"ok": False, "error": str(exc)})
        return True

    engine_ipc.send_frame(conn, {"ok": False, "error": f"unknown op: {op}"})
    return True


def _serve_conn(host: _Host, conn: socket.socket) -> None:
    """Serve frames on one connection until it closes or asks to exit."""
    try:
        conn.settimeout(None)
        while not host.shutdown.is_set():
            header, payload = engine_ipc.recv_frame(conn)
            if header is None:
                return
            if not _handle(host, header, payload, conn):
                host.shutdown.set()
                return
    except Exception as exc:  # noqa: BLE001
        log.debug("conn serve ended: %s", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _install_log() -> None:
    path = config.data_dir() / "voxtype-worker.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle-exit-sec", type=float, default=60.0)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)

    _install_log()
    host = _Host(idle_exit_sec=args.idle_exit_sec)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", args.port))
    except OSError as exc:
        print(f"BIND_FAILED {exc}", flush=True)
        return 2
    srv.listen(8)
    srv.settimeout(0.5)
    port = srv.getsockname()[1]
    # Handshake: the parent reads this line to learn our port.
    print(f"PORT {port}", flush=True)
    log.info("engine worker listening on 127.0.0.1:%d (idle_exit=%.0fs)",
             port, args.idle_exit_sec)

    threading.Thread(target=host.idle_monitor, daemon=True,
                     name="worker-idle").start()

    while not host.shutdown.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_serve_conn, args=(host, conn),
                         daemon=True).start()

    log.info("engine worker exiting")
    try:
        srv.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
