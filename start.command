#!/bin/zsh
ROOT="$(cd "$(dirname "$0")" && pwd)"

for APP in "$ROOT/dist.nosync/视频资源整理.app" "$ROOT/视频资源整理.app"; do
  if [[ -d "$APP" ]]; then
    open "$APP"
    exit 0
  fi
done

cd "$ROOT"
exec python3 desktop.py
