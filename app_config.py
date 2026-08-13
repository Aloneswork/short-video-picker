"""Single source for release metadata and shared product limits."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def resource_dir() -> Path:
    """Return source resources or PyInstaller's immutable bundle directory."""
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        candidates = [
            Path(frozen_root),
            Path(sys.executable).resolve().parent.parent / "Resources",
        ]
        for candidate in candidates:
            if (candidate / "version.json").is_file():
                return candidate
    return Path(__file__).resolve().parent


with (resource_dir() / "version.json").open(encoding="utf-8") as _handle:
    _CONFIG = json.load(_handle)


APP_VERSION = str(_CONFIG["version"])
APP_BUILD = str(_CONFIG["build"])
MAX_BATCH_LINKS = int(_CONFIG["max_batch_links"])
HISTORY_LIMIT = int(_CONFIG["history_limit"])
