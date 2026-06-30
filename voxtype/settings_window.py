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

from PySide6.QtCore import Qt, QPoint, QTimer, Signal, QObject
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
    ("display",   "Display",    "🖥"),
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


def _slider_log(path: str, lo: float, hi: float, steps: int = 500) -> QWidget:
    """Logarithmic horizontal slider + live readout, persisted to `path`.

    `lo` and `hi` are the actual values in seconds (e.g. 0.01 and 1000.0).
    The slider position ranges from 0 to `steps` linearly, which is mapped
    logarithmically to [lo, hi].
    """
    import math
    from PySide6.QtWidgets import QSlider
    s = config.load()
    cur = float(getattr(s, path, lo))

    # Ensure cur is within bounds
    cur = max(lo, min(hi, cur))

    log_lo = math.log10(lo)
    log_hi = math.log10(hi)

    w = QWidget()
    l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(10)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, steps)
    slider.setSingleStep(1)

    # Calculate initial slider value from cur
    if cur > 0:
        log_cur = math.log10(cur)
        val_idx = int(round((log_cur - log_lo) / (log_hi - log_lo) * steps))
        slider.setValue(max(0, min(steps, val_idx)))
    else:
        slider.setValue(0)

    readout = QLabel()
    readout.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; min-width: 60px;")

    def format_val(val: float) -> str:
        if val < 1.0:
            ms = int(round(val * 1000))
            if ms < 10:
                ms = 10
            return f"{ms} ms"
        elif val < 10.0:
            return f"{val:.2f} s"
        elif val < 100.0:
            return f"{val:.1f} s"
        else:
            return f"{int(round(val))} s"

    readout.setText(format_val(cur))

    def _on(i: int) -> None:
        log_val = log_lo + (i / steps) * (log_hi - log_lo)
        val = 10 ** log_val
        # Round the value to be nice
        if val < 0.1:
            val = round(val * 200) / 200  # 5ms steps
            val = max(lo, min(hi, val))
        elif val < 1.0:
            val = round(val * 100) / 100  # 10ms steps
        elif val < 10.0:
            val = round(val * 20) / 20    # 50ms steps
        elif val < 100.0:
            val = round(val * 2) / 2      # 0.5s steps
        else:
            val = round(val / 10) * 10    # 10s steps
            val = max(lo, min(hi, val))

        readout.setText(format_val(val))
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

    cb_silence = _checkbox("auto_stop_on_silence", "Enabled")
    slider_silence = _slider_float("silence_duration_sec", 0.5, 5.0, 0.1)
    slider_silence.setEnabled(cb_silence.isChecked())
    cb_silence.toggled.connect(slider_silence.setEnabled)

    body.addWidget(_row(_label("Auto-Stop On Silence",
        "In hold-mode, stop recording if the mic stays quiet for a few seconds."),
        cb_silence))
    body.addWidget(_row(_label("Silence Duration",
        "How many seconds of continuous quiet before auto-stop fires. "
        "The timer only starts after VoxType has heard at least one speech "
        "frame — a silent mic won't insta-stop."),
        slider_silence))
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

    # ── Voice Activation card ──────────────────────────────────────
    va_card, va_body = _card("Voice Activation",
        "Start dictating hands-free when you say a start word")

    cb_voice = _checkbox("voice_activation_enabled", "Enabled")
    le_words = _line_edit("voice_start_words")
    le_words.setPlaceholderText("computer, hey vox")
    le_words.setEnabled(cb_voice.isChecked())

    def _on_voice_toggle(enabled: bool) -> None:
        # _checkbox already patched the setting; also (re)start the live
        # listener and gate the words field.
        le_words.setEnabled(enabled)
        try:
            window.set_voice_activation(enabled)
        except Exception as exc:
            log.warning("set_voice_activation failed: %s", exc)
    cb_voice.toggled.connect(_on_voice_toggle)

    va_body.addWidget(_row(_label("Voice Activation",
        "Listen continuously and begin recording when you speak a start "
        "word. The mic and STT model stay active while listening, and "
        "voice-triggered captures auto-stop on silence. The hotkey still "
        "works as usual."),
        cb_voice))
    va_body.addWidget(_row(_label("Start Words",
        "Comma-separated trigger phrases. A short utterance that begins "
        "with any of these starts a dictation; the start word itself is "
        "not transcribed into your text."),
        le_words))
    va_body.addWidget(_row(_label("Match Anywhere",
        "Off (default): the utterance must START with a start word. On: "
        "trigger if a start word appears anywhere in the utterance."),
        _checkbox("voice_match_contains", "Enabled")))

    layout.addWidget(va_card)

    # ── Recording Sounds card ──────────────────────────────────────
    sound_card, sound_body = _card("Recording Sounds",
        "Audio cues for record / stop / done")
    
    cb_sounds = _checkbox("sounds_enabled", "Enabled")
    slider_dur = _slider_float("sound_duration_sec", 0.5, 1.0, 0.05)
    
    start_row = _sound_file_row("sound_start", "start", "sound_start_enabled")
    stop_row = _sound_file_row("sound_stop", "stop", "sound_stop_enabled")
    done_row = _sound_file_row("sound_done", "done", "sound_done_enabled")
    
    # Dynamic linkage
    def _link_sounds(enabled: bool) -> None:
        slider_dur.setEnabled(enabled)
        start_row.update_master(enabled)
        stop_row.update_master(enabled)
        done_row.update_master(enabled)
        
    cb_sounds.toggled.connect(_link_sounds)
    _link_sounds(cb_sounds.isChecked())
    
    sound_body.addWidget(_row(_label("Sounds",
        "Play short audio cues on record start, record stop, and "
        "transcript-typed."),
        cb_sounds))
    sound_body.addWidget(_row(_label("Sound Duration",
        "How long to play the audio cues. Supports range from 10 ms to 1000 s."),
        slider_dur))
    sound_body.addWidget(_row(_label("Start Recording",
        "Played when the hotkey is pressed. Empty = built-in tone."),
        start_row))
    sound_body.addWidget(_row(_label("Stop Recording",
        "Played when the hotkey is released (or silence auto-stop fires)."),
        stop_row))
    sound_body.addWidget(_row(_label("Processing Complete",
        "Played after the cleaned transcript has been pasted."),
        done_row))
    layout.addWidget(sound_card)
    layout.addStretch(1)
    return scroll


