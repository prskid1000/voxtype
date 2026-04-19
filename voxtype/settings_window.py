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
    ("logs",      "Logs",       "📋"),
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


def _slider_float(path: str, lo: float, hi: float, step: float = 0.1,
                  suffix: str = " s") -> QWidget:
    """Horizontal slider + live float readout, persisted to `path`."""
    from PySide6.QtWidgets import QSlider
    s = config.load()
    cur = float(getattr(s, path, lo))
    w = QWidget()
    l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(10)
    slider = QSlider(Qt.Orientation.Horizontal)
    n = int(round((hi - lo) / step))
    slider.setRange(0, n)
    slider.setSingleStep(1)
    slider.setValue(int(round((cur - lo) / step)))
    readout = QLabel(f"{cur:.1f}{suffix}")
    readout.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; min-width: 52px;")
    def _on(i: int) -> None:
        val = round(lo + i * step, 2)
        readout.setText(f"{val:.1f}{suffix}")
        config.patch(path, val)
    slider.valueChanged.connect(_on)
    l.addWidget(slider, 1)
    l.addWidget(readout)
    return w


def _spin_idle(path: str, default_sec: int = 300) -> QWidget:
    """Auto-unload composite: [Enabled] + [N s spinbox].

    Storage: the same int field (e.g. whisper_idle_unload_sec).
      0          → auto-unload disabled
      > 0        → auto-unload after N seconds

    UI:
      - Checkbox "Auto-Unload" drives enabled state
      - Spinbox is greyed out when checkbox unchecked
      - Last positive value is remembered in `_remembered` so toggling
        OFF→ON restores the previous duration, not the default
    """
    s = config.load()
    cur = int(getattr(s, path, 0))

    w = QWidget()
    l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(10)

    cb = QCheckBox("Auto-Unload")
    cb.setChecked(cur > 0)

    sp = QSpinBox()
    sp.setRange(1, 86400)
    sp.setSingleStep(30)
    sp.setSuffix(" s")
    sp.setEnabled(cur > 0)
    sp.setValue(cur if cur > 0 else default_sec)

    # Remember last nonzero value for checkbox toggling
    state = {"remembered": cur if cur > 0 else default_sec}

    def _on_spin(v: int) -> None:
        state["remembered"] = int(v)
        if cb.isChecked():
            config.patch(path, int(v))
    sp.valueChanged.connect(_on_spin)

    def _on_cb(checked: bool) -> None:
        sp.setEnabled(checked)
        if checked:
            config.patch(path, int(state["remembered"]))
        else:
            config.patch(path, 0)
    cb.toggled.connect(_on_cb)

    l.addWidget(cb); l.addWidget(sp); l.addStretch(1)
    return w


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

