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
    pill_state_req = Signal(str, str)   # state, message — crosses thread boundary

    def __init__(self, app: QApplication, loop: _AsyncLoopThread) -> None:
        super().__init__()
        self._app = app
        self._loop = loop
        self._recording_since: float = 0.0

        self.recorder = Recorder()

        self.pill = PillWindow()
        self.pill_state_req.connect(self._apply_pill_state)

        self.window = SettingsWindow(restart_service=self._restart_service)

        self.tray = Tray(
            on_toggle_window=self.window.toggle,
            on_quit=self.quit,
            on_restart_service=self._restart_service,
            on_proxy_ping=self._probe_proxy,
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

        # Boot services + probe proxy in background
        self._loop.submit(self._boot_sidecars())
        self._probe_proxy()

    # ── Pipeline ─────────────────────────────────────────────────────

    def _on_hotkey_down(self) -> None:
        """Hotkey pressed — start recording."""
        if self.recorder.recording:
            return
        log.info("hotkey down — start recording")
        try:
            self.recorder.start()
        except Exception as exc:
            log.error("recorder failed to start: %s", exc)
            self._set_pill("error", "mic error")
            return
        self._recording_since = time.monotonic()
        self._set_pill("recording", "")

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
        self._loop.submit(self._pipeline(pcm, s))

    async def _pipeline(self, pcm: bytes, s: AppSettings) -> None:
        """Async half of the dictation pipeline."""
        t0 = time.monotonic()
        raw = ""
        try:
            raw = await stt.transcribe(pcm, f"http://127.0.0.1:{s.whisper_port}")
            log.info("STT: %r", (raw[:120] + "…") if len(raw) > 120 else raw)
        except Exception as exc:
            log.error("STT failed: %s", exc)
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
        s = config.load()
        tasks = []
        if s.whisper_enabled:
            tasks.append(services.start_whisper(services.WhisperConfig(
                model=s.whisper_model, port=s.whisper_port, device=s.whisper_device,
            )))
        if s.kokoro_enabled:
            tasks.append(services.start_kokoro(services.KokoroConfig(
                port=s.kokoro_port, device=s.kokoro_device,
            )))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Warm up Whisper model with a silent WAV
        if s.whisper_enabled and services.is_running("whisper"):
            try:
                await stt.preload(f"http://127.0.0.1:{s.whisper_port}")
            except Exception:
                pass

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
        self.pill_state_req.emit("error", message)
        QTimer.singleShot(dwell_ms, lambda: self.pill_state_req.emit("idle", ""))

    @Slot(str, str)
    def _apply_pill_state(self, state: str, message: str) -> None:
        self.pill.set_state(state, message)  # type: ignore[arg-type]

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

    loop = _AsyncLoopThread()
    orch = Orchestrator(app, loop)

    # Ctrl+C on terminal runs
    signal.signal(signal.SIGINT, lambda *_: orch.quit())

    code = app.exec()
    loop.stop()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
