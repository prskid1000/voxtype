"""Global hotkey listener.

Ported from voxtype/src/main/hotkey.ts. Replaces uiohook-napi with
pynput.keyboard.Listener.

Behaviour:
  - `hold` mode: fire on_activate when the configured combo becomes held,
    on_deactivate when any combo key is released.
  - `toggle` mode: fire on_activate on first activation, on_deactivate on
    the next activation.
  - Auto-repeat keydowns are suppressed.
  - Stale-key timer expires any keys held longer than 5 s (Windows Start
    menu sometimes eats the keyup for Meta/Win).
  - Capture mode: `capture(callback)` waits for the next 1–2 keypresses
    and resolves them into a HotkeyCombo.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from pynput import keyboard

from voxtype.types import HotkeyCombo, HotkeyMode

log = logging.getLogger("voxtype.hotkey")

STALE_KEY_MS = 5000
STALE_CHECK_INTERVAL = 2.0


def _key_name(k) -> str:
    """Canonical string name used in settings + labels."""
    if hasattr(k, "name"):  # Key.ctrl_l, Key.cmd, etc.
        n = k.name
        # Collapse L/R variants
        if n in ("ctrl_l", "ctrl_r"):   return "ctrl"
        if n in ("shift_l", "shift_r"): return "shift"
        if n in ("alt_l", "alt_r", "alt_gr"): return "alt"
        if n in ("cmd", "cmd_l", "cmd_r"): return "cmd"
        return n
    if hasattr(k, "char") and k.char:
        return k.char.lower()
    return str(k)


def _label(name: str) -> str:
    """Human-friendly label for a canonical key name."""
    specials = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt",
                "cmd": "Win", "space": "Space", "enter": "Enter",
                "tab": "Tab", "esc": "Esc", "caps_lock": "CapsLock",
                "backspace": "Backspace", "delete": "Delete",
                "page_up": "PageUp", "page_down": "PageDown",
                "home": "Home", "end": "End",
                "up": "Up", "down": "Down", "left": "Left", "right": "Right"}
    if name in specials:
        return specials[name]
    if name.startswith("f") and name[1:].isdigit():
        return name.upper()
    if len(name) == 1:
        return name.upper()
    return name.capitalize()


class HotkeyListener:
    def __init__(self,
                 on_activate: Callable[[], None],
                 on_deactivate: Callable[[], None]) -> None:
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._mode: HotkeyMode = "hold"
        self._combo = HotkeyCombo()
        self._held: dict[str, float] = {}  # name → first-seen monotonic ts
        self._active = False
        self._listener: keyboard.Listener | None = None
        self._stale_timer: threading.Timer | None = None
        self._capture_cb: Callable[[HotkeyCombo], None] | None = None
        self._capture_keys: list[str] = []
        self._capture_ready = False
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────

    def set_mode(self, mode: HotkeyMode) -> None:
        with self._lock:
            self._mode = mode

    def set_combo(self, combo: HotkeyCombo) -> None:
        with self._lock:
            self._combo = combo

    def capture(self, callback: Callable[[HotkeyCombo], None]) -> None:
        """Wait for the next 1–2 keys and return them as a HotkeyCombo.
        Arms only after all currently-held keys are released."""
        with self._lock:
            self._capture_cb = callback
            self._capture_keys.clear()
            self._capture_ready = len(self._held) == 0

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release, suppress=False,
        )
        self._listener.start()
        self._schedule_stale_check()
        log.info("hotkey listener started (mode=%s, combo=%s)",
                 self._mode, self._combo.label)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._stale_timer is not None:
            self._stale_timer.cancel()
            self._stale_timer = None
        with self._lock:
            self._held.clear()
            self._active = False

    # ── Internals ────────────────────────────────────────────────────

    def _schedule_stale_check(self) -> None:
        def _tick():
            now = time.monotonic()
            with self._lock:
                stale = [k for k, ts in self._held.items()
                         if (now - ts) * 1000 > STALE_KEY_MS]
                for k in stale:
                    self._held.pop(k, None)
                    log.debug("cleared stale key %r", k)
            self._schedule_stale_check()
        t = threading.Timer(STALE_CHECK_INTERVAL, _tick)
        t.daemon = True
        t.start()
        self._stale_timer = t

    def _on_press(self, key) -> None:
        name = _key_name(key)
        with self._lock:
            # Refresh timestamp so genuinely-held keys don't expire.
            is_new = name not in self._held
            self._held[name] = time.monotonic()
            if not is_new:
                return  # auto-repeat — ignore

            # Capture mode
            if self._capture_cb is not None:
                if not self._capture_ready:
                    return
                if name not in self._capture_keys:
                    self._capture_keys.append(name)
                if len(self._capture_keys) >= 2:
                    k1, k2 = self._capture_keys[0], self._capture_keys[1]
                    combo = HotkeyCombo(key1=k1, key2=k2,
                                         label=f"{_label(k1)} + {_label(k2)}")
                    cb = self._capture_cb
                    self._capture_cb = None
                    self._capture_keys.clear()
                    # Fire callback outside lock
                    threading.Thread(target=cb, args=(combo,), daemon=True).start()
                return

            # Normal activation check
            combo = self._combo
            k1_match = combo.key1 in self._held
            k2_match = combo.key2 is None or combo.key2 in self._held
            if not (k1_match and k2_match):
                return
            if self._mode == "hold":
                if not self._active:
                    self._active = True
                    fire = self._on_activate
                    threading.Thread(target=fire, daemon=True).start()
            else:  # toggle
                if not self._active:
                    self._active = True
                    fire = self._on_activate
                else:
                    self._active = False
                    fire = self._on_deactivate
                threading.Thread(target=fire, daemon=True).start()

    def _on_release(self, key) -> None:
        name = _key_name(key)
        with self._lock:
            self._held.pop(name, None)

            # Capture-mode arm-after-release
            if self._capture_cb is not None and not self._capture_ready and not self._held:
                self._capture_ready = True
                return

            # Capture single-key finalize
            if (self._capture_cb is not None and self._capture_ready
                    and len(self._capture_keys) == 1 and not self._held):
                k1 = self._capture_keys[0]
                combo = HotkeyCombo(key1=k1, key2=None, label=_label(k1))
                cb = self._capture_cb
                self._capture_cb = None
                self._capture_keys.clear()
                threading.Thread(target=cb, args=(combo,), daemon=True).start()
                return

            # Hold-mode deactivate
            if self._mode == "hold" and self._active:
                if name == self._combo.key1 or name == self._combo.key2:
                    self._active = False
                    fire = self._on_deactivate
                    threading.Thread(target=fire, daemon=True).start()