def _build_dictation(window) -> QWidget:
    scroll, _, layout = _page()
    card, body = _card("Dictation", "Hotkey, VAD, typing mode")

    body.addWidget(_row(_label("Hotkey Mode",
        "hold: dictate while the combo is held down. toggle: tap once to start, tap again to stop."),
        _combo("hotkey_mode", [("hold", "Hold"), ("toggle", "Toggle")])))

    # Hotkey combo: live label + "Rebind" button that invokes
    # HotkeyListener.capture() through the window. The button grabs the
    # next 1–2 keys and persists the new combo.
    hk_row = QWidget()
    hk_l = QHBoxLayout(hk_row); hk_l.setContentsMargins(0, 0, 0, 0); hk_l.setSpacing(8)
    hk_label = QLabel(config.load().hotkey.label)
    hk_label.setStyleSheet("padding: 4px 8px; border: 1px solid #1e2636; border-radius: 4px; background: #0d1118;")
    hk_label.setMinimumWidth(160)
    rebind_btn = QPushButton("Rebind")
    rebind_btn.setProperty("class", "ghost")

    def _on_rebind():
        from voxtype.types import HotkeyCombo as _HC
        rebind_btn.setEnabled(False)
        hk_label.setText("Press 1-2 keys…")
        def _cb(combo: _HC) -> None:
            # Persist + update the live HotkeyListener
            config.patch("hotkey", {"key1": combo.key1, "key2": combo.key2, "label": combo.label})
            from PySide6.QtCore import QTimer as _QT
            def _refresh():
                hk_label.setText(combo.label)
                rebind_btn.setEnabled(True)
                # Push the new combo into the live listener too
                try:
                    window.set_hotkey(combo)
                except Exception:
                    pass
            _QT.singleShot(0, _refresh)
        try:
            window.capture_hotkey(_cb)
        except Exception as exc:
            log.error("rebind failed: %s", exc)
            hk_label.setText(config.load().hotkey.label)
            rebind_btn.setEnabled(True)

    rebind_btn.clicked.connect(_on_rebind)
    hk_l.addWidget(hk_label); hk_l.addStretch(1); hk_l.addWidget(rebind_btn)
    body.addWidget(_row(_label("Hotkey",
        "Click Rebind, then press the new 1–2 key combo. Colons, dots "
        "and spaces are not allowed in key names."), hk_row))

    body.addWidget(_row(_label("Auto-Stop On Silence",
        "In hold-mode, stop recording if the mic stays quiet for a few seconds."),
        _checkbox("auto_stop_on_silence", "Enabled")))
    body.addWidget(_row(_label("Silence Duration",
        "How many seconds of continuous quiet before auto-stop fires. "
        "The timer only starts after VoxType has heard at least one speech "
        "frame — a silent mic won't insta-stop."),
        _slider_float("silence_duration_sec", 0.5, 5.0, 0.1)))
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
    w_body.addWidget(_row(_label("Auto-Start On Boot",
        "If off (default), Whisper spawns on the first hotkey press. "
        "Turn on only if the first-transcribe warmup delay matters."),
        _checkbox("whisper_auto_start", "Enabled")))
    w_body.addWidget(_row(_label("Idle Unload",
        "Stop the Whisper child after N seconds of no transcribe "
        "requests. 0 = never. Next hotkey spawns it again. Same pattern "
        "as telecode's llamacpp.idle_unload_sec."),
        _spin_idle("whisper_idle_unload_sec")))
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
    k_body.addWidget(_row(_label("Auto-Start On Boot",
        "If off (default), Kokoro spawns on first TTS request. "
        "Turn on if another tool will hit the port continuously."),
        _checkbox("kokoro_auto_start", "Enabled")))
    k_body.addWidget(_row(_label("Idle Unload",
        "Stop the Kokoro child after N seconds of no speak requests. "
        "0 = never."),
        _spin_idle("kokoro_idle_unload_sec")))
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

    # Action icons under preview — compact clipboard buttons only. The
    # old "Paste At Cursor" shortcut is gone; re-pasting history at an
    # arbitrary cursor is rare enough that the clipboard + Ctrl+V in the
    # target app is the right UX.
    btn_row = QHBoxLayout()
    copy_raw = QPushButton("📋 Raw");   copy_raw.setProperty("class", "ghost")
    copy_raw.setToolTip("Copy raw Whisper transcript to clipboard")
    copy_final = QPushButton("📋 Final"); copy_final.setProperty("class", "ghost")
    copy_final.setToolTip("Copy cleaned (LLM-enhanced) transcript to clipboard")
    btn_row.addWidget(copy_raw); btn_row.addWidget(copy_final); btn_row.addStretch(1)
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

    refresh()
    return scroll