def _sound_file_row(path_field: str, cue: str, enabled_field: str) -> QWidget:
    """[Checkbox] [text field] [Browse…] [Test] [Reset]."""
    from PySide6.QtWidgets import QFileDialog
    from voxtype import sounds

    w = QWidget()
    h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)

    cb = QCheckBox("Enabled")
    s = config.load()
    cb.setChecked(bool(getattr(s, enabled_field, True)))

    le = QLineEdit()
    le.setText(str(getattr(s, path_field, "") or ""))
    le.setPlaceholderText("(built-in tone)")
    le.editingFinished.connect(lambda: config.patch(path_field, le.text()))

    browse = QPushButton("Browse…"); browse.setProperty("class", "ghost")
    browse.setFixedWidth(82)
    test = QPushButton("Test"); test.setProperty("class", "ghost")
    test.setFixedWidth(56)
    reset = QPushButton("Reset"); reset.setProperty("class", "ghost")
    reset.setFixedWidth(60)

    # Master enabled state property
    w.setProperty("master_enabled", True)

    def _sync():
        master_ok = bool(w.property("master_enabled"))
        row_ok = cb.isChecked()
        cb.setEnabled(master_ok)
        active = master_ok and row_ok
        le.setEnabled(active)
        browse.setEnabled(active)
        test.setEnabled(active)
        reset.setEnabled(active)

    def _on_cb_toggled(checked: bool) -> None:
        config.patch(enabled_field, checked)
        _sync()

    cb.toggled.connect(_on_cb_toggled)

    def _on_browse() -> None:
        fn, _ = QFileDialog.getOpenFileName(
            w, "Select sound file", le.text() or "",
            "Audio files (*.wav *.flac *.ogg *.mp3);;All files (*.*)",
        )
        if fn:
            le.setText(fn)
            config.patch(path_field, fn)

    def _on_test() -> None:
        sounds.play(cue, le.text().strip())

    def _on_reset() -> None:
        le.setText("")
        config.patch(path_field, "")

    browse.clicked.connect(_on_browse)
    test.clicked.connect(_on_test)
    reset.clicked.connect(_on_reset)

    h.addWidget(cb)
    h.addWidget(le, 1)
    h.addWidget(browse)
    h.addWidget(test)
    h.addWidget(reset)

    # Expose custom update function
    def update_master(enabled: bool) -> None:
        w.setProperty("master_enabled", enabled)
        _sync()

    w.update_master = update_master  # type: ignore[attr-defined]

    # Run initial sync
    _sync()
    return w


def _line_edit_static(text: str) -> QLineEdit:
    le = QLineEdit(text)
    le.setReadOnly(True)
    return le


class _DetectBridge(QObject):
    """Thread bridge for the Detect button. Worker thread `emit`s into
    `done`; Qt delivers the slot call on the GUI thread because the
    bridge instance is created there. `QTimer.singleShot` from a
    non-Qt thread is unreliable — Signal/Slot is the canonical fix.

    Payload: (valid, source, gated, family, family_label, error)
    """
    done = Signal(bool, str, bool, str, str, str)


def _detect_button(line_edit: QLineEdit, status_lbl: QLabel,
                    default_model: str, *, modality: str,
                    on_detected: Callable[[str], None] | None = None) -> QPushButton:
    """`Detect` button that VERIFIES the entered model id / path AND
    detects its family. `modality` ∈ {'stt', 'tts'}.

    On click:
      1. Read the model id (or fall back to `default_model`).
      2. Hand off to family_detect.verify_model_id in a worker —
         checks local path existence OR pings HF's `/api/models/<id>`.
      3. Worker emits the Detected signal — the slot updates the
         status pill on the Qt thread (✓ family / ✓ exists, unknown /
         🔒 gated / ✗ not found / ⚠ offline) and fires the
         on_detected callback so the card can rebuild its per-family
         widgets.
    """
    from voxtype.backends import family_detect as fd
    from voxtype.qt_theme import OK, WARN

    btn = QPushButton("Detect")
    btn.setProperty("class", "ghost")
    btn.setFixedWidth(62)
    bridge = _DetectBridge(btn)   # parent → lives on Qt thread

    def _set(text: str, color: str, tip: str = "") -> None:
        status_lbl.setText(text)
        status_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
        status_lbl.setToolTip(tip)

    def _on_done(valid: bool, source: str, gated: bool,
                  fam: str, label: str, err: str) -> None:
        if not valid:
            if gated:
                _set("🔒 gated",
                      WARN,
                      "HuggingFace repo requires sign-in. Run "
                      "`huggingface-cli login` in the venv first.")
            elif source == "local":
                _set("✗ path not found", WARN,
                      err or "Local path doesn't exist or has no "
                              "config.json / voices directory.")
            elif source == "hf":
                _set("✗ not found", WARN,
                      err or "Repo not found on HuggingFace.")
            else:
                _set("✗ invalid", WARN,
                      err or "Not a valid model path or HF repo id "
                              "(expected `<org>/<name>`).")
        elif fam:
            tag = "local" if source == "local" else "HF"
            _set(f"✓ {label or fam}", OK,
                  f"Verified on {tag}. Detected family: {fam}")
        else:
            tag = "local" if source == "local" else "HuggingFace"
            _set("✓ exists · unknown family", OK,
                  f"Found on {tag}, but couldn't recognise the "
                  f"architecture. The generic pipeline fallback "
                  f"will be tried at load time.")
        if on_detected is not None:
            try:
                on_detected(fam)
            except Exception:
                pass

    bridge.done.connect(_on_done)

    def _on_click() -> None:
        from threading import Thread
        model_id = (line_edit.text().strip() or default_model).strip()
        if not model_id:
            _set("(empty)", FG_MUTE)
            return
        _set("verifying…", FG_MUTE)

        def _worker() -> None:
            try:
                check = fd.verify_model_id(model_id, stt=(modality == "stt"))
                fam = check.family
                if fam:
                    label = (fd.stt_family_label(fam) if modality == "stt"
                             else fd.tts_family_label(fam))
                else:
                    label = ""
                bridge.done.emit(check.valid, check.source, check.gated,
                                  fam or "", label or "", check.error or "")
            except Exception as exc:
                log.warning("verify failed for %r: %s", model_id, exc)
                bridge.done.emit(False, "none", False, "", "", str(exc))

        Thread(target=_worker, daemon=True).start()

    btn.clicked.connect(_on_click)
    return btn


