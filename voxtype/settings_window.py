"""Frameless dark settings window — same layout pattern as telecode.

Sidebar sections:
  Dictation  — hotkey mode/combo, auto-stop, append, VAD
  Services   — Whisper + Kokoro enable/ports/models/device
  LLM        — enhance toggle, screen_context, proxy URL + model
  History    — saved transcripts (raw + cleaned), copy-back, clear
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
    ("history",   "History",    "📜"),
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


def _build_history(window) -> QWidget:
    """Saved-transcript viewer. Lists newest-first; clicking an entry
    fills a preview pane with the raw + cleaned text and offers Copy +
    Paste buttons. Refreshes on every show (cheap — bounded to 500)."""
    from datetime import datetime
    from voxtype import history as _hist
    from voxtype.typer import type_text

    scroll, content, layout = _page()
    card, body = _card("History", "Saved transcripts — newest first")

    # ── Top row: status + refresh + clear ───────────────────────────
    top = QHBoxLayout(); top.setSpacing(8)
    count_lbl = QLabel("")
    count_lbl.setStyleSheet("color: #8a96aa; font-size: 11px;")
    refresh_btn = QPushButton("Refresh"); refresh_btn.setProperty("class", "ghost")
    clear_btn = QPushButton("Clear All"); clear_btn.setProperty("class", "danger")
    top.addWidget(count_lbl); top.addStretch(1)
    top.addWidget(refresh_btn); top.addWidget(clear_btn)
    body.addLayout(top)

    # ── List + preview side-by-side ─────────────────────────────────
    from PySide6.QtWidgets import QSplitter, QPlainTextEdit
    split = QSplitter(Qt.Orientation.Horizontal)
    split.setChildrenCollapsible(False)

    entry_list = QListWidget()
    entry_list.setMinimumWidth(280)
    split.addWidget(entry_list)

    # Right-hand preview pane
    right = QWidget()
    rl = QVBoxLayout(right)
    rl.setContentsMargins(10, 0, 0, 0)
    rl.setSpacing(8)
    meta = QLabel("—")
    meta.setStyleSheet("color: #8a96aa; font-size: 11px;")
    rl.addWidget(meta)
    raw_label = QLabel("Raw (Whisper)")
    raw_label.setProperty("class", "section_header")
    rl.addWidget(raw_label)
    raw_view = QPlainTextEdit(); raw_view.setReadOnly(True); raw_view.setMaximumHeight(110)
    rl.addWidget(raw_view)
    final_label = QLabel("Final (after LLM)")
    final_label.setProperty("class", "section_header")
    rl.addWidget(final_label)
    final_view = QPlainTextEdit(); final_view.setReadOnly(True)
    rl.addWidget(final_view, 1)

    # Action buttons under preview
    btn_row = QHBoxLayout()
    copy_raw = QPushButton("Copy Raw"); copy_raw.setProperty("class", "ghost")
    copy_final = QPushButton("Copy Final"); copy_final.setProperty("class", "ghost")
    paste_btn = QPushButton("Paste Final At Cursor"); paste_btn.setProperty("class", "primary")
    btn_row.addWidget(copy_raw); btn_row.addWidget(copy_final); btn_row.addStretch(1)
    btn_row.addWidget(paste_btn)
    rl.addLayout(btn_row)

    split.addWidget(right)
    split.setStretchFactor(0, 0); split.setStretchFactor(1, 1)
    split.setSizes([320, 560])
    body.addWidget(split, 1)

    card.setMinimumHeight(520)
    layout.addWidget(card, 1)

    state: dict = {"entries": []}

    def _fmt_row(e) -> str:
        ts = datetime.fromtimestamp(e.timestamp).strftime("%b %d  %H:%M:%S")
        preview = (e.final or e.raw or "").replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:60] + "…"
        return f"{ts}   {preview}"

    def refresh():
        entries = list(reversed(_hist.load()))  # newest first
        state["entries"] = entries
        entry_list.clear()
        for e in entries:
            item = QListWidgetItem(_fmt_row(e))
            entry_list.addItem(item)
        count_lbl.setText(f"{len(entries)} entries")
        if entries:
            entry_list.setCurrentRow(0)
        else:
            meta.setText("(empty)")
            raw_view.setPlainText("")
            final_view.setPlainText("")

    def _on_select(row: int):
        if row < 0 or row >= len(state["entries"]):
            return
        e = state["entries"][row]
        ts = datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        flags = []
        if e.enhanced:
            flags.append("LLM-enhanced")
        flags.append(f"{e.duration_ms} ms")
        if e.app:
            flags.append(e.app)
        meta.setText(f"{ts}   ·   {'   ·   '.join(flags)}")
        raw_view.setPlainText(e.raw or "")
        final_view.setPlainText(e.final or "")

    def _copy(text: str):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

    def _current():
        row = entry_list.currentRow()
        if row < 0 or row >= len(state["entries"]):
            return None
        return state["entries"][row]

    def _on_copy_raw():
        e = _current();  e and _copy(e.raw or "")

    def _on_copy_final():
        e = _current();  e and _copy(e.final or "")

    def _on_paste():
        e = _current()
        if not e:
            return
        # type_text blocks (PowerShell SendKeys) — run off the Qt thread
        import threading
        threading.Thread(target=type_text, args=(e.final or e.raw or "", False),
                         daemon=True).start()

    def _on_clear():
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(content, "Clear history",
                                 f"Delete all {len(state['entries'])} saved entries?"
                                 ) == QMessageBox.StandardButton.Yes:
            _hist.clear()
            refresh()

    entry_list.currentRowChanged.connect(_on_select)
    refresh_btn.clicked.connect(refresh)
    clear_btn.clicked.connect(_on_clear)
    copy_raw.clicked.connect(_on_copy_raw)
    copy_final.clicked.connect(_on_copy_final)
    paste_btn.clicked.connect(_on_paste)

    refresh()
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
            elif sid == "history":
                w = _build_history(self)
            else:
                return
            self._pages[sid] = w
            self._stack.addWidget(w)
        else:
            # Refresh on re-entry so newly-saved entries appear
            fn = getattr(self._pages[sid], "refresh", None)
            if callable(fn):
                try: fn()
                except Exception: pass
        self._stack.setCurrentWidget(self._pages[sid])
