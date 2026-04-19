"""Frameless always-on-top status pill — circular 44px orb.

Ports the Electron `<Pill>` component (voxtype/src/renderer/components/Pill.tsx)
to PySide6. Visible at all times (subtle breathing orb when idle);
every non-idle state has its own glyph + animation:

  idle       → slate concentric rings, slow breathe
  recording  → red pulsing dot + waveform bars, shell stretches to pill
  processing → amber arc spinner
  enhancing  → indigo 4-point sparkle
  typing     → green check stroke
  error      → red jolt bolt

Draggable (position persisted to settings). Tray can hide the pill
entirely via `hide_for_session()` + `show_from_session()`.
"""
from __future__ import annotations

import logging
import math

from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QSize, QRectF
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QPen, QMouseEvent, QPainterPath,
)
from PySide6.QtWidgets import QWidget

from voxtype import config
from voxtype.types import PillState

log = logging.getLogger("voxtype.pill")

ORB_SIZE = 22
REC_W, REC_H = 56, 20

# The widget resizes per-state so the translucent window bounds never
# extend past the drawn shape (avoids the DWM frame Windows paints
# around any empty translucent area). Padding is zero except for a 1px
# cushion so antialiased edges aren't clipped.
_PAD = 1


_BG = {
    "idle":       QColor(13, 17, 23, 217),
    "recording":  QColor(26, 10, 10, 229),
    "processing": QColor(18, 16, 10, 229),
    "enhancing":  QColor(12, 10, 20, 229),
    "typing":     QColor(6,  18, 13, 229),
    "error":      QColor(20, 8,  10, 229),
}
_BORDER = {
    "idle":       QColor(255, 255, 255, 15),
    "recording":  QColor(239, 68,  68,  77),
    "processing": QColor(245, 158, 11,  51),
    "enhancing":  QColor(167, 139, 250, 51),
    "typing":     QColor(52,  211, 153, 64),
    "error":      QColor(248, 113, 113, 64),
}


