"""Screenshot the display under the cursor, paint a red cursor ring,
encode to base64 JPEG. Used for the LLM enhance step when
`screen_context` is on.

Ported from voxtype/src/main/screen-capture.ts — same visual marker
(red ring + dot), same 768-px max dimension, same JPEG quality.
mss for fast per-monitor capture, Pillow for resize + marker draw."""
from __future__ import annotations

import base64
import ctypes
import io
import logging
from ctypes import wintypes

import mss
from PIL import Image, ImageDraw

log = logging.getLogger("voxtype.screen_capture")

MAX_DIM = 768
JPEG_QUALITY = 70
RING_RADIUS = 16
RING_THICKNESS = 3
DOT_RADIUS = 3
MARKER_RGB = (255, 0, 0)


def _cursor_pos() -> tuple[int, int]:
    """Absolute desktop coords of the cursor. Windows-only."""
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def capture_active_screen() -> str | None:
    """Return a base64-encoded JPEG of the display under the cursor with
    a red cursor marker painted on it, or None on any failure."""
    try:
        cx, cy = _cursor_pos()
        with mss.mss() as sct:
            # Pick the monitor whose bbox contains the cursor.
            # Monitor 0 is the full virtual screen; skip it.
            chosen = None
            for mon in sct.monitors[1:]:
                left, top, width, height = mon["left"], mon["top"], mon["width"], mon["height"]
                if left <= cx < left + width and top <= cy < top + height:
                    chosen = mon
                    break
            if chosen is None:
                chosen = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(chosen)

        img = Image.frombytes("RGB", shot.size, shot.rgb)
        w, h = img.size
        scale = min(1.0, MAX_DIM / max(w, h))
        if scale < 1.0:
            tw, th = int(round(w * scale)), int(round(h * scale))
            img = img.resize((tw, th), Image.Resampling.LANCZOS)
        tw, th = img.size

        # Cursor in thumbnail coords
        tcx = int(round((cx - chosen["left"]) * scale))
        tcy = int(round((cy - chosen["top"]) * scale))
        if 0 <= tcx < tw and 0 <= tcy < th:
            draw = ImageDraw.Draw(img)
            draw.ellipse(
                [tcx - RING_RADIUS, tcy - RING_RADIUS,
                 tcx + RING_RADIUS, tcy + RING_RADIUS],
                outline=MARKER_RGB, width=RING_THICKNESS,
            )
            draw.ellipse(
                [tcx - DOT_RADIUS, tcy - DOT_RADIUS,
                 tcx + DOT_RADIUS, tcy + DOT_RADIUS],
                fill=MARKER_RGB,
            )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        log.info("screen capture failed: %s", exc)
        return None