def _model_row(path_field: str, default_model: str, *,
                modality: str,
                on_detected: Callable[[str], None] | None = None) -> QWidget:
    """Model row: [text field] [Browse] [Detect] [family status pill].

    Synchronous repo-id heuristic runs on every textChanged so the
    family pill + Advanced widgets update instantly without waiting
    for the user to click Detect (Detect is now the FULL HF-API check
    for when the heuristic returns nothing)."""
    from PySide6.QtWidgets import QFileDialog
    from voxtype.backends import family_detect as fd

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
            w, "Select model file or directory", le.text() or "",
            "All files (*.*)",
        )
        if fn:
            le.setText(fn)
            config.patch(path_field, fn)
            _push_fast_detect(fn)

    browse.clicked.connect(_on_browse)

    status = QLabel("")
    status.setMinimumWidth(160)
    status.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px;")

    def _push_fast_detect(text: str) -> None:
        """Cheap repo-id detection (no network). Fires the on_detected
        callback so the card rebuilds family pill + voice picker."""
        from voxtype.qt_theme import OK
        text = (text or "").strip() or default_model
        if not text:
            return
        if modality == "stt":
            fam = fd.detect_stt_family_fast(text)
            label = fd.stt_family_label(fam) if fam else ""
        else:
            fam = fd.detect_tts_family_fast(text)
            label = fd.tts_family_label(fam) if fam else ""
        if fam:
            status.setText(f"✓ {label or fam}")
            status.setStyleSheet(f"color: {OK}; font-size: 11px;")
            status.setToolTip(f"Detected family: {fam}  (heuristic — "
                                f"click Detect to verify against HuggingFace)")
        else:
            status.setText("")
        if on_detected is not None:
            try:
                on_detected(fam or "")
            except Exception:
                pass

    le.textChanged.connect(_push_fast_detect)
    detect = _detect_button(le, status, default_model,
                              modality=modality, on_detected=on_detected)

    h.addWidget(le, 1)
    h.addWidget(browse)
    h.addWidget(detect)
    h.addWidget(status)

    # Initial detect from saved/default value
    _push_fast_detect(le.text())
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

    # Optimistic in-flight action so the row reacts on click instead of
    # waiting up to 1 s for the next poll. `action` ∈ {"load","unload",
    # "reload"}; `since` guards against a stuck transition.
    import time as _time
    pend: dict = {"action": None, "since": 0.0}
    _PENDING_TIMEOUT = 180.0

    def _mk(label: str, action: str, cb: Callable[[], None]) -> QPushButton:
        b = QPushButton(label)
        b.setProperty("class", "ghost")
        b.setFixedHeight(28)
        b.clicked.connect(lambda: _click(action, cb))
        return b

    def _click(action: str, cb: Callable[[], None]) -> None:
        pend["action"] = action
        pend["since"] = _time.monotonic()
        try:
            cb()
        except Exception as exc:
            log.error("lifecycle action failed: %s", exc)
            pend["action"] = None
        _refresh()  # paint the busy state immediately

    btn_load   = _mk(load_label,   "load",   on_load)
    btn_unload = _mk(unload_label, "unload", on_unload)
    btn_reload = _mk(reload_label, "reload", on_reload)
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
    _PEND_TEXT = {"load": "loading…", "unload": "unloading…",
                  "reload": "reloading…"}

    def _resolved(action: str, kind: str) -> bool:
        if kind == "err":
            return True
        if action in ("load", "reload"):
            return kind == "ok"
        if action == "unload":
            return kind in ("idle", "off")
        return True

    def _refresh() -> None:
        try:
            text, kind = status_getter()
        except Exception:
            text, kind = ("error", "err")
        action = pend["action"]
        if action is not None:
            if _resolved(action, kind) or (
                    _time.monotonic() - pend["since"] > _PENDING_TIMEOUT):
                pend["action"] = None
            else:
                text, kind = (_PEND_TEXT.get(action, "working…"), "busy")
        color, glyph = KINDS.get(kind, (FG_MUTE, "○"))
        pill.setText(f"{glyph} {text}")
        pill.setStyleSheet(f"color: {color}; font-size: 11.5px;")
        # While an action is in flight, lock all three buttons.
        if pend["action"] is not None:
            btn_load.setEnabled(False)
            btn_unload.setEnabled(False)
            btn_reload.setEnabled(False)
        else:
            btn_load.setEnabled(kind in ("idle", "off", "err"))
            btn_unload.setEnabled(kind in ("ok", "busy"))
            btn_reload.setEnabled(kind not in ("off",))

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


# ── Live-state tile (telecode-style) ─────────────────────────────────

def _make_progress_bar(ratio: float, label: str) -> QWidget:
    """Thin horizontal progress bar with a caption above it. `ratio` in
    [0, 1]. Mirrors telecode's status-tile progress viz."""
    from voxtype.qt_theme import WARN, BG_ELEV
    ratio = max(0.0, min(1.0, ratio))
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
    if label:
        cap = QLabel(label)
        cap.setStyleSheet(f"color: {FG_MUTE}; font-size: 10px;")
        v.addWidget(cap)
    track = QFrame()
    track.setFixedHeight(4)
    track.setStyleSheet(f"background: {BG_ELEV}; border-radius: 2px;")
    fh = QHBoxLayout(track)
    fh.setContentsMargins(0, 0, 0, 0); fh.setSpacing(0)
    fill = QFrame()
    fill.setStyleSheet(f"background: {WARN}; border-radius: 2px;")
    pct = int(round(ratio * 100))
    fh.addWidget(fill, max(1, pct))
    fh.addStretch(max(1, 100 - pct))
    v.addWidget(track)
    return w


