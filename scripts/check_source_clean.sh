#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SOURCE_FIND=(
  find "$ROOT"
  -path "$ROOT/.git" -prune -o
  -path "$ROOT/.venv" -prune -o
  -path "$ROOT/venv" -prune -o
  -path "$ROOT/vendor" -prune -o
  -path "$ROOT/dist.nosync" -prune -o
  -path "$ROOT/.build-staging.nosync" -prune -o
)

if "${SOURCE_FIND[@]}" -type d \( -name __pycache__ -o -name .pytest_cache \) -print -quit | grep -q .; then
  echo "源码目录包含 Python/pytest 缓存；请清理后再发布。" >&2
  exit 1
fi
if "${SOURCE_FIND[@]}" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) -print -quit | grep -q .; then
  echo "源码目录包含缓存或 Finder 残留；请清理后再发布。" >&2
  exit 1
fi

echo "源码洁净度检查通过。"
