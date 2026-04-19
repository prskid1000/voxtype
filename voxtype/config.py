"""Settings file I/O — atomic write + hot-reload, mirroring telecode's
config.py pattern.

Storage lives alongside the source tree under `voxtype/data/`
(repo-relative), so settings, history, and logs travel with the
checkout. The `data/` dir is in .gitignore. Users who need to move the
storage elsewhere can set the VOXTYPE_DATA_DIR environment variable.

Resolved paths:
  {data_dir}/settings.json
  {data_dir}/history.json
  {data_dir}/voxtype.log   (rotated to voxtype.log.prev on restart)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from voxtype.types import AppSettings

log = logging.getLogger("voxtype.config")

# Default: voxtype/data/ in the repo. Override with $VOXTYPE_DATA_DIR.
_DEFAULT_ROOT = Path(__file__).resolve().parent / "data"
_ROOT = Path(os.environ.get("VOXTYPE_DATA_DIR", str(_DEFAULT_ROOT)))
_SETTINGS_PATH = _ROOT / "settings.json"
_LOCK = threading.Lock()
_CACHE: AppSettings | None = None


def data_dir() -> Path:
    _ROOT.mkdir(parents=True, exist_ok=True)
    return _ROOT


def settings_path() -> Path:
    return _SETTINGS_PATH


def load() -> AppSettings:
    """Read settings from disk; fall back to defaults on any error."""
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            _CACHE = AppSettings.from_json(raw)
        except FileNotFoundError:
            _CACHE = AppSettings()
            _save_locked(_CACHE)
        except Exception as exc:
            log.warning("settings.json unreadable (%s); using defaults", exc)
            _CACHE = AppSettings()
        return _CACHE


def save(settings: AppSettings) -> None:
    """Atomic write + refresh cache."""
    with _LOCK:
        _save_locked(settings)


def reload() -> AppSettings:
    """Force a re-read from disk."""
    global _CACHE
    with _LOCK:
        _CACHE = None
    return load()


def patch(path: str, value: Any) -> None:
    """Apply a dotted-path patch + persist. Matches telecode's tray
    patch_settings() signature so UI code feels familiar.

    Only known top-level fields are accepted — unknown keys are ignored."""
    s = load()
    keys = path.split(".")
    if keys[0] == "hotkey":
        target = s.hotkey
        for k in keys[1:-1]:
            target = getattr(target, k)
        setattr(target, keys[-1], value)
    elif len(keys) == 1 and hasattr(s, keys[0]):
        setattr(s, keys[0], value)
    else:
        log.warning("patch: unknown settings path %r", path)
        return
    save(s)


# ── Internals ────────────────────────────────────────────────────────

def _save_locked(settings: AppSettings) -> None:
    global _CACHE
    data_dir()
    tmp = _SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(settings.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, _SETTINGS_PATH)
    _CACHE = settings