def _build_logs(window) -> QWidget:
    """Live-tailing log viewer — same pattern as telecode.

    File picker on top, QPlainTextEdit with QSyntaxHighlighter coloring
    levels / tracebacks / URLs / timestamps. 1 s QTimer appends only the
    new bytes since last poll; rotation detected via size-shrink → full
    reload. Initial load capped at last 512 KB.
    """
    import os, subprocess, sys as _s
    from PySide6.QtCore import QRegularExpression
    from PySide6.QtGui import (
        QTextCharFormat, QColor, QSyntaxHighlighter, QFont, QTextCursor,
    )
    from PySide6.QtWidgets import QPlainTextEdit
    from voxtype import config as _cfg
    from voxtype.qt_theme import (
        ACCENT, WARN, ERR, OK, FG_DIM, FG_MUTE, BG_ELEV,
    )

    LOG_FILES = ["voxtype.log", "voxtype.log.prev"]
    MAX_TAIL_BYTES = 512 * 1024

    class LogHighlighter(QSyntaxHighlighter):
        def __init__(self, doc):
            super().__init__(doc)
            def fmt(color: str, bold: bool = False) -> QTextCharFormat:
                f = QTextCharFormat()
                f.setForeground(QColor(color))
                if bold:
                    f.setFontWeight(QFont.Weight.DemiBold)
                return f
            self._rules = [
                (QRegularExpression(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,\.]?\d*"), fmt(FG_MUTE)),
                (QRegularExpression(r"\b(CRITICAL|FATAL)\b"), fmt("#ff9aa2", True)),
                (QRegularExpression(r"\b(ERROR|ERR)\b"),      fmt(ERR, True)),
                (QRegularExpression(r"\b(WARN(ING)?)\b"),     fmt(WARN, True)),
                (QRegularExpression(r"\b(INFO)\b"),           fmt(ACCENT, True)),
                (QRegularExpression(r"\b(DEBUG|TRACE)\b"),    fmt(FG_DIM, True)),
                (QRegularExpression(r"\[[\w\.\-]+\]"),        fmt(OK)),
                (QRegularExpression(r'^\s*File\s+".+?",\s+line\s+\d+.*$'), fmt("#b892ff")),
                (QRegularExpression(r"^\s*Traceback \(most recent call last\):.*$"), fmt(ERR, True)),
                (QRegularExpression(r"^\s*\w*(Error|Exception):.*$"), fmt(ERR)),
                (QRegularExpression(r"https?://\S+"),         fmt(ACCENT)),
                (QRegularExpression(r"\b\d+(\.\d+)?\b"),      fmt("#a8b3c7")),
            ]

        def highlightBlock(self, text: str) -> None:
            for rx, f in self._rules:
                it = rx.globalMatch(text)
                while it.hasNext():
                    m = it.next()
                    self.setFormat(m.capturedStart(), m.capturedLength(), f)

    scroll, _, layout = _page()
    card, body = _card("Logs", "Live-tailing viewer · auto-refreshes")

    top = QHBoxLayout(); top.setSpacing(8)
    picker = QComboBox()
    for n in LOG_FILES:
        picker.addItem(n)
    picker.setMinimumWidth(180)
    size_lbl = QLabel("—"); size_lbl.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px;")
    follow_cb = QCheckBox("Follow"); follow_cb.setChecked(True)
    clear_btn = QPushButton("Clear View"); clear_btn.setProperty("class", "ghost")
    open_btn = QPushButton("Open Externally"); open_btn.setProperty("class", "ghost")
    reveal_btn = QPushButton("Reveal Folder"); reveal_btn.setProperty("class", "ghost")
    top.addWidget(picker); top.addWidget(size_lbl); top.addStretch(1)
    top.addWidget(follow_cb); top.addSpacing(8)
    top.addWidget(clear_btn); top.addWidget(open_btn); top.addWidget(reveal_btn)
    body.addLayout(top)

    viewer = QPlainTextEdit()
    viewer.setReadOnly(True)
    viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    viewer.setStyleSheet(
        f"QPlainTextEdit {{ background: {BG_ELEV}; border: 1px solid {BORDER};"
        f" border-radius: 6px; font-family: 'JetBrains Mono', Consolas, monospace;"
        f" font-size: 11.5px; padding: 6px 8px; selection-background-color: {ACCENT};"
        f" selection-color: #000; }}"
    )
    viewer.setMinimumHeight(440)
    highlighter = LogHighlighter(viewer.document())
    body.addWidget(viewer, 1)

    card.setMinimumHeight(520)
    layout.addWidget(card, 1)

    state: dict = {"path": None, "pos": 0, "size": 0}

    def _log_path(name: str):
        return _cfg.data_dir() / name

    def _human_bytes(n: int) -> str:
        size = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _load_initial(path):
        viewer.clear()
        if not path.exists():
            viewer.setPlainText(f"[file not found: {path}]")
            state["pos"] = 0; state["size"] = 0
            size_lbl.setText("—")
            return
        size = path.stat().st_size
        state["size"] = size
        start = max(0, size - MAX_TAIL_BYTES)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                if start > 0:
                    f.readline()
                data = f.read()
                state["pos"] = f.tell()
            text = data.decode("utf-8", errors="replace")
            if start > 0:
                text = f"… (showing last {_human_bytes(len(data))} of {_human_bytes(size)}) …\n" + text
            viewer.setPlainText(text)
            if follow_cb.isChecked():
                viewer.moveCursor(QTextCursor.MoveOperation.End)
            size_lbl.setText(_human_bytes(size))
        except Exception as e:
            viewer.setPlainText(f"[error reading {path}: {e}]")

    def _tail():
        path = state.get("path")
        if path is None or not path.exists():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < state["pos"]:
            _load_initial(path); return
        if size == state["pos"]:
            return
        try:
            with open(path, "rb") as f:
                f.seek(state["pos"])
                data = f.read()
                state["pos"] = f.tell()
                state["size"] = size
        except Exception:
            return
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        at_bottom = follow_cb.isChecked() or (
            viewer.verticalScrollBar().value() >= viewer.verticalScrollBar().maximum() - 2
        )
        cursor = viewer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        size_lbl.setText(_human_bytes(size))
        if at_bottom:
            viewer.moveCursor(QTextCursor.MoveOperation.End)

    def _on_pick(idx: int):
        name = picker.itemText(idx)
        state["path"] = _log_path(name)
        state["pos"] = 0
        _load_initial(state["path"])

    def _open_external():
        p = state.get("path")
        if not p:
            return
        try:
            if _s.platform == "win32":
                os.startfile(str(p))
            elif _s.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception:
            pass

    def _reveal():
        folder = _cfg.data_dir()
        try:
            if _s.platform == "win32":
                subprocess.Popen(["explorer", str(folder)])
            elif _s.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            pass

    picker.currentIndexChanged.connect(_on_pick)
    clear_btn.clicked.connect(viewer.clear)
    open_btn.clicked.connect(_open_external)
    reveal_btn.clicked.connect(_reveal)
    _on_pick(0)

    from PySide6.QtCore import QTimer
    timer = QTimer(scroll)
    timer.setInterval(1000)
    timer.timeout.connect(_tail)
    timer.start()

    return scroll


# ── Window ───────────────────────────────────────────────────────────

class SettingsWindow(QMainWindow):
    def __init__(self,
                 restart_service: Callable[[str], None],
                 capture_hotkey: Callable[[Callable], None] | None = None,
                 set_hotkey: Callable[[object], None] | None = None) -> None:
        super().__init__()
        self._restart_service = restart_service
        self._capture_hotkey = capture_hotkey
        self._set_hotkey = set_hotkey
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

    def capture_hotkey(self, cb: Callable) -> None:
        if self._capture_hotkey:
            self._capture_hotkey(cb)

    def set_hotkey(self, combo) -> None:
        if self._set_hotkey:
            self._set_hotkey(combo)

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
                w = _build_dictation(self)
            elif sid == "services":
                w = _build_services(self)
            elif sid == "llm":
                w = _build_llm(self)
            elif sid == "history":
                w = _build_history(self)
            elif sid == "logs":
                w = _build_logs(self)
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
