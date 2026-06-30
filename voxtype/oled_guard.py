"""OLED burn-in guard — periodic fullscreen black flash.

Gives OLED pixels a brief, regular rest by flashing a solid-black
topmost frame a few times per second. Reuses VoxType's no-focus overlay
pattern (the same window flags as the status pill plus
WindowTransparentForInput) so the black frame NEVER steals focus or eats
a click while you're typing — the failure mode a plain topmost Tk window
(as in the standalone spec) would cause in a dictation app.

Design:
  - One persistent QWidget; shown/hidden rather than recreated.
  - A QTimer fires every `1000 / flashes_per_sec` ms. Each tick shows the
    black frame, then a one-shot QTimer hides it after one display-refresh
    interval (`1000 / refresh_rate` ms). The interval-in-ms is the same at
    every refresh rate — higher Hz just makes each flash shorter and less
    noticeable.
  - All methods run on the Qt thread (the Orchestrator owns the instance).
    No torch, no extra thread, no busy-wait.

This is best-effort, not vsync-locked — see "Known Limitations" in the
spec. The single-frame hide is subject to a few ms of timer jitter; that
is fine for a rest-cycle flash.
"""
from __future__ import annotations

import ctypes
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QWidget

from voxtype.types import AppSettings

log = logging.getLogger("voxtype.oled")

# Floor for the per-flash duration so a single-frame hide isn't lost to
# QTimer scheduling jitter (which is coarser than 1 ms on Windows).
_MIN_FLASH_MS = 4


def _set_timer_period(ms: int | None) -> None:
    """Raise/lower the Windows multimedia timer resolution. Without this
    a 4 ms QTimer actually fires at the ~15.6 ms default tick, stretching
    the flash to 3-4 frames and making it clearly visible. `timeBeginPeriod(1)`
    pins it to 1 ms while the guard runs; pass None to release. No-op off
    Windows or if winmm is unavailable."""
    try:
        if ms is None:
            ctypes.windll.winmm.timeEndPeriod(1)  # type: ignore[attr-defined]
        else:
            ctypes.windll.winmm.timeBeginPeriod(int(ms))  # type: ignore[attr-defined]
    except Exception:
        pass


def primary_refresh_rate() -> float:
    """Current primary-display refresh rate in Hz (best effort).

    Uses Qt's QScreen instead of the raw EnumDisplaySettings ctypes call
    from the spec — same number, and no struct to keep in sync. Falls
    back to 60 Hz when no screen is available (e.g. headless tests)."""
    scr = QGuiApplication.primaryScreen()
    if scr is not None:
        rr = float(scr.refreshRate() or 0.0)
        if rr >= 1.0:
            return rr
    return 60.0


class OledGuard(QWidget):
    def __init__(self) -> None:
        super().__init__()
        # Same no-focus overlay flags as PillWindow, plus
        # WindowTransparentForInput so the frame is click-through (a click
        # landing during the ~one-frame flash passes to the app beneath).
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0))
        self.setPalette(pal)

        self._enabled = False
        self._flashes_per_sec = 2
        self._frame_ms = _MIN_FLASH_MS
        self._opacity = 1.0
        self._period_held = False

        # PreciseTimer → Qt uses the 1 ms multimedia timer instead of the
        # coarse ~15.6 ms tick, so the one-frame hide is actually one frame.
        self._loop = QTimer(self)
        self._loop.setTimerType(Qt.TimerType.PreciseTimer)
        self._loop.timeout.connect(self._flash)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._hide_timer.timeout.connect(self.hide)

    # ── Public API ───────────────────────────────────────────────────

    def apply(self, s: AppSettings) -> None:
        """(Re)configure from settings. Safe to call repeatedly — the
        tray submenu and the Display settings page both route here via the
        orchestrator after patching config. Qt-thread only."""
        self._flashes_per_sec = max(1, int(getattr(s, "oled_flashes_per_sec", 2)))
        self._frame_ms = max(_MIN_FLASH_MS,
                             int(round(1000.0 / primary_refresh_rate())))
        self._opacity = max(0.05, min(1.0, float(getattr(s, "oled_flash_opacity", 1.0))))
        self.setWindowOpacity(self._opacity)
        self._enabled = bool(getattr(s, "oled_guard_enabled", False))
        if self._enabled:
            self._start()
        else:
            self._stop()

    def stop(self) -> None:
        """Tear down on quit."""
        self._stop()

    # ── Internals ────────────────────────────────────────────────────

    def _start(self) -> None:
        if not self._period_held:
            _set_timer_period(1)
            self._period_held = True
        interval = max(1, int(round(1000.0 / self._flashes_per_sec)))
        self._loop.start(interval)
        log.info("OLED guard on: %d flash/s, %dms frame at ~%.0fHz, %.0f%% dark",
                 self._flashes_per_sec, self._frame_ms, primary_refresh_rate(),
                 self._opacity * 100)

    def _stop(self) -> None:
        self._loop.stop()
        self._hide_timer.stop()
        self.hide()
        if self._period_held:
            _set_timer_period(None)
            self._period_held = False

    def _flash(self) -> None:
        # Re-read primary geometry each tick so a resolution/monitor
        # change is picked up without a settings round-trip. Primary
        # display only (multi-monitor is a future enhancement in the spec).
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            self.setGeometry(scr.geometry())
        self.show()
        self.raise_()
        self._hide_timer.start(self._frame_ms)
