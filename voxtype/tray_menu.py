"""QSystemTrayIcon + right-click menu for VoxType.

Submenus:
  Whisper / Kokoro / LLM  — status line + restart action
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

from voxtype import config, services
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
                 on_proxy_ping: Callable[[], None]) -> None:
        self._on_toggle_window = on_toggle_window
        self._on_quit = on_quit
        self._on_restart_service = on_restart_service
        self._on_proxy_ping = on_proxy_ping

        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip("VoxType")

        self.menu = QMenu()
        self.menu.setStyleSheet(QSS)

        # ── Whisper submenu ─────────────────────────────────────────
        self._whisper_menu = self.menu.addMenu("⬡ Whisper")
        self._whisper_status = QAction("Disabled", self._whisper_menu)
        self._whisper_status.setEnabled(False)
        self._whisper_menu.addAction(self._whisper_status)
        self._whisper_menu.addSeparator()
        restart_w = QAction("Restart", self._whisper_menu)
        restart_w.triggered.connect(lambda: on_restart_service("whisper"))
        self._whisper_menu.addAction(restart_w)

        # ── Kokoro submenu ──────────────────────────────────────────
        self._kokoro_menu = self.menu.addMenu("⬡ Kokoro")
        self._kokoro_status = QAction("Disabled", self._kokoro_menu)
        self._kokoro_status.setEnabled(False)
        self._kokoro_menu.addAction(self._kokoro_status)
        self._kokoro_menu.addSeparator()
        restart_k = QAction("Restart", self._kokoro_menu)
        restart_k.triggered.connect(lambda: on_restart_service("kokoro"))
        self._kokoro_menu.addAction(restart_k)

        # ── LLM submenu ─────────────────────────────────────────────
        self._llm_menu = self.menu.addMenu("⬡ LLM")
        self._llm_status = QAction("telecode proxy: ?", self._llm_menu)
        self._llm_status.setEnabled(False)
        self._llm_menu.addAction(self._llm_status)
        self._llm_menu.addSeparator()
        ping = QAction("Test Proxy", self._llm_menu)
        ping.triggered.connect(lambda: on_proxy_ping())
        self._llm_menu.addAction(ping)

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

    def _refresh(self) -> None:
        settings = config.load()

        # Whisper
        if settings.whisper_enabled:
            s = services.get_status("whisper")
            if s.running and s.ready:
                self._whisper_menu.setTitle(f"⬢ Whisper: Ready :{settings.whisper_port}")
                self._whisper_status.setText(f"PID {s.pid} · port {settings.whisper_port}")
            elif s.running:
                self._whisper_menu.setTitle("⬡ Whisper: Starting")
                self._whisper_status.setText("warming up…")
            else:
                self._whisper_menu.setTitle("⬡ Whisper: Stopped")
                self._whisper_status.setText(s.last_error or "not running")
        else:
            self._whisper_menu.setTitle("⬡ Whisper: Disabled")
            self._whisper_status.setText("disabled in settings")

        # Kokoro
        if settings.kokoro_enabled:
            s = services.get_status("kokoro")
            if s.running and s.ready:
                self._kokoro_menu.setTitle(f"⬢ Kokoro: Ready :{settings.kokoro_port}")
                self._kokoro_status.setText(f"PID {s.pid} · port {settings.kokoro_port}")
            elif s.running:
                self._kokoro_menu.setTitle("⬡ Kokoro: Starting")
                self._kokoro_status.setText("warming up…")
            else:
                self._kokoro_menu.setTitle("⬡ Kokoro: Stopped")
                self._kokoro_status.setText(s.last_error or "not running")
        else:
            self._kokoro_menu.setTitle("⬡ Kokoro: Disabled")
            self._kokoro_status.setText("disabled in settings")

        # LLM
        if self._llm_reachable is True:
            self._llm_menu.setTitle(f"⬢ LLM: {settings.proxy_model}")
            self._llm_status.setText(f"proxy {settings.proxy_url}")
        elif self._llm_reachable is False:
            self._llm_menu.setTitle("⬡ LLM: Unreachable")
            self._llm_status.setText(f"no response from {settings.proxy_url}")
        else:
            self._llm_menu.setTitle("⬡ LLM: Unknown")
            self._llm_status.setText(f"click Test Proxy · {settings.proxy_url}")