def _live_state_tile(name: str) -> QWidget:
    """Telecode-style 'Live state' tile for an engine card.

    Shows the model's running state as a big value, the detected family
    as sub-text, and — while loaded with auto-unload enabled — a
    countdown progress bar ('Auto-unload in Ns'). Polled once a second
    off a QTimer parented to the tile, so it survives load/unload."""
    from voxtype.qt_theme import OK, ERR, WARN, FG, BG_ELEV

    tile = QFrame()
    tile.setObjectName("liveTile")

    root = QHBoxLayout(tile)
    root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

    bar = QFrame()
    bar.setFixedWidth(4)
    root.addWidget(bar)

    body_w = QWidget()
    body = QVBoxLayout(body_w)
    body.setContentsMargins(14, 12, 16, 12); body.setSpacing(5)
    root.addWidget(body_w, 1)

    hdr = QHBoxLayout(); hdr.setContentsMargins(0, 0, 0, 0); hdr.setSpacing(0)
    title = QLabel("LIVE STATE")
    title.setStyleSheet(f"color: {FG_MUTE}; font-size: 10px; "
                         f"letter-spacing: 1.5px; font-weight: 500;")
    dot = QLabel("●")
    hdr.addWidget(title); hdr.addStretch(1); hdr.addWidget(dot)
    body.addLayout(hdr)

    value = QLabel("—")
    value.setStyleSheet(f"color: {FG}; font-size: 20px; font-weight: 600;")
    body.addWidget(value)

    sub = QLabel("")
    sub.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
    sub.setWordWrap(True)
    body.addWidget(sub)

    viz_host = QWidget()
    viz_layout = QVBoxLayout(viz_host)
    viz_layout.setContentsMargins(0, 2, 0, 0); viz_layout.setSpacing(0)
    body.addWidget(viz_host)

    _COLOR = {"ok": OK, "busy": WARN, "err": ERR, "idle": FG_MUTE, "off": FG_MUTE}

    def _set_state(kind: str) -> None:
        c = _COLOR.get(kind, FG_MUTE)
        bar.setStyleSheet(f"background: {c}; border-top-left-radius: 8px; "
                          f"border-bottom-left-radius: 8px;")
        dot.setStyleSheet(f"color: {c}; font-size: 9px;")
        border = {
            "ok":   "rgba(86, 224, 194, 0.55)",
            "busy": "rgba(245, 165, 36, 0.50)",
            "err":  "rgba(255, 110, 110, 0.50)",
        }.get(kind, BORDER)
        tile.setStyleSheet(
            f"#liveTile {{ background: {BG_ELEV}; border: 1px solid {border}; "
            f"border-radius: 8px; }}")

    def _set_viz(widget: QWidget | None) -> None:
        while viz_layout.count():
            it = viz_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        if widget is not None:
            viz_layout.addWidget(widget)

    def _refresh() -> None:
        s_cfg = config.load()
        if not bool(getattr(s_cfg, f"{name}_enabled", False)):
            _set_state("off"); value.setText("Disabled"); sub.setText("")
            _set_viz(None); return
        # Read straight from the engine — its EngineStatus carries the
        # detected `family`, which process.get_status() drops. Fetch once
        # so status and the idle countdown stay consistent.
        eng = _engine_for_ui(name)
        try:
            st = eng.get_status()
        except Exception:
            st = None
        if st is None:
            _set_state("idle"); value.setText("Unloaded")
            sub.setText("Loads on first hotkey press"); _set_viz(None); return
        if st.last_error and not st.ready and not st.running:
            _set_state("err"); value.setText("Error")
            sub.setText(str(st.last_error)[:80]); _set_viz(None); return
        if st.ready:
            _set_state("ok"); value.setText("Ready")
            fam = getattr(st, "family", "") or ""
            sub.setText(f"{fam} loaded" if fam else "Model loaded")
            limit, rem = eng.idle_info()
            if limit > 0 and rem >= 0:
                _set_viz(_make_progress_bar(
                    rem / limit if limit else 0.0,
                    f"Auto-unload in {int(rem)}s"))
            else:
                _set_viz(None)
            return
        if st.running:
            _set_state("busy"); value.setText("Loading…")
            sub.setText("Downloading / initialising model"); _set_viz(None); return
        # Not loaded. Two-stage detail (mirrors telecode's docgraph tile):
        # stage 1 = weights (above, "Ready" + auto-unload bar); stage 2 =
        # the worker process / CUDA context. "Warm" = worker still alive
        # with weights freed (context cached, fast reload); "Idle" = worker
        # exited, context fully released.
        from voxtype.engine_host import get_host
        snap = get_host().cached_status()
        if snap.get("alive") and not snap.get("down"):
            _set_state("idle"); value.setText("Warm")
            sub.setText("Weights freed · GPU context cached")
            exit_rem = float(snap.get("exit_remaining", -1.0))
            exit_lim = float(snap.get("idle_exit_sec", 0) or 0)
            if exit_rem >= 0 and exit_lim > 0:
                _set_viz(_make_progress_bar(exit_rem / exit_lim,
                                            f"Release GPU in {int(exit_rem)}s"))
            else:
                _set_viz(None)
            return
        _set_state("idle"); value.setText("Idle")
        sub.setText("GPU fully released · loads on first use"); _set_viz(None)

    _set_state("idle")
    _refresh()
    timer = QTimer(tile)
    timer.setInterval(1000)
    timer.timeout.connect(_refresh)
    timer.start()
    return tile


