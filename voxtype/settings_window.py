"""Frameless dark settings window — same layout pattern as telecode.

Sidebar sections:
  Dictation  — hotkey mode/combo, auto-stop, append, VAD
  Services   — Server + STT + TTS (enable / model / device per engine)
  LLM        — enhance toggle, screen_context, proxy URL + model
  History    — saved transcripts (raw + cleaned), copy-back, clear
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QScrollArea, QFrame, QPushButton, QStackedWidget,
    QLineEdit, QComboBox, QCheckBox, QSpinBox,
)

from voxtype import config
from voxtype.qt_theme import QSS, BG, FG, FG_DIM, FG_MUTE, BORDER, BG_CARD
from voxtype.types import AppSettings, HotkeyCombo

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
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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

    Storage: the same int field (e.g. stt_idle_unload_sec).
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
        from PySide6.QtCore import QMetaObject, Q_ARG, Qt as _Qt
        rebind_btn.setEnabled(False)
        hk_label.setText("Press 1-2 keys…")

        def _cb(combo: _HC) -> None:
            """Fires on pynput's worker thread after the user presses
            the new combo. config.patch + HotkeyListener.set_combo are
            thread-safe, but Qt widget mutations MUST be marshalled to
            the Qt thread or they're silently lost (the symptom the
            user reported as "rebind doesn't work")."""
            config.patch("hotkey", {
                "key1": combo.key1, "key2": combo.key2, "label": combo.label,
            })
            try:
                window.set_hotkey(combo)
            except Exception as exc:
                log.warning("set_hotkey on live listener failed: %s", exc)
            # Qt-thread marshalling for the two widget mutations
            QMetaObject.invokeMethod(
                hk_label, "setText",
                _Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, combo.label),
            )
            QMetaObject.invokeMethod(
                rebind_btn, "setEnabled",
                _Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, True),
            )

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
        "Skip empty recordings — don't call STT if the buffer is pure silence."),
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


def _hf_check_button(line_edit: QLineEdit, status_lbl: QLabel,
                      default_model: str = "") -> QPushButton:
    """`Check` button that pings huggingface.co/api/models/<id> or, if
    the entered value looks like a local path, just checks existence.
    Empty field → checks the built-in default. Mirrors the DocGraph
    reranker pattern in telecode."""
    from voxtype.qt_theme import OK, ERR, WARN
    btn = QPushButton("Check")
    btn.setProperty("class", "ghost")
    btn.setFixedWidth(58)

    async def _do_check() -> None:
        from pathlib import Path
        import aiohttp
        text = (line_edit.text().strip() or default_model).strip()
        if not text:
            status_lbl.setText("(empty)")
            status_lbl.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px;")
            return
        # Local path? skip HF and just stat the file.
        candidate = Path(text).expanduser()
        if candidate.exists():
            status_lbl.setText("✓ local")
            status_lbl.setStyleSheet(f"color: {OK}; font-size: 11px;")
            return
        status_lbl.setText("checking…")
        status_lbl.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px;")
        # HF API uses GET — HEAD returns 401 even for public repos.
        url = f"https://huggingface.co/api/models/{text.strip('/')}"
        headers = {"User-Agent": "voxtype/1.0", "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                     allow_redirects=True) as resp:
                    if resp.status == 200:
                        status_lbl.setText("✓ found")
                        status_lbl.setStyleSheet(f"color: {OK}; font-size: 11px;")
                    elif resp.status in (401, 403):
                        status_lbl.setText("🔒 private")
                        status_lbl.setStyleSheet(f"color: {WARN}; font-size: 11px;")
                    elif resp.status == 404:
                        status_lbl.setText("✗ not found")
                        status_lbl.setStyleSheet(f"color: {ERR}; font-size: 11px;")
                    else:
                        status_lbl.setText(f"? {resp.status}")
                        status_lbl.setStyleSheet(f"color: {WARN}; font-size: 11px;")
        except Exception:
            status_lbl.setText("error")
            status_lbl.setStyleSheet(f"color: {ERR}; font-size: 11px;")

    def _on_click() -> None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_do_check())
            else:
                asyncio.run(_do_check())
        except RuntimeError:
            asyncio.run(_do_check())

    btn.clicked.connect(_on_click)
    return btn


def _model_row(path_field: str, default_model: str = "") -> QWidget:
    """Model path row: text field (HF repo OR local path) + Browse + Check + status pill.

    Same pattern as docgraph's reranker model row:
      - Empty field → engine uses `default_model` automatically.
      - Placeholder text shows the default so users know what they'll get.
      - Free text accepts an HF repo ID (e.g.
        `csukuangfj/sherpa-onnx-whisper-small.en`) or a local path.
      - Browse picks a local `.onnx` file.
      - Check verifies the value (local stat first, then HF API).
    """
    from PySide6.QtWidgets import QFileDialog
    w = QWidget()
    h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
    le = QLineEdit()
    le.setText(str(getattr(config.load(), path_field, "")))
    le.setPlaceholderText(f"{default_model}  (default)" if default_model else "")
    le.editingFinished.connect(lambda: config.patch(path_field, le.text()))

    browse = QPushButton("Browse…")
    browse.setProperty("class", "ghost")
    browse.setFixedWidth(82)

    def _on_browse() -> None:
        fn, _ = QFileDialog.getOpenFileName(
            w, "Select ONNX model file", le.text() or "",
            "ONNX models (*.onnx);;All files (*.*)",
        )
        if fn:
            le.setText(fn)
            config.patch(path_field, fn)

    browse.clicked.connect(_on_browse)

    status = QLabel("")
    status.setMinimumWidth(86)
    status.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px;")
    le.textChanged.connect(lambda _t: (status.setText(""), None)[1])
    check = _hf_check_button(le, status, default_model)

    h.addWidget(le, 1)
    h.addWidget(browse)
    h.addWidget(check)
    h.addWidget(status)
    return w


def _lifecycle_row(load_label: str, unload_label: str, reload_label: str,
                    on_load: Callable[[], None],
                    on_unload: Callable[[], None],
                    on_reload: Callable[[], None],
                    status_getter: Callable[[], tuple[str, str]]) -> QWidget:
    """Card-footer row: [● status text]      [Load] [Unload] [Reload].

    `status_getter()` returns (text, kind) where kind ∈
    {"ok", "busy", "idle", "err", "off"} — drives pill colour. Polled
    once a second via a QTimer parented to the returned widget."""
    from voxtype.qt_theme import OK, ERR, WARN, ACCENT

    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 4, 0, 0)
    h.setSpacing(8)

    pill = QLabel("○ —")
    pill.setStyleSheet(f"color: {FG_MUTE}; font-size: 11.5px;")
    h.addWidget(pill)
    h.addStretch(1)

    def _mk(label: str, cb: Callable[[], None]) -> QPushButton:
        b = QPushButton(label)
        b.setProperty("class", "ghost")
        b.setFixedHeight(28)
        b.clicked.connect(lambda: _safe(cb))
        return b

    def _safe(cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception as exc:
            log.error("lifecycle action failed: %s", exc)

    btn_load   = _mk(load_label,   on_load)
    btn_unload = _mk(unload_label, on_unload)
    btn_reload = _mk(reload_label, on_reload)
    h.addWidget(btn_load)
    h.addWidget(btn_unload)
    h.addWidget(btn_reload)

    KINDS = {
        "ok":   (OK,      "●"),
        "busy": (WARN,    "◐"),
        "idle": (FG_MUTE, "○"),
        "err":  (ERR,     "✗"),
        "off":  (FG_MUTE, "○"),
    }

    def _refresh() -> None:
        try:
            text, kind = status_getter()
        except Exception:
            text, kind = ("error", "err")
        color, glyph = KINDS.get(kind, (FG_MUTE, "○"))
        pill.setText(f"{glyph} {text}")
        pill.setStyleSheet(f"color: {color}; font-size: 11.5px;")
        # Enable/disable buttons by state
        btn_load.setEnabled(kind in ("idle", "off", "err"))
        btn_unload.setEnabled(kind in ("ok", "busy"))
        btn_reload.setEnabled(kind != "off")

    _refresh()
    timer = QTimer(row)
    timer.setInterval(1000)
    timer.timeout.connect(_refresh)
    timer.start()
    return row


def _engine_status(name: str) -> tuple[str, str]:
    """Translate process.get_status('stt'|'tts') into (text, kind)."""
    from voxtype import process
    s_cfg = config.load()
    enabled = bool(getattr(s_cfg, f"{name}_enabled", False))
    if not enabled:
        return ("disabled", "off")
    s = process.get_status(name)
    if s.ready:
        return ("ready", "ok")
    if s.running:
        return ("loading…", "busy")
    if s.last_error:
        return (f"error: {s.last_error[:48]}", "err")
    return ("unloaded", "idle")


def _server_status() -> tuple[str, str]:
    from voxtype import server
    s_cfg = config.load()
    if not s_cfg.server_enabled:
        return ("disabled", "off")
    return ("running", "ok") if server.is_running() else ("stopped", "idle")


def _build_services(window) -> QWidget:
    scroll, _, layout = _page()

    # ── Server card ────────────────────────────────────────────────
    srv_card, srv_body = _card("OpenAI HTTP Server",
                                "Single port exposing /v1/audio/transcriptions + /v1/audio/speech")
    srv_body.addWidget(_row(_label("Enabled",
        "Embedded server. External clients (telecode, MCP tools) reach the "
        "in-process engines through this. Turn off if you only use VoxType "
        "for local dictation."),
        _checkbox("server_enabled", "Run embedded server")))
    srv_body.addWidget(_row(_label("Port"), _spin("server_port", 1024, 65535)))
    srv_body.addWidget(_lifecycle_row(
        "Start", "Stop", "Restart",
        on_load=lambda: window.start_server(),
        on_unload=lambda: window.stop_server(),
        on_reload=lambda: window.restart_server(),
        status_getter=_server_status,
    ))
    layout.addWidget(srv_card)

    # ── STT card ───────────────────────────────────────────────────
    s_card, s_body = _card("STT", "speech-to-text · transformers + torch (Whisper)")
    s_body.addWidget(_row(_label("Enabled"),
        _checkbox("stt_enabled", "Run STT")))
    s_body.addWidget(_row(_label("Auto-Start On Boot",
        "If off (default), the model loads on the first hotkey press. "
        "Turn on only if the first-transcribe warmup delay matters."),
        _checkbox("stt_auto_start", "Enabled")))
    s_body.addWidget(_row(_label("Idle Unload",
        "Unload the STT model after N seconds of no transcribe "
        "requests. 0 = never. Next request reloads it automatically."),
        _spin_idle("stt_idle_unload_sec")))
    from voxtype.stt_engine import DEFAULT_MODEL as _STT_DEFAULT
    s_body.addWidget(_row(_label("Model",
        "HuggingFace repo ID (auto-downloaded) or local path to a "
        "Whisper-family model. Empty = use the built-in default "
        "shown as placeholder."),
        _model_row("stt_model_path", _STT_DEFAULT)))
    s_body.addWidget(_row(_label("Device",
        "Falls back to CPU automatically if torch.cuda.is_available() is False."),
        _combo("stt_device", [("cpu", "CPU"), ("cuda", "GPU (CUDA)")])))
    s_body.addWidget(_row(_label("Language",
        "ISO 639-1 code (en, de, ja, etc.). Leave 'en' for English."),
        _line_edit("stt_language")))
    s_body.addWidget(_row(_label("Task",
        "transcribe = output source language. translate = output English "
        "regardless of source (Whisper's built-in translation mode)."),
        _combo("stt_task", [("transcribe", "Transcribe"), ("translate", "Translate → EN")])))
    s_body.addWidget(_row(_label("Precision",
        "Inference dtype. auto = fp16 on GPU, fp32 on CPU. bf16 needs "
        "Ampere+ (RTX 30xx / A100+) — same speed as fp16, wider numeric "
        "range. fp32 is the slowest but most accurate."),
        _combo("stt_dtype", [
            ("auto", "Auto"), ("fp16", "fp16 (GPU fast)"),
            ("bf16", "bf16 (Ampere+)"), ("fp32", "fp32 (accurate)"),
        ])))
    s_body.addWidget(_row(_label("Beams",
        "Beam-search width. 1 = greedy decoding, fastest. Higher = lower "
        "WER but ~N× slower. Stick to 1 for live dictation."),
        _spin("stt_num_beams", 1, 10)))
    s_body.addWidget(_row(_label("Initial Prompt",
        "Free text fed to the decoder to bias decoding. Useful for "
        "jargon / acronyms / proper names (e.g. \"VoxType, telecode, "
        "RouteMagic\"). Empty = no bias."),
        _line_edit("stt_initial_prompt")))
    s_body.addWidget(_row(_label("Warm Up On Load",
        "Run a dummy 1-second inference right after the model loads so "
        "the FIRST real hotkey press isn't slow (CUDA kernel autotune, "
        "lazy weight materialisation)."),
        _checkbox("stt_warmup", "Enabled")))
    s_body.addWidget(_row(_label("torch.compile",
        "JIT-compile the model for ~20-40% steady-state speedup. Adds "
        "~30 s to the FIRST inference (one-time compile). Leave off "
        "unless you transcribe constantly."),
        _checkbox("stt_torch_compile", "Enabled")))
    s_body.addWidget(_lifecycle_row(
        "Load", "Unload", "Reload",
        on_load=lambda: window.start_service("stt"),
        on_unload=lambda: window.stop_service("stt"),
        on_reload=lambda: window.restart_service("stt"),
        status_getter=lambda: _engine_status("stt"),
    ))
    layout.addWidget(s_card)

    # ── TTS card ───────────────────────────────────────────────────
    t_card, t_body = _card("TTS", "text-to-speech · kokoro + torch (Kokoro-82M)")
    t_body.addWidget(_row(_label("Enabled"),
        _checkbox("tts_enabled", "Run TTS")))
    t_body.addWidget(_row(_label("Auto-Start On Boot"),
        _checkbox("tts_auto_start", "Enabled")))
    t_body.addWidget(_row(_label("Idle Unload",
        "Unload the TTS model after N seconds of no synthesise calls. "
        "0 = never."),
        _spin_idle("tts_idle_unload_sec")))
    from voxtype.tts_engine import DEFAULT_MODEL as _TTS_DEFAULT
    t_body.addWidget(_row(_label("Model",
        "HuggingFace repo ID. Default = `hexgrad/Kokoro-82M` (54 voices, "
        "9 language families). Empty = use the built-in default shown as "
        "placeholder."),
        _model_row("tts_model_path", _TTS_DEFAULT)))
    t_body.addWidget(_row(_label("Device",
        "Falls back to CPU automatically if torch.cuda.is_available() is False."),
        _combo("tts_device", [("cpu", "CPU"), ("cuda", "GPU (CUDA)")])))
    t_body.addWidget(_row(_label("Voice",
        "Kokoro voice name. Prefix encodes language + gender:\n"
        " a{f,m}_*  American English  (af_heart, am_adam, …)\n"
        " b{f,m}_*  British English   (bf_emma, bm_george, …)\n"
        " e{f,m}_*  Spanish · f_  French · h_  Hindi · i_  Italian\n"
        " j{f,m}_*  Japanese (jf_alpha, jm_kumo)\n"
        " p{f,m}_*  Brazilian Portuguese · z{f,m}_*  Mandarin Chinese"),
        _line_edit("tts_speaker")))
    t_body.addWidget(_row(_label("Speed",
        "Synthesis rate. 1.0 = normal, >1 = faster, <1 = slower."),
        _slider_float("tts_length_scale", 0.5, 2.0, 0.05, suffix="x")))
    t_body.addWidget(_row(_label("Fallback Lang",
        "Phonemizer fallback language for text that doesn't match the "
        "voice prefix. a=Am-En, b=Br-En, e=es, f=fr, h=hi, i=it, j=ja, "
        "p=pt-br, z=zh. Voice prefix still wins per call."),
        _combo("tts_lang_code", [
            ("a", "a — American English"), ("b", "b — British English"),
            ("e", "e — Spanish"), ("f", "f — French"),
            ("h", "h — Hindi"), ("i", "i — Italian"),
            ("j", "j — Japanese"), ("p", "p — Portuguese (BR)"),
            ("z", "z — Mandarin"),
        ])))
    t_body.addWidget(_row(_label("Stream Audio",
        "Reply with chunked WAV — first audio plays in ~200 ms instead "
        "of waiting for the whole utterance. Big TTFB win for long text."),
        _checkbox("tts_stream", "Enabled")))
    t_body.addWidget(_row(_label("Warm Up On Load",
        "Run a dummy synth right after the pipeline loads so the FIRST "
        "real /v1/audio/speech call isn't slow."),
        _checkbox("tts_warmup", "Enabled")))
    t_body.addWidget(_row(_label("torch.compile",
        "JIT-compile the Kokoro model. ~15% steady-state speedup with a "
        "shorter first-call penalty than Whisper (Kokoro is only 82M)."),
        _checkbox("tts_torch_compile", "Enabled")))
    t_body.addWidget(_lifecycle_row(
        "Load", "Unload", "Reload",
        on_load=lambda: window.start_service("tts"),
        on_unload=lambda: window.stop_service("tts"),
        on_reload=lambda: window.restart_service("tts"),
        status_getter=lambda: _engine_status("tts"),
    ))
    layout.addWidget(t_card)

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
    raw_label = QLabel("Raw (STT)")
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
    copy_raw.setToolTip("Copy raw STT transcript to clipboard")
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

    LOG_FILES = [
        "voxtype.log", "voxtype.log.prev",
    ]
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
    # Scrollbar QSS inlined here because Qt's cascade stops descending
    # into a widget that sets its own stylesheet — the global QScrollBar
    # rule wouldn't reach this viewer's child scrollbars otherwise,
    # leaving them invisible (0-width, transparent handle).
    viewer.setStyleSheet(
        f"QPlainTextEdit {{ background: {BG_ELEV}; border: 1px solid {BORDER};"
        f" border-radius: 6px; font-family: 'JetBrains Mono', Consolas, monospace;"
        f" font-size: 11.5px; padding: 6px 8px; selection-background-color: {ACCENT};"
        f" selection-color: #000; }}"
        f"QScrollBar:vertical {{ background: #151b28; width: 14px; margin: 2px 0; border: none; }}"
        f"QScrollBar::handle:vertical {{ background: #4a5a82; border-radius: 5px; min-height: 28px; }}"
        f"QScrollBar::handle:vertical:hover {{ background: #6b82b8; }}"
        f"QScrollBar:horizontal {{ background: #151b28; height: 14px; margin: 0 2px; border: none; }}"
        f"QScrollBar::handle:horizontal {{ background: #4a5a82; border-radius: 5px; min-width: 28px; }}"
        f"QScrollBar::handle:horizontal:hover {{ background: #6b82b8; }}"
        f"QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}"
        f"QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}"
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
                 start_service: Callable[[str], None] | None = None,
                 stop_service: Callable[[str], None] | None = None,
                 restart_server: Callable[[], None] | None = None,
                 start_server: Callable[[], None] | None = None,
                 stop_server: Callable[[], None] | None = None,
                 capture_hotkey: Callable[[Callable], None] | None = None,
                 set_hotkey: Callable[[object], None] | None = None) -> None:
        super().__init__()
        self._restart_service = restart_service
        self._start_service = start_service
        self._stop_service = stop_service
        self._restart_server = restart_server
        self._start_server = start_server
        self._stop_server = stop_server
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

    def start_service(self, name: str) -> None:
        if self._start_service:
            self._start_service(name)

    def stop_service(self, name: str) -> None:
        if self._stop_service:
            self._stop_service(name)

    def start_server(self) -> None:
        if self._start_server:
            self._start_server()

    def stop_server(self) -> None:
        if self._stop_server:
            self._stop_server()

    def restart_server(self) -> None:
        if self._restart_server:
            self._restart_server()

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
