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
entirely via `hide_for_session()` + `show_from_session()`, or switch
it to "active only" via `set_active_only()` so the idle orb stays
hidden and the pill only surfaces during non-idle states.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QSize, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QPen, QMouseEvent, QPainterPath, QLinearGradient,
)
from PySide6.QtWidgets import QWidget

from voxtype import config
from voxtype.types import PillState

log = logging.getLogger("voxtype.pill")

ORB_SIZE = 28
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
        # "Active only": keep the pill hidden while idle, reveal it only
        # for non-idle states. Seeded from settings; toggled from the tray.
        self._active_only = bool(config.load().pill_active_only)
        # Optional source of live audio levels (oldest → newest, each in
        # [0, 1]). Wired by the orchestrator to Recorder.levels so the
        # waveform reflects the actual mic instead of a sine wave.
        self.level_provider: Callable[[], list[float]] | None = None

        self._phase = 0
        self._tick = QTimer(self)
        self._tick.setInterval(80)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._place()
        self._apply_visibility()

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
        self._apply_visibility()
        self.update()

    def _should_be_visible(self) -> bool:
        if self._force_hidden:
            return False
        if self._active_only and self._state == "idle":
            return False
        return True

    def _apply_visibility(self) -> None:
        if self._should_be_visible():
            self.show()
            self.raise_()
        else:
            self.hide()

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
        self._apply_visibility()

    def set_active_only(self, enabled: bool) -> None:
        """Toggle "active only" mode: hide the idle orb, show the pill
        only during non-idle states. Applies immediately."""
        self._active_only = bool(enabled)
        self._apply_visibility()

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

        # Idle: pure transparent background — just the breathing dots.
        # The shell (bg fill + border) is only drawn for active states so
        # the pill doesn't show a persistent grey box on the desktop.
        if state != "idle":
            bg = QColor(_BG.get(state, _BG["idle"]))
            border = _BORDER.get(state, _BORDER["idle"])
            p.setBrush(QBrush(bg))
            p.setPen(QPen(border, 1.0))
            radius = 6.0
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
        # Breathing oscillation (height/alpha)
        breathe = 0.7 + 0.3 * math.sin(self._phase / 25.0)
        # Color oscillation (half-speed to toggle every other breath)
        color_shift = math.sin(self._phase / 50.0)
        
        bar_count = 5
        bar_width = 3.0
        spacing = 2.0
        base_heights = [8.0, 12.0, 16.0, 12.0, 8.0]
        
        total_width = bar_count * bar_width + (bar_count - 1) * spacing
        start_x = cx - total_width / 2.0
        
        # Calculate grey levels based on color_shift:
        # Oscillates the average brightness so one pulse is light, the next is dark.
        # Shift ranges from -60 to +60.
        shift = 60 * color_shift
        v1 = int(max(40, min(230, 160 + shift)))
        v2 = int(max(20, min(210, 70 + shift)))
        
        alpha = int(255 * 0.95 * breathe)
        grad = QLinearGradient(QPointF(start_x, cy), QPointF(start_x + total_width, cy))
        grad.setColorAt(0.0, QColor(v1, v1, v1, alpha))
        grad.setColorAt(1.0, QColor(v2, v2, v2, alpha))
        
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        
        for i in range(bar_count):
            h = base_heights[i] * breathe
            x = start_x + i * (bar_width + spacing)
            y = cy - h / 2.0
            p.drawRoundedRect(QRectF(x, y, bar_width, h), 1.0, 1.0)

    def _draw_recording(self, p: QPainter,
                         rx: float, ry: float, rw: float, rh: float) -> None:
        bar_count = 11
        levels: list[float] = []
        if self.level_provider is not None:
            try:
                levels = self.level_provider()
            except Exception:
                levels = []

        # Latest level drives the dot pulse; falls back to sine when no
        # provider is wired (orchestrator-less smoke tests).
        if levels:
            pulse = 0.4 + 0.6 * levels[-1]
        else:
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

        bar_x = rx + 16
        for i in range(bar_count):
            if levels:
                # Newest level on the right; pad with 0 if we don't have
                # a full ring yet so bars grow in from the right edge.
                idx = len(levels) - bar_count + i
                lvl = levels[idx] if 0 <= idx < len(levels) else 0.0
            else:
                lvl = abs(math.sin((self._phase + i * 4) / 6.0))
            h = 2.0 + 6.0 * lvl
            alpha = int(90 + 140 * lvl)
            p.setBrush(QBrush(QColor(248, 113, 113, max(40, min(alpha, 235)))))
            p.drawRoundedRect(QRectF(bar_x + i * 3, ry + rh / 2 - h / 2, 1.6, h), 0.8, 0.8)

    def _draw_processing(self, p: QPainter, cx: float, cy: float) -> None:
        # Three amber dots orbiting the center; the lead dot is brightest
        # and largest, the trailing two fade for a comet-trail feel.
        p.setPen(Qt.PenStyle.NoPen)
        orbit_r = 5.5
        base = self._phase * 0.22
        spec = ((1.9, 235), (1.5, 150), (1.1, 80))
        for i, (size, alpha) in enumerate(spec):
            a = base - i * 0.55
            x = cx + orbit_r * math.cos(a)
            y = cy + orbit_r * math.sin(a)
            p.setBrush(QBrush(QColor(245, 158, 11, alpha)))
            p.drawEllipse(QRectF(x - size, y - size, 2 * size, 2 * size))

    def _draw_enhancing(self, p: QPainter, cx: float, cy: float) -> None:
        # 4-point star that slowly rotates while pulsing — feels more
        # "magical processing" than the static twinkle. Points rotated
        # in-place to avoid juggling QPainter save/restore state.
        twinkle = 0.78 + 0.22 * math.sin(self._phase / 6.5)
        rot = math.radians(self._phase * 3.0)
        cr, sr = math.cos(rot), math.sin(rot)
        arm = 5.4 * twinkle
        waist = 1.3
        local = (
            (0, -arm), (waist, -waist), (arm, 0), (waist, waist),
            (0, arm), (-waist, waist), (-arm, 0), (-waist, -waist),
        )
        path = QPainterPath()
        first = True
        for x, y in local:
            wx = cx + x * cr - y * sr
            wy = cy + x * sr + y * cr
            if first:
                path.moveTo(wx, wy)
                first = False
            else:
                path.lineTo(wx, wy)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(167, 139, 250, int(230 * twinkle))))
        p.drawPath(path)
        # Bright bead at the centre, breathing out of phase with the star.
        bead_r = 1.0 + 0.6 * (1.0 - twinkle)
        p.setBrush(QBrush(QColor(224, 215, 255, 235)))
        p.drawEllipse(QRectF(cx - bead_r, cy - bead_r, 2 * bead_r, 2 * bead_r))

    def _draw_typing(self, p: QPainter, cx: float, cy: float) -> None:
        # Progressively-drawn check that loops: ~1.4s draw, ~0.4s hold,
        # then restart. The pen drags along the two segments by length
        # (not by raw t) so the speed feels uniform across the elbow.
        loop_len = 22
        progress = (self._phase % loop_len) / loop_len
        draw_t = min(1.0, progress / 0.78)
        pen = QPen(QColor(52, 211, 153), 1.9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        ax, ay = cx - 4.5, cy + 0.4
        bx, by = cx - 1.0, cy + 3.2
        ex, ey = cx + 4.5, cy - 3.2
        seg1 = math.hypot(bx - ax, by - ay)
        seg2 = math.hypot(ex - bx, ey - by)
        target = draw_t * (seg1 + seg2)
        path = QPainterPath()
        path.moveTo(ax, ay)
        if target <= seg1:
            t = target / seg1 if seg1 else 1.0
            path.lineTo(ax + t * (bx - ax), ay + t * (by - ay))
        else:
            path.lineTo(bx, by)
            t = (target - seg1) / seg2 if seg2 else 1.0
            path.lineTo(bx + t * (ex - bx), by + t * (ey - by))
        p.drawPath(path)

    def _draw_error(self, p: QPainter, cx: float, cy: float) -> None:
        # Pulsing halo behind a jittering bolt — reads as alarm without
        # being noisy. Halo pulse uses |sin| so the ring fully fades in
        # and out instead of just brightening.
        pulse = abs(math.sin(self._phase / 4.5))
        halo_r = 5.0 + 2.0 * pulse
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(248, 113, 113, int(70 * pulse))))
        p.drawEllipse(QRectF(cx - halo_r, cy - halo_r, 2 * halo_r, 2 * halo_r))
        jitter = 0.4 * math.sin(self._phase / 1.6)
        p.translate(jitter, 0)
        path = QPainterPath()
        path.moveTo(cx + 0.5, cy - 4.5); path.lineTo(cx - 2,   cy + 0.5)
        path.lineTo(cx - 0.5, cy + 0.5); path.lineTo(cx - 1,   cy + 4.5)
        path.lineTo(cx + 2,   cy - 0.5); path.lineTo(cx + 0.5, cy - 0.5)
        path.closeSubpath()
        p.setBrush(QBrush(QColor(248, 113, 113)))
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
        margin = 56            # sits well above the taskbar
        x = g.x() + (g.width() - self.width()) // 2
        y = g.y() + g.height() - self.height() - margin
        self.move(x, y)

    def _on_tick(self) -> None:
        self._phase = (self._phase + 1) % 36000
        if self._state in ("idle", "recording", "processing",
                           "enhancing", "typing", "error"):
            self.update()