def _engine_for_ui(name: str):
    """The live STT/TTS engine singleton — its get_status()/idle_info()
    expose `family` and the auto-unload countdown the tile needs."""
    if name == "stt":
        from voxtype.stt_engine import get_engine
    else:
        from voxtype.tts_engine import get_engine
    return get_engine()


# ── Spec-driven widget renderer ──────────────────────────────────────


def _render_option(spec, bag_path: str) -> QWidget:
    """Build a Qt widget from one OptionSpec. The widget reads its
    initial value from `<bag_path>.<spec.key>` and writes back on
    change via `config.patch()`. `bag_path` is "stt_opts" or
    "tts_opts" — the per-family options dict."""
    s = config.load()
    bag = getattr(s, bag_path, {}) or {}
    current = bag.get(spec.key, spec.default)
    full_path = f"{bag_path}.{spec.key}"

    if spec.kind == "bool":
        w = QCheckBox(spec.label)
        w.setChecked(bool(current))
        w.toggled.connect(lambda v: config.patch(full_path, bool(v)))
        return w
    if spec.kind == "enum":
        w = QComboBox()
        for val, lbl in spec.choices:
            w.addItem(lbl, val)
        idx = next((i for i, (v, _) in enumerate(spec.choices)
                     if v == current), 0)
        w.setCurrentIndex(idx)
        w.currentIndexChanged.connect(
            lambda i: config.patch(full_path, w.itemData(i))
        )
        return w
    if spec.kind == "int":
        w = QSpinBox()
        lo = int(spec.min if spec.min is not None else 0)
        hi = int(spec.max if spec.max is not None else 100)
        w.setRange(lo, hi)
        w.setValue(int(current))
        w.valueChanged.connect(lambda v: config.patch(full_path, int(v)))
        return w
    if spec.kind == "float":
        from PySide6.QtWidgets import QSlider
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)
        lo = float(spec.min if spec.min is not None else 0.0)
        hi = float(spec.max if spec.max is not None else 1.0)
        step = float(spec.step if spec.step is not None else 0.1)
        slider = QSlider(Qt.Orientation.Horizontal)
        n = max(1, int(round((hi - lo) / step)))
        slider.setRange(0, n)
        cur = float(current)
        slider.setValue(int(round((cur - lo) / step)))
        readout = QLabel(f"{cur:.2f}")
        readout.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; min-width: 52px;")
        def _on(i: int) -> None:
            val = round(lo + i * step, 4)
            readout.setText(f"{val:.2f}")
            config.patch(full_path, float(val))
        slider.valueChanged.connect(_on)
        layout.addWidget(slider, 1); layout.addWidget(readout)
        return wrap
    # "str" / "text" / unknown → text field
    w = QLineEdit()
    w.setText(str(current or ""))
    w.editingFinished.connect(lambda: config.patch(full_path, w.text()))
    return w


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

    layout.addWidget(_build_stt_card(window))
    layout.addWidget(_build_tts_card(window))
    layout.addStretch(1)
    return scroll


