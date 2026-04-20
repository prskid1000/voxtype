"""VoxType orchestrator — ties hotkey → record → STT → LLM → typer.

Architecture:
  - Qt runs on the main thread (QApplication.exec blocks there).
  - A dedicated asyncio loop runs on a worker thread for HTTP /
    subprocess tasks (Whisper upload, LLM enhance, service probes).
  - The hotkey listener runs on pynput's own thread.

State transitions (PillState):
    idle → recording → processing → [enhancing →] typing → idle
  On any failure → error for 2 s → idle.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QObject, QTimer, Signal, Slot, QCoreApplication
from PySide6.QtWidgets import QApplication

from voxtype import config, debug_log, services, stt, llm
from voxtype.audio import Recorder
from voxtype.hotkey import HotkeyListener
from voxtype.pill_window import PillWindow
from voxtype.settings_window import SettingsWindow
from voxtype.tray_menu import Tray
from voxtype.typer import type_text
from voxtype.vad import has_speech, estimate_duration
from voxtype.screen_capture import capture_active_screen
from voxtype.history import Entry, add as history_add, now as history_now
from voxtype.types import AppSettings
from voxtype import __version__

log = logging.getLogger("voxtype.main")


# ══════════════════════════════════════════════════════════════════════
# Asyncio-on-a-thread helper
# ══════════════════════════════════════════════════════════════════════

class _AsyncLoopThread:
    """Run asyncio.new_event_loop() on a dedicated thread and expose
    run_coroutine_threadsafe-style submission."""
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="voxtype-asyncio", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def submit(self, coro):
        """Schedule `coro` on the worker loop; returns a concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


# ══════════════════════════════════════════════════════════════════════
# Orchestrator — holds all state + wires up handlers
# ══════════════════════════════════════════════════════════════════════

