"""Transcription history — append-only JSON file under the data dir.

Bounded to the most recent N entries to keep the settings-UI list render
fast. Matches the shape of voxtype/src/main/history.ts."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from voxtype import config

log = logging.getLogger("voxtype.history")

MAX_ENTRIES = 500


@dataclass
class Entry:
    timestamp: float            # unix seconds
    raw: str                    # Whisper transcript
    final: str                  # after LLM enhance (== raw if disabled)
    enhanced: bool              # did LLM enhance run?
    duration_ms: int            # time from hotkey-release to paste
    app: str = ""               # foreground window title at paste time


def _path() -> Path:
    return config.data_dir() / "history.json"


def load() -> list[Entry]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("history.json unreadable (%s)", exc)
        return []
    out: list[Entry] = []
    for item in raw:
        try:
            out.append(Entry(**item))
        except TypeError:
            continue
    return out


def add(entry: Entry) -> None:
    entries = load()
    entries.append(entry)
    # Trim to the newest MAX_ENTRIES
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    try:
        tmp = _path().with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_path())
    except Exception as exc:
        log.warning("history write failed: %s", exc)


def clear() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


def now() -> float:
    return time.time()