def _build_stt_card(window) -> QWidget:
    """STT card — single generic backend, auto-detects family.

    Layout: lifecycle controls + model picker at the top; universal
    knobs (language, device, dtype, warmup, torch.compile) in the
    middle; family-specific "Advanced" widgets at the bottom, rebuilt
    from the backend's `runtime_options()` whenever a family is
    detected (either via Detect button or after Load completes)."""
    from voxtype.backends import family_detect as fd
    from voxtype.stt_engine import (
        DEFAULT_MODEL as _STT_DEFAULT,
        language_combo_options as _stt_langs,
        all_language_codes as _stt_lang_codes,
        get_engine as _stt_engine,
    )

    card, body = _card("STT", "speech-to-text · auto-detects family")
    body.addWidget(_row(_label("Enabled"),
        _checkbox("stt_enabled", "Run STT")))
    body.addWidget(_row(_label("Auto-Start On Boot",
        "If off (default), the model loads on the first hotkey press. "
        "Turn on only if the first-transcribe warmup delay matters."),
        _checkbox("stt_auto_start", "Enabled")))
    body.addWidget(_row(_label("Idle Unload",
        "Unload the STT model after N seconds of no transcribe "
        "requests. 0 = never."),
        _spin_idle("stt_idle_unload_sec")))

    # ── Advanced container (rebuilt on family change) ────────────────
    adv_widget = QWidget()
    adv_layout = QVBoxLayout(adv_widget)
    adv_layout.setContentsMargins(0, 0, 0, 0); adv_layout.setSpacing(10)

    # The widgets below are rebuilt on family change. We track them so
    # they can be removed and re-added.
    state: dict = {"adv_rows": [], "current_family": ""}

    def _rebuild_advanced(family: str) -> None:
        """Tear down family-specific Advanced widgets and rebuild from
        the backend's runtime_options() spec for the new family.
        Family pill itself is handled inline in the model row."""
        # Remove old rows
        for row in state["adv_rows"]:
            adv_layout.removeWidget(row)
            row.deleteLater()
        state["adv_rows"] = []
        state["current_family"] = family
        # Build new rows from spec
        specs = fd.stt_runtime_options(family) if family else []
        for spec in specs:
            widget = _render_option(spec, "stt_opts")
            row = _row(_label(spec.label, spec.help), widget)
            adv_layout.addWidget(row)
            state["adv_rows"].append(row)
        # Toggle visibility of the universal language picker by
        # multilingual capability.
        caps = fd.stt_capabilities(family) if family else set()
        nonlocal_lang = state.get("lang_row")
        if nonlocal_lang is not None:
            nonlocal_lang.setVisible("multilingual" in caps or not family)
        # Dtype row visibility
        dtype_row = state.get("dtype_row")
        if dtype_row is not None:
            dtype_row.setVisible("dtype" in caps or not family)
        compile_row = state.get("compile_row")
        if compile_row is not None:
            compile_row.setVisible("torch_compile" in caps or not family)

    # Model field with Detect callback that rebuilds advanced widgets.
    body.addWidget(_row(_label("Model",
        "HuggingFace repo ID (auto-downloaded) or local path. Paste "
        "anything — Whisper, Wav2Vec2, MMS, Seamless, Moonshine, "
        "SpeechT5, … — the family is auto-detected. Empty = use the "
        "built-in default shown as placeholder."),
        _model_row("stt_model_path", _STT_DEFAULT,
                    modality="stt", on_detected=_rebuild_advanced)))

    body.addWidget(_row(_label("Device",
        "Falls back to CPU automatically if torch.cuda.is_available() is False."),
        _combo("stt_device", [("cpu", "CPU"), ("cuda", "GPU (CUDA)")])))

    # Language — universal, but hidden for single-lang families
    if str(getattr(config.load(), "stt_language", "")) not in _stt_lang_codes():
        config.patch("stt_language", "en")
    lang_row = _row(_label("Language",
        "Decoder hint for multilingual models (Whisper, MMS, Seamless). "
        "Auto-detect lets Whisper guess. Single-language families "
        "ignore this field."),
        _combo("stt_language", _stt_langs()))
    body.addWidget(lang_row); state["lang_row"] = lang_row

    # Precision — gated by supports("dtype")
    dtype_row = _row(_label("Precision",
        "Inference dtype. auto = fp16 on GPU, fp32 on CPU. bf16 needs "
        "Ampere+ (RTX 30xx / A100+)."),
        _combo("stt_dtype", [
            ("auto", "Auto"), ("fp16", "fp16 (GPU fast)"),
            ("bf16", "bf16 (Ampere+)"), ("fp32", "fp32 (accurate)"),
        ]))
    body.addWidget(dtype_row); state["dtype_row"] = dtype_row

    # Attention implementation — universal across all transformers families.
    attn_row = _row(_label("Attention",
        "Attention backend. auto = let transformers pick (sdpa on modern "
        "versions). flash_attention_2 needs fp16/bf16 + Ampere+ AND the "
        "flash-attn wheel installed (see setup.ps1 -FlashAttn)."),
        _combo("stt_attn_impl", [
            ("auto", "Auto"), ("sdpa", "SDPA (default)"),
            ("flash_attention_2", "Flash-Attn 2"),
            ("eager", "Eager (compat)"),
        ]))
    body.addWidget(attn_row); state["attn_row"] = attn_row

    # ── Advanced (per-family) ────────────────────────────────────────
    body.addWidget(QLabel(""))  # spacer
    adv_header = QLabel("Advanced (per-family)")
    adv_header.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px; "
                              f"text-transform: uppercase; letter-spacing: 1px;")
    body.addWidget(adv_header)
    body.addWidget(adv_widget)

    body.addWidget(_row(_label("Warm Up On Load",
        "Run a dummy inference after load so the FIRST real call "
        "isn't slow."),
        _checkbox("stt_warmup", "Enabled")))

    compile_row = _row(_label("torch.compile",
        "JIT-compile the model for ~20-40% steady-state speedup. Adds "
        "~30 s to the FIRST inference (one-time compile). Leave off "
        "unless you transcribe constantly."),
        _checkbox("stt_torch_compile", "Enabled"))
    body.addWidget(compile_row); state["compile_row"] = compile_row

    body.addWidget(_live_state_tile("stt"))
    body.addWidget(_lifecycle_row(
        "Load", "Unload", "Reload",
        on_load=lambda: window.start_service("stt"),
        on_unload=lambda: window.stop_service("stt"),
        on_reload=lambda: window.restart_service("stt"),
        status_getter=lambda: _engine_status("stt"),
    ))

    # ── Initial state ───────────────────────────────────────────────
    # Two sources: synchronous repo-id heuristic on the saved/default
    # model id (no network), then live engine.get_backend() for the
    # case where the model is already loaded with a confirmed family.
    _initial = (str(getattr(config.load(), "stt_model_path", ""))
                  or _STT_DEFAULT)
    _initial_fam = fd.detect_stt_family_fast(_initial)
    if _initial_fam:
        _rebuild_advanced(_initial_fam)

    def _poll_family() -> None:
        """Catch the family the engine confirms after a successful Load.
        Engine status callbacks fire on the executor thread; touching
        Qt widgets from there is unsafe, so we poll instead."""
        try:
            be = _stt_engine().get_backend()
            fam = be.detected_family() if be is not None else ""
            if fam and fam != state.get("current_family"):
                _rebuild_advanced(fam)
        except Exception:
            pass

    poll = QTimer(card)
    poll.setInterval(1500)
    poll.timeout.connect(_poll_family)
    poll.start()

    return card


