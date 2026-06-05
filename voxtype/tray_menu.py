"""QSystemTrayIcon + right-click menu for VoxType.

Submenus:
  STT / TTS / LLM         — status line + load/unload actions
  Open Settings Window    — default left-click
  Quit VoxType
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from voxtype import config, process
from voxtype.qt_theme import QSS

log = logging.getLogger("voxtype.tray")


def make_icon() -> QIcon:
    """Load the PNG icon shipped under resources/."""
    p = Path(__file__).parent / "resources" / "icon.png"
    if p.exists():
        pm = QPixmap(str(p))
        return QIcon(pm)
    # Fallback: blank 16x16 transparent
    pm = QPixmap(16, 16)
    pm.fill()
    return QIcon(pm)


class Tray:
    def __init__(self,
                 on_toggle_window: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_restart_service: Callable[[str], None],
                 on_start_service: Callable[[str], None],
                 on_stop_service: Callable[[str], None],
                 on_proxy_ping: Callable[[], None],
                 on_pill_reset: Callable[[], None] | None = None,
                 on_pill_hide:  Callable[[], None] | None = None,
                 on_pill_show:  Callable[[], None] | None = None,
                 on_pill_active_only: Callable[[bool], None] | None = None) -> None:
        self._on_toggle_window = on_toggle_window
        self._on_quit = on_quit
        self._on_restart_service = on_restart_service
        self._on_start_service = on_start_service
        self._on_stop_service = on_stop_service
        self._on_proxy_ping = on_proxy_ping
        self._on_pill_reset = on_pill_reset
        self._on_pill_hide  = on_pill_hide
        self._on_pill_show  = on_pill_show
        self._on_pill_active_only = on_pill_active_only

        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip("VoxType")

        self.menu = QMenu()
        self.menu.setStyleSheet(QSS)

        # ── STT submenu ─────────────────────────────────────────────
        self._stt_menu = self.menu.addMenu("⬡ STT")
        self._stt_status = QAction("Disabled", self._stt_menu)
        self._stt_status.setEnabled(False)
        self._stt_menu.addAction(self._stt_status)
        self._stt_menu.addSeparator()
        self._stt_start = QAction("Load", self._stt_menu)
        self._stt_start.triggered.connect(lambda: on_start_service("stt"))
        self._stt_menu.addAction(self._stt_start)
        self._stt_stop = QAction("Unload", self._stt_menu)
        self._stt_stop.triggered.connect(lambda: on_stop_service("stt"))
        self._stt_menu.addAction(self._stt_stop)
        self._stt_restart = QAction("Reload", self._stt_menu)
        self._stt_restart.triggered.connect(lambda: on_restart_service("stt"))
        self._stt_menu.addAction(self._stt_restart)

        # ── TTS submenu ─────────────────────────────────────────────
        self._tts_menu = self.menu.addMenu("⬡ TTS")
        self._tts_status = QAction("Disabled", self._tts_menu)
        self._tts_status.setEnabled(False)
        self._tts_menu.addAction(self._tts_status)
        self._tts_menu.addSeparator()
        self._tts_start = QAction("Load", self._tts_menu)
        self._tts_start.triggered.connect(lambda: on_start_service("tts"))
        self._tts_menu.addAction(self._tts_start)
        self._tts_stop = QAction("Unload", self._tts_menu)
        self._tts_stop.triggered.connect(lambda: on_stop_service("tts"))
        self._tts_menu.addAction(self._tts_stop)
        self._tts_restart = QAction("Reload", self._tts_menu)
        self._tts_restart.triggered.connect(lambda: on_restart_service("tts"))
        self._tts_menu.addAction(self._tts_restart)

        # ── LLM submenu ─────────────────────────────────────────────
        self._llm_menu = self.menu.addMenu("⬡ LLM")
        self._llm_status = QAction("telecode proxy: ?", self._llm_menu)
        self._llm_status.setEnabled(False)
        self._llm_menu.addAction(self._llm_status)
        self._llm_menu.addSeparator()
        ping = QAction("Test Proxy", self._llm_menu)
        ping.triggered.connect(lambda: on_proxy_ping())
        self._llm_menu.addAction(ping)

        # ── Pill submenu ────────────────────────────────────────────
        self._pill_menu = self.menu.addMenu("⬢ Pill")
        self._pill_hide_show = QAction("Hide Pill", self._pill_menu)
        self._pill_hide_show.triggered.connect(self._on_pill_hide_show_click)
        self._pill_menu.addAction(self._pill_hide_show)
        reset_pos = QAction("Reset Position", self._pill_menu)
        reset_pos.triggered.connect(lambda: self._on_pill_reset and self._on_pill_reset())
        self._pill_menu.addAction(reset_pos)
        self._pill_menu.addSeparator()
        # Active-only: hide the idle orb, surface the pill only while
        # recording / processing / etc. Checkable, persisted to settings.
        self._pill_active_only = QAction("Only Show When Active", self._pill_menu)
        self._pill_active_only.setCheckable(True)
        self._pill_active_only.setChecked(bool(config.load().pill_active_only))
        self._pill_active_only.toggled.connect(self._on_pill_active_only_toggle)
        self._pill_menu.addAction(self._pill_active_only)
        # Seed from settings so a restart remembers the last hide/show.
        self._pill_is_hidden = bool(config.load().pill_hidden)
        self._pill_hide_show.setText("Show Pill" if self._pill_is_hidden else "Hide Pill")
        if self._pill_is_hidden and self._on_pill_hide:
            self._on_pill_hide()

        self.menu.addSeparator()

        # ── Open settings window ────────────────────────────────────
        open_act = QAction("Open Settings Window", self.menu)
        open_act.triggered.connect(on_toggle_window)
        self.menu.addAction(open_act)
        self.menu.setDefaultAction(open_act)

        self.menu.addSeparator()
        quit_act = QAction("Quit VoxType", self.menu)
        quit_act.triggered.connect(on_quit)
        self.menu.addAction(quit_act)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)

        self._llm_reachable: bool | None = None

        self._refresh_timer = QTimer()
        self._refresh_timer.setInterval(2000)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()
        self._refresh()

        self.tray.show()

    # ── Public hooks ─────────────────────────────────────────────────

    def set_llm_reachable(self, reachable: bool | None) -> None:
        self._llm_reachable = reachable
        self._refresh()

    def hide(self) -> None:
        self.tray.hide()

    # ── Internals ────────────────────────────────────────────────────

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # left click
            self._on_toggle_window()

    def _on_pill_hide_show_click(self) -> None:
        if self._pill_is_hidden:
            if self._on_pill_show:
                self._on_pill_show()
            self._pill_is_hidden = False
            self._pill_hide_show.setText("Hide Pill")
        else:
            if self._on_pill_hide:
                self._on_pill_hide()
            self._pill_is_hidden = True
            self._pill_hide_show.setText("Show Pill")
        config.patch("pill_hidden", self._pill_is_hidden)

    def _on_pill_active_only_toggle(self, checked: bool) -> None:
        config.patch("pill_active_only", bool(checked))
        if self._on_pill_active_only:
            self._on_pill_active_only(bool(checked))

    def _refresh(self) -> None:
        settings = config.load()

        # STT — empty model_path falls back to the engine's built-in
        # default, so Load/Reload are always enabled when STT is on.
        if settings.stt_enabled:
            s = process.get_status("stt")
            from voxtype.stt_engine import DEFAULT_MODEL as _STT_DEFAULT
            chosen = settings.stt_model_path or _STT_DEFAULT
            if s.ready:
                self._stt_menu.setTitle("⬢ STT: Ready")
                model_short = chosen.split("\\")[-1].split("/")[-1]
                self._stt_status.setText(f"{model_short} · {settings.stt_device}")
            elif s.running:
                self._stt_menu.setTitle("⬡ STT: Loading")
                self._stt_status.setText("warming up…")
            else:
                self._stt_menu.setTitle("⬡ STT: Unloaded")
                self._stt_status.setText(s.last_error or "not loaded")
            self._stt_start.setEnabled(not s.ready)
            self._stt_stop.setEnabled(s.ready)
            self._stt_restart.setEnabled(True)
        else:
            self._stt_menu.setTitle("⬡ STT: Disabled")
            self._stt_status.setText("disabled in settings")
            self._stt_start.setEnabled(False)
            self._stt_stop.setEnabled(False)
            self._stt_restart.setEnabled(False)

        # TTS — empty model_path falls back to the engine's built-in
        # default, same as STT.
        if settings.tts_enabled:
            s = process.get_status("tts")
            from voxtype.tts_engine import DEFAULT_MODEL as _TTS_DEFAULT
            chosen = settings.tts_model_path or _TTS_DEFAULT
            if s.ready:
                self._tts_menu.setTitle("⬢ TTS: Ready")
                model_name = chosen.split("\\")[-1].split("/")[-1]
                self._tts_status.setText(f"{model_name} · {settings.tts_device}")
            elif s.running:
                self._tts_menu.setTitle("⬡ TTS: Loading")
                self._tts_status.setText("warming up…")
            else:
                self._tts_menu.setTitle("⬡ TTS: Unloaded")
                self._tts_status.setText(s.last_error or "not loaded")
            self._tts_start.setEnabled(not s.ready)
            self._tts_stop.setEnabled(s.ready)
            self._tts_restart.setEnabled(True)
        else:
            self._tts_menu.setTitle("⬡ TTS: Disabled")
            self._tts_status.setText("disabled in settings")
            self._tts_start.setEnabled(False)
            self._tts_stop.setEnabled(False)
            self._tts_restart.setEnabled(False)

        # LLM — hide until a real request establishes reachability.
        from voxtype import llm as _llm
        status = _llm.get_status()
        if not status.last_checked:
            self._llm_menu.menuAction().setVisible(False)
        else:
            self._llm_menu.menuAction().setVisible(True)
            if status.reachable:
                self._llm_menu.setTitle(f"⬢ LLM: {settings.proxy_model}")
                self._llm_status.setText(f"proxy {settings.proxy_url}")
            else:
                self._llm_menu.setTitle("⬡ LLM: Unreachable")
                self._llm_status.setText(f"no response from {settings.proxy_url}")

        # Tray hover tooltip — both engines fall back to the built-in
        # default when the model_path setting is blank.
        from voxtype.stt_engine import DEFAULT_MODEL as _STT_DEFAULT
        from voxtype.tts_engine import DEFAULT_MODEL as _TTS_DEFAULT

        def _short(path: str) -> str:
            return path.split("\\")[-1].split("/")[-1]

        bits: list[str] = [f"VoxType · hotkey {settings.hotkey.label}"]
        if settings.stt_enabled:
            ws = process.get_status("stt")
            chosen = settings.stt_model_path or _STT_DEFAULT
            model_short = _short(chosen)
            if ws.ready:
                bits.append(f"STT {model_short}")
            elif ws.running:
                bits.append(f"STT loading… ({model_short})")
            else:
                bits.append(f"STT unloaded ({model_short})")
        else:
            bits.append("STT off")
        if settings.tts_enabled:
            ts = process.get_status("tts")
            chosen = settings.tts_model_path or _TTS_DEFAULT
            model_short = _short(chosen)
            if ts.ready:
                bits.append(f"TTS {model_short}")
            elif ts.running:
                bits.append(f"TTS loading… ({model_short})")
            else:
                bits.append(f"TTS unloaded ({model_short})")
        else:
            bits.append("TTS off")
        if status.last_checked:
            bits.append(f"LLM {'ok' if status.reachable else 'down'} · {settings.proxy_model}")
        if settings.server_enabled:
            bits.append(f"HTTP :{settings.server_port}")
        self.tray.setToolTip("\n".join(bits))
