"""Frameless dark settings window — same layout pattern as telecode.

Sidebar sections:
  Dictation  — hotkey mode/combo, auto-stop, append, VAD
  Services   — Whisper + Kokoro enable/ports/models/device
  LLM        — enhance toggle, screen_context, proxy URL + model
  About      — version, links, log location
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QScrollArea, QFrame, QPushButton, QStackedWidget,
    QLineEdit, QComboBox, QCheckBox, QSpinBox,
)

from voxtype import config
from voxtype.qt_theme import QSS, BG, FG, FG_DIM, FG_MUTE, BORDER, BG_CARD
from voxtype.types import AppSettings, HotkeyCombo
from voxtype.whisper_model import WHISPER_MODELS
from voxtype.kokoro_voice import FEATURED_VOICES

log = logging.getLogger("voxtype.settings_window")


SECTIONS = [
    ("dictation", "Dictation",  "🎙"),
    ("services",  "Services",   "⚙"),
    ("llm",       "LLM",        "🧠"),
    ("about",     "About",      "ℹ"),
]


# ── Helpers ──────────────────────────────────────────────────────────

def _page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    content = QWidget()
    content.setObjectName("content")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(16)
    scroll.setWidget(content)
    return scroll, content, layout


def _card(title: str, sub: str = "") -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setProperty("class", "card")
    outer = QVBoxLayout(card)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    head = QWidget()
    hl = QHBoxLayout(head)
    hl.setContentsMargins(16, 12, 16, 12)
    hl.setSpacing(10)
    t = QLabel(title); t.setProperty("class", "card_title")
    hl.addWidget(t)
    if sub:
        s = QLabel(sub); s.setProperty("class", "card_sub")
        hl.addWidget(s)
    hl.addStretch(1)
    outer.addWidget(head)
    sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
    sep.setStyleSheet(f"color: {BORDER};")
    outer.addWidget(sep)
    body = QWidget()
    bl = QVBoxLayout(body)
    bl.setContentsMargins(16, 12, 16, 12)
    bl.setSpacing(10)
    outer.addWidget(body)
    return card, bl


def _row(left: QWidget, right: QWidget) -> QWidget:
    w = QWidget()
    l = QHBoxLayout(w)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(14)
    left.setFixedWidth(220)
    l.addWidget(left, 0, Qt.AlignmentFlag.AlignTop)
    l.addWidget(right, 1)
    return w


def _label(text: str, help_text: str = "") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    lbl = QLabel(text); lbl.setProperty("class", "row_label")
    v.addWidget(lbl)
    if help_text:
        h = QLabel(help_text); h.setProperty("class", "row_help")
        h.setWordWrap(True)
        v.addWidget(h)
    return w


def _checkbox(path: str, text: str) -> QCheckBox:
    s = config.load()
    parts = path.split(".")
    val = s
    for p in parts:
        val = getattr(val, p) if hasattr(val, p) else False
    cb = QCheckBox(text)
    cb.setChecked(bool(val))
    cb.toggled.connect(lambda v, p=path: config.patch(p, bool(v)))
    return cb


def _line_edit(path: str) -> QLineEdit:
    s = config.load()
    val = getattr(s, path, "")
    le = QLineEdit()
    le.setText(str(val))
    le.editingFinished.connect(lambda: config.patch(path, le.text()))
    return le


def _spin(path: str, lo: int, hi: int, step: int = 1) -> QSpinBox:
    s = config.load()
    val = getattr(s, path, lo)
    sp = QSpinBox()
    sp.setRange(lo, hi)
    sp.setSingleStep(step)
    sp.setValue(int(val))
    sp.valueChanged.connect(lambda v, p=path: config.patch(p, int(v)))
    return sp


def _combo(path: str, options: list[tuple[str, str]]) -> QComboBox:
    """Options: list of (value, label)."""
    s = config.load()
    current = str(getattr(s, path, ""))
    cb = QComboBox()
    for value, label in options:
        cb.addItem(label, value)
    idx = next((i for i, (v, _) in enumerate(options) if v == current), 0)
    cb.setCurrentIndex(idx)
    cb.currentIndexChanged.connect(lambda i: config.patch(path, cb.itemData(i)))
    return cb


# ── Titlebar ─────────────────────────────────────────────────────────

class _TitleBar(QWidget):
    minimize = Signal()
    close_w  = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("titlebar")
        self.setFixedHeight(34)
        self._drag: QPoint | None = None
        l = QHBoxLayout(self)
        l.setContentsMargins(12, 0, 0, 0)
        l.setSpacing(8)
        icon = QLabel("🎙"); icon.setObjectName("titlebar_icon")
        title = QLabel("VoxType"); title.setObjectName("titlebar_title")
        l.addWidget(icon); l.addWidget(title); l.addStretch(1)
        for text, sig, cls in [("─", self.minimize, ""), ("✕", self.close_w, "tb_close")]:
            b = QPushButton(text)
            b.setProperty("class", f"tb_btn {cls}".strip())
            b.setFixedHeight(34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(sig.emit)
            l.addWidget(b)

    def mousePressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        self._drag = None


# ── Section builders ─────────────────────────────────────────────────

def _build_dictation() -> QWidget:
    scroll, _, layout = _page()
    card, body = _card("Dictation", "Hotkey, VAD, typing mode")

    body.addWidget(_row(_label("Hotkey Mode",
        "hold: dictate while the combo is held down. toggle: tap once to start, tap again to stop."),
        _combo("hotkey_mode", [("hold", "Hold"), ("toggle", "Toggle")])))

    # Hotkey combo display (read-only for now; capture UI added later)
    hk = config.load().hotkey
    body.addWidget(_row(_label("Hotkey", "Currently-bound key combo. Edit via settings.json."),
        _line_edit_static(hk.label)))

    body.addWidget(_row(_label("Auto-Stop On Silence",
        "In hold-mode, stop recording if the mic stays quiet for a few seconds."),
        _checkbox("auto_stop_on_silence", "Enabled")))
    body.addWidget(_row(_label("VAD",
        "Skip empty recordings — don't call Whisper if the buffer is pure silence."),
        _checkbox("vad_enabled", "Enabled")))
    body.addWidget(_row(_label("Append Mode",
        "Send {End} before paste so dictation lands at end-of-line."),
        _checkbox("append_mode", "Enabled")))
    body.addWidget(_row(_label("Save History",
        "Persist every transcript to ~/.voxtype/history.json."),
        _checkbox("save_history", "Enabled")))

    layout.addWidget(card)
    layout.addStretch(1)
    return scroll


def _line_edit_static(text: str) -> QLineEdit:
    le = QLineEdit(text)
    le.setReadOnly(True)
    return le


def _build_services(window) -> QWidget:
    scroll, _, layout = _page()

    w_card, w_body = _card("Whisper STT", "faster-whisper-server child process")
    w_body.addWidget(_row(_label("Enabled"), _checkbox("whisper_enabled", "Run Whisper as a child process")))
    w_body.addWidget(_row(_label("Port"), _spin("whisper_port", 1024, 65535)))
    w_body.addWidget(_row(_label("Model"),
        _combo("whisper_model", [(m, lab) for m, lab in WHISPER_MODELS])))
    w_body.addWidget(_row(_label("Device"),
        _combo("whisper_device", [("gpu", "GPU (CUDA)"), ("cpu", "CPU")])))
    restart_whisper = QPushButton("Restart Whisper")
    restart_whisper.setProperty("class", "ghost")
    restart_whisper.clicked.connect(lambda: window.restart_service("whisper"))
    w_body.addWidget(restart_whisper)
    layout.addWidget(w_card)

    k_card, k_body = _card("Kokoro TTS", "Optional. Off by default.")
    k_body.addWidget(_row(_label("Enabled"), _checkbox("kokoro_enabled", "Run Kokoro as a child process")))
    k_body.addWidget(_row(_label("Port"), _spin("kokoro_port", 1024, 65535)))
    k_body.addWidget(_row(_label("Voice"),
        _combo("kokoro_voice", [(v, lab) for v, lab in FEATURED_VOICES])))
    k_body.addWidget(_row(_label("Device"),
        _combo("kokoro_device", [("gpu", "GPU (CUDA)"), ("cpu", "CPU")])))
    restart_kokoro = QPushButton("Restart Kokoro")
    restart_kokoro.setProperty("class", "ghost")
    restart_kokoro.clicked.connect(lambda: window.restart_service("kokoro"))
    k_body.addWidget(restart_kokoro)
    layout.addWidget(k_card)

    layout.addStretch(1)
    return scroll


def _build_llm(window) -> QWidget:
    scroll, _, layout = _page()
    card, body = _card("LLM Transcript Cleanup", "Routed through telecode proxy — no LM Studio")

    body.addWidget(_row(_label("Enhance",
        "Clean up filler words and add punctuation using the LLM."),
        _checkbox("enhance_enabled", "Enabled")))
    body.addWidget(_row(_label("Screen Context",
        "Include a screenshot of the active display so the LLM can resolve 'this'/'that' references."),
        _checkbox("screen_context", "Enabled")))
    body.addWidget(_row(_label("Proxy URL",
        "telecode's dual-protocol proxy. Default: http://127.0.0.1:1235"),
        _line_edit("proxy_url")))
    body.addWidget(_row(_label("Model",
        "Model key as registered in telecode's llamacpp.models (or a mapped alias)."),
        _line_edit("proxy_model")))

    ping_btn = QPushButton("Test Proxy Connection")
    ping_btn.setProperty("class", "ghost")
    status = QLabel(""); status.setStyleSheet(f"color: {FG_DIM};")

    def _ping():
        from voxtype import llm
        s = config.load()
        async def _do():
            alive = await llm.proxy_alive(s.proxy_url)
            status.setText("● reachable" if alive else "○ unreachable")
        asyncio.get_event_loop().create_task(_do()) if asyncio._get_running_loop() else asyncio.run(_do())
    ping_btn.clicked.connect(_ping)

    row = QWidget(); rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0)
    rl.addWidget(ping_btn); rl.addWidget(status); rl.addStretch(1)
    body.addWidget(row)

    layout.addWidget(card)
    layout.addStretch(1)
    return scroll


def _build_about() -> QWidget:
    scroll, _, layout = _page()
    card, body = _card("About", "VoxType (Python)")
    from voxtype import __version__
    body.addWidget(QLabel(f"Version: {__version__}"))
    body.addWidget(QLabel(f"Data dir: {config.data_dir()}"))
    body.addWidget(QLabel("LLM: telecode proxy (http://127.0.0.1:1235)"))
    body.addWidget(QLabel("STT: faster-whisper-server (child process)"))
    body.addWidget(QLabel("TTS: Kokoro-FastAPI (child process, optional)"))
    layout.addWidget(card)
    layout.addStretch(1)
    return scroll


# ── Window ───────────────────────────────────────────────────────────

class SettingsWindow(QMainWindow):
    def __init__(self, restart_service: Callable[[str], None]) -> None:
        super().__init__()
        self._restart_service = restart_service
        self.setWindowTitle("VoxType")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.resize(920, 600)

        root = QWidget(); root.setObjectName("window_root")
        root.setStyleSheet(QSS)
        rl = QVBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self._titlebar = _TitleBar(root)
        self._titlebar.minimize.connect(self.showMinimized)
        self._titlebar.close_w.connect(self.hide)
        rl.addWidget(self._titlebar)

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        self._sidebar = QListWidget()
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setFixedWidth(180)
        for sid, label, icon in SECTIONS:
            it = QListWidgetItem(f"  {icon}    {label}")
            it.setData(Qt.ItemDataRole.UserRole, sid)
            self._sidebar.addItem(it)
        self._sidebar.currentRowChanged.connect(self._on_row)

        self._stack = QStackedWidget()
        self._pages: dict[str, QWidget] = {}

        bl.addWidget(self._sidebar)
        bl.addWidget(self._stack, 1)
        rl.addWidget(body, 1)

        self.setCentralWidget(root)
        self._sidebar.setCurrentRow(0)

    def restart_service(self, name: str) -> None:
        self._restart_service(name)

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _on_row(self, row: int) -> None:
        if row < 0 or row >= len(SECTIONS):
            return
        sid, _, _ = SECTIONS[row]
        if sid not in self._pages:
            if sid == "dictation":
                w = _build_dictation()
            elif sid == "services":
                w = _build_services(self)
            elif sid == "llm":
                w = _build_llm(self)
            else:
                w = _build_about()
            self._pages[sid] = w
            self._stack.addWidget(w)
        self._stack.setCurrentWidget(self._pages[sid])