def _build_tts_card(window) -> QWidget:
    """TTS card — single generic backend, auto-detects family."""
    from voxtype.backends import family_detect as fd
    from voxtype.tts_engine import (
        DEFAULT_MODEL as _TTS_DEFAULT,
        get_engine as _tts_engine,
    )

    card, body = _card("TTS", "text-to-speech · auto-detects family")
    body.addWidget(_row(_label("Enabled"),
        _checkbox("tts_enabled", "Run TTS")))
    body.addWidget(_row(_label("Auto-Start On Boot"),
        _checkbox("tts_auto_start", "Enabled")))
    body.addWidget(_row(_label("Idle Unload",
        "Unload the TTS model after N seconds of no synthesise calls. "
        "0 = never."),
        _spin_idle("tts_idle_unload_sec")))

    # Per-family Advanced container
    adv_widget = QWidget()
    adv_layout = QVBoxLayout(adv_widget)
    adv_layout.setContentsMargins(0, 0, 0, 0); adv_layout.setSpacing(10)

    state: dict = {"adv_rows": [], "current_family": ""}

    # Voice + speed widgets need to be rebuilt too — voice catalog
    # depends on the family.
    voice_row_holder = QWidget()
    voice_row_layout = QVBoxLayout(voice_row_holder)
    voice_row_layout.setContentsMargins(0, 0, 0, 0); voice_row_layout.setSpacing(0)

    def _rebuild_voice_picker(family: str) -> None:
        """Rebuild the voice combo. Static catalogs in family_detect
        cover Kokoro (54 voices), Bark (preset speakers), Parler
        (style presets), and SpeechT5 (default x-vectors) — no engine
        load required. Families with implicit voices (VITS) or no
        catalog (generic fallback) get a free-text input."""
        # Clear holder
        while voice_row_layout.count():
            child = voice_row_layout.takeAt(0).widget()
            if child is not None:
                child.deleteLater()
        voices = fd.tts_voices_for_family(family) if family else []
        # Fall through to the loaded backend's catalog if family
        # doesn't have a static one (e.g. SpeechT5 voices the user
        # added by typing).
        if not voices:
            from voxtype.tts_engine import get_engine as _eng
            be = _eng().get_backend()
            if be is not None:
                voices = be.voices()
        if voices:
            opts = [(v.voice_id,
                     f"{v.voice_id}  ·  {v.language} · "
                     f"{v.gender or '—'} · {v.display_name}")
                    for v in voices]
            # Snap the saved value to the catalog default if it's not
            # in the list — avoids the dropdown silently picking
            # entry-0 with a mismatched stored value.
            cur = str(getattr(config.load(), "tts_voice", "") or "")
            if cur not in {v.voice_id for v in voices}:
                config.patch("tts_voice", voices[0].voice_id)
            combo = _combo("tts_voice", opts)
            new_row = _row(_label("Voice",
                "Voice catalog from the detected family."),
                combo)
        else:
            # Unknown family or no static catalog — text field.
            le = _line_edit("tts_voice")
            new_row = _row(_label("Voice",
                "Voice id. Pick a model with a known catalog "
                "(Kokoro / Bark / Parler / SpeechT5) or type a "
                "backend-specific voice."),
                le)
        voice_row_layout.addWidget(new_row)

    def _rebuild_advanced(family: str) -> None:
        # Family pill itself is handled inline in the model row.
        # Tear down old rows
        for row in state["adv_rows"]:
            adv_layout.removeWidget(row)
            row.deleteLater()
        state["adv_rows"] = []
        state["current_family"] = family
        # Build new
        specs = fd.tts_runtime_options(family) if family else []
        for spec in specs:
            widget = _render_option(spec, "tts_opts")
            row = _row(_label(spec.label, spec.help), widget)
            adv_layout.addWidget(row)
            state["adv_rows"].append(row)
        # Universal-gated visibility
        caps = fd.tts_capabilities(family) if family else set()
        speed_row = state.get("speed_row")
        if speed_row is not None:
            speed_row.setVisible("speed" in caps or not family)
        stream_row = state.get("stream_row")
        if stream_row is not None:
            stream_row.setVisible("stream" in caps or not family)
        compile_row = state.get("compile_row")
        if compile_row is not None:
            compile_row.setVisible("torch_compile" in caps or not family)
        # Rebuild voice picker — catalog may have changed.
        _rebuild_voice_picker(family)

    body.addWidget(_row(_label("Model",
        "HuggingFace repo ID (auto-downloaded) or local path. Paste "
        "anything — Kokoro, MMS-TTS, SpeechT5, Bark, Parler, … — the "
        "family is auto-detected."),
        _model_row("tts_model_path", _TTS_DEFAULT,
                    modality="tts", on_detected=_rebuild_advanced)))

    body.addWidget(_row(_label("Device",
        "Falls back to CPU automatically if torch.cuda.is_available() is False."),
        _combo("tts_device", [("cpu", "CPU"), ("cuda", "GPU (CUDA)")])))

    # Voice (rebuilds with family — initial state populated by the
    # final detect-from-saved-model pass at the bottom of the card)
    body.addWidget(voice_row_holder)

    # Speed (universal-gated)
    speed_row = _row(_label("Speed",
        "Synthesis rate. 1.0 = normal, >1 = faster, <1 = slower."),
        _slider_float("tts_speed", 0.5, 2.0, 0.05, suffix="x"))
    body.addWidget(speed_row); state["speed_row"] = speed_row

    # Attention implementation (universal across transformers families).
    attn_row = _row(_label("Attention",
        "Attention backend. auto = transformers default (sdpa). "
        "flash_attention_2 needs fp16/bf16 + Ampere+ + the flash-attn "
        "wheel (setup.ps1 -FlashAttn)."),
        _combo("tts_attn_impl", [
            ("auto", "Auto"), ("sdpa", "SDPA (default)"),
            ("flash_attention_2", "Flash-Attn 2"),
            ("eager", "Eager (compat)"),
        ]))
    body.addWidget(attn_row); state["attn_row"] = attn_row

    # Seed — universal RNG for sampling-based families.
    seed_row = _row(_label("Seed",
        "RNG seed for sampling-based TTS (VITS, Bark, Parler, Orpheus, "
        "Higgs). -1 = random. Set a fixed value for reproducible renders."),
        _spin("tts_seed", -1, 2_147_483_647))
    body.addWidget(seed_row); state["seed_row"] = seed_row

    # ── Advanced ────────────────────────────────────────────────────
    body.addWidget(QLabel(""))
    adv_header = QLabel("Advanced (per-family)")
    adv_header.setStyleSheet(f"color: {FG_MUTE}; font-size: 11px; "
                              f"text-transform: uppercase; letter-spacing: 1px;")
    body.addWidget(adv_header)
    body.addWidget(adv_widget)

    stream_row = _row(_label("Stream Audio",
        "Reply with chunked WAV — first audio plays in ~200 ms. "
        "Only honoured by backends that support streaming (Kokoro)."),
        _checkbox("tts_stream", "Enabled"))
    body.addWidget(stream_row); state["stream_row"] = stream_row

    body.addWidget(_row(_label("Warm Up On Load",
        "Run a dummy synth after the pipeline loads so the FIRST "
        "real call isn't slow."),
        _checkbox("tts_warmup", "Enabled")))

    compile_row = _row(_label("torch.compile",
        "JIT-compile the model for steady-state speedup."),
        _checkbox("tts_torch_compile", "Enabled"))
    body.addWidget(compile_row); state["compile_row"] = compile_row

    body.addWidget(_live_state_tile("tts"))
    body.addWidget(_lifecycle_row(
        "Load", "Unload", "Reload",
        on_load=lambda: window.start_service("tts"),
        on_unload=lambda: window.stop_service("tts"),
        on_reload=lambda: window.restart_service("tts"),
        status_getter=lambda: _engine_status("tts"),
    ))

    # Initial detect from saved/default model id (cheap, no network).
    _initial = (str(getattr(config.load(), "tts_model_path", ""))
                  or _TTS_DEFAULT)
    _initial_fam = fd.detect_tts_family_fast(_initial)
    if _initial_fam:
        _rebuild_advanced(_initial_fam)
    else:
        # No family detected yet → render a plain text input so the
        # Voice row isn't blank.
        _rebuild_voice_picker("")

    def _poll_family() -> None:
        try:
            be = _tts_engine().get_backend()
            fam = be.detected_family() if be is not None else ""
            if fam and fam != state.get("current_family"):
                _rebuild_advanced(fam)
        except Exception:
            pass

    poll = QTimer(card)
    poll.setInterval(1500)
    poll.timeout.connect(_poll_family)
    poll.start()

    return card


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


