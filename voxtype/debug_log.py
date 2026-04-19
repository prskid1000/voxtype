"""Session debug log — rotated per launch.

File: {data_dir}/voxtype.log (previous run archived as voxtype.log.prev).
Same rotation pattern as telecode's main.py."""
from __future__ import annotations

import logging
import sys

from voxtype import config


def install(level: int = logging.INFO) -> None:
    """Set up logging for the session. Safe to call multiple times."""
    log_path = config.data_dir() / "voxtype.log"
    prev = log_path.with_suffix(".log.prev")
    try:
        if log_path.exists():
            if prev.exists():
                prev.unlink()
            log_path.rename(prev)
    except Exception:
        pass

    root = logging.getLogger()
    # Remove existing handlers (idempotence)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_h = logging.FileHandler(str(log_path), encoding="utf-8")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stderr)
    stream_h.setFormatter(fmt)
    root.addHandler(stream_h)
