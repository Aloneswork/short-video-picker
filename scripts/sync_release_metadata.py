#!/usr/bin/env python3
"""Render or verify release-facing version strings from version.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
VERSION = str(CONFIG["version"])
BUILD = str(CONFIG["build"])


def rendered(path: Path, text: str) -> str:
    text = re.sub(r"v\d+\.\d+\.\d+（Build \d+）", f"v{VERSION}（Build {BUILD}）", text)
    text = re.sub(r"视频资源整理-v\d+\.\d+\.\d+-macOS-arm64\.zip", f"视频资源整理-v{VERSION}-macOS-arm64.zip", text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only verify; do not modify files")
    args = parser.parse_args()
    stale: list[str] = []
    for name in ("README.md", "RELEASE_NOTES.md"):
        path = ROOT / name
        before = path.read_text(encoding="utf-8")
        after = rendered(path, before)
        if before == after:
            continue
        if args.check:
            stale.append(name)
        else:
            path.write_text(after, encoding="utf-8")
    if stale:
        print(f"发行元数据未同步：{', '.join(stale)}", file=sys.stderr)
        return 1
    print(f"发行元数据已同步：v{VERSION}（Build {BUILD}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