class Orchestrator(QObject):
    pill_state_req  = Signal(str, str)    # state, message — cross-thread
    flash_error_req = Signal(str, int)    # message, dwell_ms — cross-thread

    def __init__(self, app: QApplication, loop: _AsyncLoopThread) -> None:
        super().__init__()
        self._app = app
        self._loop = loop
        self._recording_since: float = 0.0
        # In-flight pipeline tracker. While this future is alive+unfinished
        # we ignore new hotkey presses — neither Whisper nor llama.cpp
        # handle mid-request cancellation reliably on our setup (Whisper's
        # sync threadpool route can't be aborted at all, llama.cpp has
        # ~1 s poll latency). Simpler + cleaner: the user can't start a
        # new turn until the current one finishes or the timeouts fire.
        self._pipeline_future = None  # type: ignore[var-annotated]

        self.recorder = Recorder()

        self.pill = PillWindow()
        self.pill_state_req.connect(self._apply_pill_state)
        self.flash_error_req.connect(self._apply_flash_error)

        self.window = SettingsWindow(
            restart_service=self._restart_service,
            capture_hotkey=self._capture_hotkey,
            set_hotkey=self._apply_hotkey,
        )

        self.tray = Tray(
            on_toggle_window=self.window.toggle,
            on_quit=self.quit,
            on_restart_service=self._restart_service,
            on_proxy_ping=self._probe_proxy,
            on_pill_reset=self.pill.reset_position,
            on_pill_hide=self.pill.hide_for_session,
            on_pill_show=self.pill.show_from_session,
        )

        # Hotkey
        self.hotkey = HotkeyListener(
            on_activate=self._on_hotkey_down,
            on_deactivate=self._on_hotkey_up,
        )
        s = config.load()
        self.hotkey.set_mode(s.hotkey_mode)
        self.hotkey.set_combo(s.hotkey)
        self.hotkey.start()

        # Lazy startup: we spawn the Whisper/Kokoro child processes that
        # VoxType owns, but we do NOT probe the LLM proxy or warm up any
        # model. All health state is populated by real requests (first
        # dictation → Whisper; enhance_enabled + first hotkey → LLM proxy).
        self._loop.submit(self._boot_sidecars())

    # ── Pipeline ─────────────────────────────────────────────────────

    def _pipeline_busy(self) -> bool:
        fut = self._pipeline_future
        return fut is not None and not fut.done()

    def _on_hotkey_down(self) -> None:
        """Hotkey pressed — start recording."""
        if self.recorder.recording:
            return
        if self._pipeline_busy():
            # Previous turn is still processing (STT / LLM / paste).
            # Ignore the hotkey rather than queue another request behind
            # whatever is already in flight — see __init__ for the why.
            log.info("hotkey down ignored — previous pipeline still running")
            self._flash_error("Busy, please wait", dwell_ms=900)
            return
        s = config.load()
        silence_dur = float(s.silence_duration_sec) if s.auto_stop_on_silence else 0.0
        log.info("hotkey down — start recording (silence auto-stop: %s)",
                 f"{silence_dur}s" if silence_dur else "off")
        try:
            self.recorder.start(
                silence_duration=silence_dur,
                on_silence=self._on_auto_silence,
            )
        except Exception as exc:
            log.error("recorder failed to start: %s", exc)
            self._set_pill("error", "mic error")
            return
        self._recording_since = time.monotonic()
        self._set_pill("recording", "")

    def _on_auto_silence(self) -> None:
        """Called from a worker thread when the recorder hits
        `silence_duration` seconds of quiet. Same code path as
        hotkey-up so toggle + hold modes both work."""
        log.info("silence auto-stop fired")
        self._on_hotkey_up()

    def _on_hotkey_up(self) -> None:
        """Hotkey released — finalise capture + run the pipeline."""
        if not self.recorder.recording:
            return
        pcm = self.recorder.stop()
        dur = estimate_duration(pcm)
        log.info("hotkey up — captured %.2fs (%d bytes)", dur, len(pcm))
        if not pcm:
            self._set_pill("idle", "")
            return

        s = config.load()

        # VAD
        if s.vad_enabled and not has_speech(pcm):
            log.info("VAD rejected empty recording")
            self._flash_error("No speech detected")
            return

        self._set_pill("processing", "")
        self._pipeline_future = self._loop.submit(self._pipeline(pcm, s))

    async def _pipeline(self, pcm: bytes, s: AppSettings) -> None:
        """Async half of the dictation pipeline."""
        t0 = time.monotonic()
        raw = ""
        # Idle-unload may have stopped Whisper; (re)spawn if needed.
        try:
            await self._ensure_whisper_running()
        except Exception as exc:
            log.error("Whisper spawn failed: %s", exc)
            self._flash_error("Whisper failed to start")
            return

        services.mark_used("whisper")
        try:
            raw = await stt.transcribe(pcm, f"http://127.0.0.1:{s.whisper_port}")
            log.info("STT: %r", (raw[:120] + "…") if len(raw) > 120 else raw)
        except asyncio.TimeoutError:
            log.error("STT timed out — server unresponsive (check whisper.log)")
            self._flash_error("Whisper hung")
            return
        except Exception as exc:
            log.error("STT failed: %s: %s", type(exc).__name__, exc)
            self._flash_error("STT failed")
            return

        if not raw.strip():
            log.info("STT returned empty")
            self._flash_error("No text")
            return

        final = raw
        if s.enhance_enabled:
            self.pill_state_req.emit("enhancing", "")
            shot = None
            if s.screen_context:
                # Screen capture is sync; run in executor so we don't stall loop
                shot = await asyncio.get_event_loop().run_in_executor(
                    None, capture_active_screen)
            try:
                final = await llm.enhance(
                    raw, s.proxy_url, s.proxy_model,
                    screenshot_jpeg_b64=shot,
                )
            except Exception as exc:
                log.warning("enhance failed, using raw: %s", exc)
                final = raw

        self.pill_state_req.emit("typing", "")
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, type_text, final, bool(s.append_mode),
            )
        except Exception as exc:
            log.error("type_text failed: %s", exc)
            self._flash_error("Paste failed")
            return

        if s.save_history:
            try:
                history_add(Entry(
                    timestamp=history_now(),
                    raw=raw, final=final,
                    enhanced=bool(s.enhance_enabled),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                ))
            except Exception:
                pass

        # Brief pause so the pill's "typing" tick is visible
        await asyncio.sleep(0.4)
        self.pill_state_req.emit("idle", "")

    # ── Services / proxy ─────────────────────────────────────────────

    async def _boot_sidecars(self) -> None:
        """Start the sidecar processes only if the user opted in AND
        asked for them to be pre-started. With the new idle-unload knobs
        we keep things cold by default; services come up on first use."""
        s = config.load()

        # Register idle-unload thresholds even for services we haven't
        # spawned yet — when they come up later, the watcher already knows
        # their timeout.
        services.set_idle_unload("whisper", s.whisper_idle_unload_sec)
        services.set_idle_unload("kokoro",  s.kokoro_idle_unload_sec)
        services.start_idle_watcher()

        tasks = []
        if s.whisper_enabled and s.whisper_auto_start:
            tasks.append(services.start_whisper(services.WhisperConfig(
                model=s.whisper_model, port=s.whisper_port, device=s.whisper_device,
            )))
        if s.kokoro_enabled and s.kokoro_auto_start:
            tasks.append(services.start_kokoro(services.KokoroConfig(
                port=s.kokoro_port, device=s.kokoro_device,
            )))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # No warm-up call. The first real request populates the model
        # cache; latency is paid once on first hotkey press, not on boot.

    async def _ensure_whisper_running(self) -> None:
        """Idempotent: spawn Whisper if it's not already running. Called
        lazily from the pipeline when we're about to transcribe."""
        s = config.load()
        if not s.whisper_enabled:
            return
        if services.is_running("whisper"):
            return
        await services.start_whisper(services.WhisperConfig(
            model=s.whisper_model, port=s.whisper_port, device=s.whisper_device,
        ))

    def _capture_hotkey(self, cb) -> None:
        """Called by Settings → Dictation → Rebind. Forwards to the
        live HotkeyListener so the next 1-2 key presses become the
        new combo."""
        try:
            self.hotkey.capture(cb)
        except Exception as exc:
            log.error("hotkey.capture failed: %s", exc)

    def _apply_hotkey(self, combo) -> None:
        """Push a freshly-captured combo into the running listener."""
        try:
            self.hotkey.set_combo(combo)
        except Exception as exc:
            log.error("hotkey.set_combo failed: %s", exc)

    def _restart_service(self, name: str) -> None:
        s = config.load()
        if name == "whisper":
            cfg = services.WhisperConfig(
                model=s.whisper_model, port=s.whisper_port, device=s.whisper_device,
            )
        else:
            cfg = services.KokoroConfig(port=s.kokoro_port, device=s.kokoro_device)
        self._loop.submit(services.restart_service(name, cfg))

    def _probe_proxy(self) -> None:
        async def _do():
            s = config.load()
            alive = await llm.proxy_alive(s.proxy_url)
            # Schedule back on Qt thread
            QTimer.singleShot(0, lambda: self.tray.set_llm_reachable(alive))
        self._loop.submit(_do())

    # ── Pill state ───────────────────────────────────────────────────

    def _set_pill(self, state: str, message: str) -> None:
        self.pill_state_req.emit(state, message)

    def _flash_error(self, message: str, dwell_ms: int = 2000) -> None:
        """Thread-safe: emit a signal that's handled on the Qt thread.
        The previous implementation called QTimer.singleShot directly
        from pynput's worker thread, which has no Qt event loop — the
        reset-to-idle timer never fired and the pill got stuck red."""
        self.flash_error_req.emit(message, int(dwell_ms))

    @Slot(str, str)
    def _apply_pill_state(self, state: str, message: str) -> None:
        self.pill.set_state(state, message)  # type: ignore[arg-type]

    @Slot(str, int)
    def _apply_flash_error(self, message: str, dwell_ms: int) -> None:
        """Runs on the Qt thread. Safe to start QTimer here."""
        self.pill.set_state("error", message)  # type: ignore[arg-type]
        QTimer.singleShot(dwell_ms, lambda: self.pill.set_state("idle", ""))  # type: ignore[arg-type]

    # ── Quit ────────────────────────────────────────────────────────

    def quit(self) -> None:
        log.info("quit requested")
        try:
            self.hotkey.stop()
        except Exception:
            pass
        # Kick off async shutdown of sidecars + proxy
        fut = self._loop.submit(services.stop_all())
        try:
            fut.result(timeout=6.0)
        except Exception:
            pass
        self.tray.hide()
        self._app.quit()
        # Watchdog — force-exit 5s later if Qt/threads hang
        def _nuke():
            log.warning("graceful shutdown timed out — os._exit(0)")
            os._exit(0)
        threading.Timer(5.0, _nuke).start()


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    debug_log.install()
    log.info("VoxType %s starting", __version__)

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("VoxType")
    app.setQuitOnLastWindowClosed(False)

    # Single-instance guard: if another VoxType is already running, ask
    # it to surface its settings window and exit cleanly. Scheduled-task
    # double-starts and manual `python -m voxtype` relaunches during
    # development both end up here.
    from voxtype.single_instance import is_already_running, InstanceServer
    if is_already_running():
        log.info("exiting — another instance handled activation")
        return 0

    loop = _AsyncLoopThread()
    orch = Orchestrator(app, loop)

    # Become the registered instance; incoming b"show" commands flip
    # the settings window visible + raise.
    _server = InstanceServer(on_show=orch.window.toggle)
    app._voxtype_instance_server = _server  # keep alive

    # Ctrl+C on terminal runs
    signal.signal(signal.SIGINT, lambda *_: orch.quit())

    code = app.exec()
    loop.stop()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