class PillWindow(QWidget):
    state_changed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # Start at idle size; _resize_for_state() re-sizes on state flip
        # so the translucent window bounds never exceed the drawn shape.
        self._resize_for_state("idle")

        self._state: PillState = "idle"
        self._message: str = ""
        self._drag_pos: QPoint | None = None
        self._force_hidden = False

        self._phase = 0
        self._tick = QTimer(self)
        self._tick.setInterval(80)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._place()
        self.show()

    # ── Public API ───────────────────────────────────────────────────

    def set_state(self, state: PillState, message: str = "") -> None:
        prev = self._state
        self._state = state
        self._message = message
        # Resize on shape change (orb ↔ pill) and recenter around the
        # previous visual center so the orb doesn't appear to jump.
        shape_changed = (prev == "recording") != (state == "recording")
        if shape_changed:
            old_center = self.frameGeometry().center()
            self._resize_for_state(state)
            new_rect = self.frameGeometry()
            new_rect.moveCenter(old_center)
            self.move(new_rect.topLeft())
        if not self._force_hidden:
            self.show()
            self.raise_()
        self.update()

    def _resize_for_state(self, state: PillState) -> None:
        if state == "recording":
            w, h = REC_W + 2 * _PAD, REC_H + 2 * _PAD
        else:
            w, h = ORB_SIZE + 2 * _PAD, ORB_SIZE + 2 * _PAD
        self.setFixedSize(QSize(w, h))

    def reset_position(self) -> None:
        self._center_bottom()
        try:
            config.patch("pill_x", self.x())
            config.patch("pill_y", self.y())
        except Exception:
            pass

    def hide_for_session(self) -> None:
        self._force_hidden = True
        self.hide()

    def show_from_session(self) -> None:
        self._force_hidden = False
        self.show()
        self.raise_()

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
        W, H = self.width(), self.height()
        state = self._state
        is_recording = state == "recording"

        if is_recording:
            rw, rh = REC_W, REC_H
            rx = (W - rw) // 2
            ry = (H - rh) // 2
        else:
            rw, rh = ORB_SIZE, ORB_SIZE
            rx = (W - rw) // 2
            ry = (H - rh) // 2

        bg = QColor(_BG.get(state, _BG["idle"]))
        border = _BORDER.get(state, _BORDER["idle"])
        if state == "idle":
            breathe = 0.85 + 0.15 * math.sin(self._phase / 25.0)
            bg.setAlpha(int(bg.alpha() * breathe))
        p.setBrush(QBrush(bg))
        p.setPen(QPen(border, 1.0))
        radius = rh / 2
        p.drawRoundedRect(QRectF(rx, ry, rw, rh), radius, radius)

        cx = rx + rw / 2.0
        cy = ry + rh / 2.0
        if state == "idle":
            self._draw_idle(p, cx, cy)
        elif state == "recording":
            self._draw_recording(p, rx, ry, rw, rh)
        elif state == "processing":
            self._draw_processing(p, cx, cy)
        elif state == "enhancing":
            self._draw_enhancing(p, cx, cy)
        elif state == "typing":
            self._draw_typing(p, cx, cy)
        elif state == "error":
            self._draw_error(p, cx, cy)
        p.end()

    # ── Glyphs ───────────────────────────────────────────────────────

    def _draw_idle(self, p: QPainter, cx: float, cy: float) -> None:
        breathe = 0.85 + 0.15 * math.sin(self._phase / 25.0)
        p.setPen(Qt.PenStyle.NoPen)
        for r, a in ((3.5, 60), (2.5, 50), (1.5, 40)):
            p.setBrush(QBrush(QColor(170, 180, 200, int(a * breathe))))
            p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

    def _draw_recording(self, p: QPainter,
                         rx: float, ry: float, rw: float, rh: float) -> None:
        pulse = 0.6 + 0.4 * math.sin(self._phase / 8.0)
        dot_r = 2.4
        dot_x = rx + 8
        dot_y = ry + rh / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(248, 113, 113, int(80 * pulse))))
        p.drawEllipse(QRectF(dot_x - dot_r - 1.2, dot_y - dot_r - 1.2,
                              2 * (dot_r + 1.2), 2 * (dot_r + 1.2)))
        p.setBrush(QBrush(QColor(248, 113, 113)))
        p.drawEllipse(QRectF(dot_x - dot_r, dot_y - dot_r, 2 * dot_r, 2 * dot_r))
        bar_count = 11
        bar_x = rx + 16
        for i in range(bar_count):
            h = 2.0 + 6.0 * abs(math.sin((self._phase + i * 4) / 6.0))
            alpha = int(90 + 140 * (h - 2.0) / 6.0)
            p.setBrush(QBrush(QColor(248, 113, 113, max(40, min(alpha, 235)))))
            p.drawRoundedRect(QRectF(bar_x + i * 3, ry + rh / 2 - h / 2, 1.6, h), 0.8, 0.8)

    def _draw_processing(self, p: QPainter, cx: float, cy: float) -> None:
        pen = QPen(QColor(245, 158, 11), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = 5.0
        start = (self._phase * 12) % 360
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r),
                  int(start * 16), int(110 * 16))

    def _draw_enhancing(self, p: QPainter, cx: float, cy: float) -> None:
        twinkle = 0.75 + 0.25 * math.sin(self._phase / 7.0)
        p.setPen(Qt.PenStyle.NoPen)
        v = QPainterPath()
        v.moveTo(cx,     cy - 5); v.lineTo(cx + 1, cy)
        v.lineTo(cx,     cy + 5); v.lineTo(cx - 1, cy)
        v.closeSubpath()
        p.setBrush(QBrush(QColor(167, 139, 250, int(230 * twinkle))))
        p.drawPath(v)
        h = QPainterPath()
        h.moveTo(cx - 5, cy); h.lineTo(cx,     cy - 1)
        h.lineTo(cx + 5, cy); h.lineTo(cx,     cy + 1)
        h.closeSubpath()
        p.setBrush(QBrush(QColor(129, 140, 248, int(178 * twinkle))))
        p.drawPath(h)
        p.setBrush(QBrush(QColor(196, 181, 253)))
        p.drawEllipse(QRectF(cx - 0.8, cy - 0.8, 1.6, 1.6))

    def _draw_typing(self, p: QPainter, cx: float, cy: float) -> None:
        pen = QPen(QColor(52, 211, 153), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - 4, cy + 0.3)
        path.lineTo(cx - 1, cy + 3)
        path.lineTo(cx + 4, cy - 3)
        p.drawPath(path)

    def _draw_error(self, p: QPainter, cx: float, cy: float) -> None:
        jitter = 0.3 * math.sin(self._phase / 2.0)
        p.translate(jitter, 0)
        path = QPainterPath()
        path.moveTo(cx + 0.5, cy - 4.5); path.lineTo(cx - 2,   cy + 0.5)
        path.lineTo(cx - 0.5, cy + 0.5); path.lineTo(cx - 1,   cy + 4.5)
        path.lineTo(cx + 2,   cy - 0.5); path.lineTo(cx + 0.5, cy - 0.5)
        path.closeSubpath()
        p.setBrush(QBrush(QColor(248, 113, 113)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)
        p.translate(-jitter, 0)

    # ── Placement ────────────────────────────────────────────────────

    def _place(self) -> None:
        s = config.load()
        if s.pill_x >= 0 and s.pill_y >= 0:
            self.move(s.pill_x, s.pill_y)
        else:
            self._center_bottom()

    def _center_bottom(self) -> None:
        """Place at the center-bottom of the primary display, right
        above the taskbar. `availableGeometry()` excludes the taskbar,
        so a small margin puts the pill hugging the bottom edge."""
        screen = self.screen() or None
        if screen is None:
            return
        g = screen.availableGeometry()
        margin = 2             # essentially flush with the taskbar
        x = g.x() + (g.width() - self.width()) // 2
        y = g.y() + g.height() - self.height() - margin
        self.move(x, y)

    def _on_tick(self) -> None:
        self._phase = (self._phase + 1) % 36000
        if self._state in ("idle", "recording", "processing", "enhancing", "error"):
            self.update()