def _build_display(window) -> QWidget:
    """Display section — currently just the OLED burn-in guard.

    The user sets one knob (black flashes per second); the per-flash
    duration is derived from the auto-detected refresh rate. Toggling
    either control patches config then calls window.set_oled_guard() so
    the live guard re-applies immediately (mirrors the voice-activation
    plumbing)."""
    from voxtype.oled_guard import primary_refresh_rate

    scroll, _, layout = _page()
    card, body = _card("OLED Burn-In Guard",
                        "Flash a black frame to rest OLED pixels")

    def _notify(*_args) -> None:
        try:
            window.set_oled_guard()
        except Exception as exc:
            log.warning("set_oled_guard failed: %s", exc)

    cb = _checkbox("oled_guard_enabled", "Enabled")
    cb.toggled.connect(_notify)

    # Flashes/sec — discrete presets matching the spec's guide table.
    # Stored as an int, so build the combo inline (the _combo helper
    # compares string-typed itemData against the value).
    rate = QComboBox()
    cur = int(getattr(config.load(), "oled_flashes_per_sec", 2))
    for val, lbl in [
        (1, "1 / sec — very gentle, invisible"),
        (2, "2 / sec — balanced (default)"),
        (4, "4 / sec — aggressive"),
        (6, "6 / sec — very aggressive, visible flicker"),
    ]:
        rate.addItem(lbl, val)
    rate.setCurrentIndex(next((i for i in range(rate.count())
                               if rate.itemData(i) == cur), 1))

    def _on_rate(i: int) -> None:
        config.patch("oled_flashes_per_sec", int(rate.itemData(i)))
        _notify()
    rate.currentIndexChanged.connect(_on_rate)

    # Darkness — full black is the most noticeable. A translucent dim is
    # far gentler. Stored 0.05–1.0; shown as a percentage.
    from PySide6.QtWidgets import QSlider
    dark = QWidget()
    dl = QHBoxLayout(dark); dl.setContentsMargins(0, 0, 0, 0); dl.setSpacing(10)
    cur_op = float(getattr(config.load(), "oled_flash_opacity", 1.0))
    op_slider = QSlider(Qt.Orientation.Horizontal)
    op_slider.setRange(10, 100)
    op_slider.setValue(int(round(cur_op * 100)))
    op_read = QLabel(f"{int(round(cur_op * 100))}%")
    op_read.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; min-width: 52px;")

    def _on_op(v: int) -> None:
        op_read.setText(f"{v}%")
        config.patch("oled_flash_opacity", round(v / 100.0, 2))
        _notify()
    op_slider.valueChanged.connect(_on_op)
    dl.addWidget(op_slider, 1); dl.addWidget(op_read)

    rr = primary_refresh_rate()
    rr_lbl = QLabel(f"{rr:.0f} Hz  ·  ~{1000.0 / rr:.1f} ms per black frame")
    rr_lbl.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")

    body.addWidget(_row(_label("OLED Guard",
        "Flash a fullscreen black frame a few times per second so OLED "
        "pixels get a brief, regular rest. The frame never steals focus "
        "or blocks clicks. Off by default — it is a mild, panel-dependent "
        "flicker, not a guaranteed burn-in cure."),
        cb))
    body.addWidget(_row(_label("Black Flashes / sec",
        "How often the black frame appears. Each flash lasts a single "
        "display frame, so higher refresh rates flash less noticeably "
        "while keeping the same rest cadence."),
        rate))
    body.addWidget(_row(_label("Flash Darkness",
        "100% = full black (pixels fully off — maximum rest, most "
        "noticeable). Lower values dim the screen instead of blacking it "
        "out: gentler and far less visible, with proportionally less "
        "pixel rest. Try ~40-60% if a full black flash is distracting."),
        dark))
    body.addWidget(_row(_label("Display Refresh",
        "Auto-detected primary display. Multi-monitor setups are guarded "
        "on the primary display only."),
        rr_lbl))

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
                 set_hotkey: Callable[[object], None] | None = None,
                 set_voice_activation: Callable[[bool], None] | None = None,
                 set_oled_guard: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._restart_service = restart_service
        self._start_service = start_service
        self._stop_service = stop_service
        self._restart_server = restart_server
        self._start_server = start_server
        self._stop_server = stop_server
        self._capture_hotkey = capture_hotkey
        self._set_hotkey = set_hotkey
        self._set_voice_activation = set_voice_activation
        self._set_oled_guard = set_oled_guard
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

    def set_voice_activation(self, enabled: bool) -> None:
        if self._set_voice_activation:
            self._set_voice_activation(bool(enabled))

    def set_oled_guard(self) -> None:
        if self._set_oled_guard:
            self._set_oled_guard()

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
            elif sid == "display":
                w = _build_display(self)
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
