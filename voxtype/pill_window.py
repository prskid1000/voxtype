"""Frameless always-on-top status pill shown during dictation.

Replaces the React <Pill> component (voxtype/src/renderer/components/Pill.tsx).
Draggable; position persisted to settings (pill_x / pill_y).

State → display:
  idle       → hidden
  recording  → red ring, "Listening"
  processing → amber spinner, "Transcribing"
  enhancing  → blue spinner, "Enhancing"
  typing     → green check, "Typing"
  error      → red border, short message
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QSize
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QMouseEvent
from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QVBoxLayout

from voxtype import config
from voxtype.qt_theme import ACCENT, ERR, WARN, OK, FG, BG_CARD, BORDER, FG_DIM
from voxtype.types import PillState

log = logging.getLogger("voxtype.pill")


class PillWindow(QWidget):
    """Small always-on-top dictation status pill."""

    state_changed = Signal(str, str)  # new_state, message

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool            # hide from taskbar
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(QSize(220, 56))

        self._state: PillState = "idle"
        self._message: str = ""
        self._drag_pos: QPoint | None = None

        # Spinner animation
        self._phase = 0
        self._tick = QTimer(self)
        self._tick.setInterval(80)
        self._tick.timeout.connect(self._on_tick)

        # Place at saved position (or bottom-centre)
        s = config.load()
        if s.pill_x >= 0 and s.pill_y >= 0:
            self.move(s.pill_x, s.pill_y)
        else:
            self._center_bottom()

        self.hide()

    # ── Public API ───────────────────────────────────────────────────

    def set_state(self, state: PillState, message: str = "") -> None:
        """Update pill state + label. Call from any thread via Qt signal."""
        self._state = state
        self._message = message
        if state == "idle":
            self._tick.stop()
            self.hide()
        else:
            self.show()
            self.raise_()
            if state in ("recording", "processing", "enhancing", "typing"):
                if not self._tick.isActive():
                    self._tick.start()
            else:
                self._tick.stop()
        self.update()

    # ── Dragging ─────────────────────────────────────────────────────

    def mousePressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        self._drag_pos = None
        # Persist new position
        try:
            config.patch("pill_x", self.x())
            config.patch("pill_y", self.y())
        except Exception:
            pass
        super().mouseReleaseEvent(e)

    # ── Painting ─────────────────────────────────────────────────────

    def paintEvent(self, _e) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Background
        p.setBrush(QBrush(QColor(BG_CARD)))
        border_col = QColor(BORDER)
        if self._state == "error":
            border_col = QColor(ERR)
        p.setPen(QPen(border_col, 1))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 14, 14)

        # Indicator dot / spinner on the left
        cx, cy, r = 26, h // 2, 10
        if self._state == "recording":
            p.setBrush(QBrush(QColor(ERR)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        elif self._state == "typing":
            p.setBrush(QBrush(QColor(OK)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        elif self._state in ("processing", "enhancing"):
            color = QColor(WARN) if self._state == "processing" else QColor(ACCENT)
            p.setPen(QPen(color, 2))
            start = (self._phase * 16) % 360
            p.drawArc(cx - r, cy - r, 2 * r, 2 * r, start * 16, 100 * 16)
        elif self._state == "error":
            p.setBrush(QBrush(QColor(ERR)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)

        # Text
        text = {
            "idle":       "",
            "recording":  "Listening",
            "processing": "Transcribing",
            "enhancing":  "Enhancing",
            "typing":     "Typing",
            "error":      self._message or "Error",
        }.get(self._state, "")
        if text:
            p.setPen(QPen(QColor(FG)))
            font = p.font()
            font.setPointSize(10)
            font.setWeight(500)  # type: ignore[arg-type]
            p.setFont(font)
            p.drawText(48, 0, w - 48 - 14, h, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

        # Drag hint on the right
        p.setPen(QPen(QColor(FG_DIM)))
        p.drawText(w - 18, 0, 14, h, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "⋮⋮")

        p.end()

    # ── Internals ────────────────────────────────────────────────────

    def _center_bottom(self) -> None:
        screen = self.screen() or None
        if screen is None:
            return
        g = screen.availableGeometry()
        x = g.x() + (g.width() - self.width()) // 2
        y = g.y() + g.height() - self.height() - 80
        self.move(x, y)

    def _on_tick(self) -> None:
        self._phase = (self._phase + 1) % 360
        self.update()
