#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi
SIGN_IDENTITY="${SIGN_IDENTITY:--}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
DIST="$ROOT/dist.nosync"
FINAL_APP="$DIST/视频资源整理.app"
STAGING_ROOT="$ROOT/.build-staging.nosync"
RUNTIME_VENDOR="$STAGING_ROOT/runtime-vendor"
STAGING_DIST="$STAGING_ROOT/dist"
STAGING_WORK="$STAGING_ROOT/work"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "构建已停止：未找到 Python 3.13 构建环境：$PYTHON_BIN" >&2
  exit 2
fi
PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_VERSION" != "3.13" ]]; then
  echo "构建已停止：当前依赖要求 Python 3.13，实际为 $PYTHON_VERSION。" >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import PyInstaller, PIL, webview, websocket, bottle, proxy_tools, objc, AppKit, Quartz, WebKit, Security, UniformTypeIdentifiers' >/dev/null 2>&1 && [[ ! -d "$ROOT/vendor/webview" ]]; then
  echo "构建已停止：构建环境不完整。请先运行：$PYTHON_BIN -m pip install -r requirements-macos.txt" >&2
  exit 2
fi

VERSION="$($PYTHON_BIN -c 'import json; print(json.load(open("version.json", encoding="utf-8"))["version"])')"
BUILD_NUMBER="$($PYTHON_BIN -c 'import json; print(json.load(open("version.json", encoding="utf-8"))["build"])')"
ARCHIVE_NAME="视频资源整理-v${VERSION}-macOS-arm64.zip"
ARCHIVE="$DIST/$ARCHIVE_NAME"
CHECKSUM="$DIST/SHA256SUMS.txt"

if /usr/bin/pgrep -f "$FINAL_APP/Contents/MacOS/视频资源整理" >/dev/null 2>&1; then
  echo "构建已停止：请先退出正在运行的“视频资源整理”，再重新构建。" >&2
  exit 2
fi
"$ROOT/scripts/check_source_clean.sh"
"$PYTHON_BIN" "$ROOT/scripts/sync_release_metadata.py" --check
if [[ "$STAGING_ROOT" != "$ROOT/.build-staging.nosync" || -L "$STAGING_ROOT" ]]; then
  echo "构建已停止：暂存目录校验失败。" >&2
  exit 2
fi

rm -rf -- "$STAGING_ROOT"
mkdir -p "$RUNTIME_VENDOR" "$STAGING_DIST" "$STAGING_WORK"
if [[ -d "$ROOT/vendor/webview" ]]; then
  /usr/bin/rsync -a \
    --exclude 'PIL' \
    --exclude 'pillow-*.dist-info' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'tests' \
    --exclude 'test' \
    --exclude 'bin' \
    "$ROOT/vendor/" "$RUNTIME_VENDOR/"
fi

PYTHONPATH="$RUNTIME_VENDOR" "$PYTHON_BIN" -c \
  'import PIL, webview, objc, AppKit, Quartz, WebKit, Security, UniformTypeIdentifiers, websocket, bottle, proxy_tools'

SHORT_VIDEO_PICKER_ROOT="$ROOT" \
SHORT_VIDEO_PICKER_VENDOR="$RUNTIME_VENDOR" \
PYINSTALLER_CONFIG_DIR="$STAGING_ROOT/pyinstaller-config" \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONNOUSERSITE=1 \
"$PYTHON_BIN" -m PyInstaller \
  --clean \
  --noconfirm \
  --distpath "$STAGING_DIST" \
  --workpath "$STAGING_WORK" \
  "$ROOT/short_video_picker.spec"

STAGING_APP="$STAGING_DIST/视频资源整理.app"
STAGING_EXECUTABLE="$STAGING_APP/Contents/MacOS/视频资源整理"
if [[ ! -x "$STAGING_EXECUTABLE" ]]; then
  echo "构建已停止：PyInstaller 未生成可执行应用。" >&2
  exit 3
fi

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$STAGING_APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$STAGING_APP/Contents/Info.plist"
/usr/bin/xattr -cr "$STAGING_APP"

if [[ "$SIGN_IDENTITY" == "-" ]]; then
  /usr/bin/codesign --force --deep --sign - "$STAGING_APP"
  SIGNING_DESCRIPTION="ad-hoc 临时签名"
else
  /usr/bin/codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$STAGING_APP"
  SIGNING_DESCRIPTION="Developer ID：$SIGN_IDENTITY"
fi
/usr/bin/codesign --verify --deep --strict --verbose=2 "$STAGING_APP"

PROBE_JSON="$STAGING_ROOT/runtime-probe.json"
LOOPBACK_JSON="$STAGING_ROOT/loopback-probe.json"
PARSER_INPUT="$STAGING_ROOT/parser-input.json"
PARSER_OUTPUT="$STAGING_ROOT/parser-output.json"
/usr/bin/printf '{}\n' > "$PARSER_INPUT"
"$STAGING_EXECUTABLE" --runtime-probe "$PROBE_JSON"
"$STAGING_EXECUTABLE" --loopback-probe "$LOOPBACK_JSON"
"$STAGING_EXECUTABLE" --parser-assistant < "$PARSER_INPUT" > "$PARSER_OUTPUT"
"$PYTHON_BIN" -c '
import json, pathlib, sys
runtime = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
parser = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
loopback = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
names = {entry["name"]: entry for entry in runtime["checks"]}
assert runtime["frozen"] is True
assert names["内置 Python"]["ok"] is True
assert names["Pillow"]["ok"] is True
assert names["macOS WebView"]["ok"] is True
assert parser["results"] == []
assert loopback["ok"] is True and loopback["loopback"] is True and loopback["tokenized"] is True
' "$PROBE_JSON" "$PARSER_OUTPUT" "$LOOPBACK_JSON"

if ! find "$STAGING_APP/Contents" -iname 'Python*' -print -quit | /usr/bin/grep -q .; then
  echo "构建已停止：应用包中没有检测到内置 Python 运行时。" >&2
  exit 3
fi
if find "$STAGING_APP" -type d \( -name __pycache__ -o -name tests -o -name test -o -name '*.dSYM' -o -name 'backup*' \) -print -quit | /usr/bin/grep -q .; then
  echo "构建已停止：应用包仍包含缓存、测试或调试目录。" >&2
  exit 3
fi
if find "$STAGING_APP" -type f \( -name '*.log' -o -name '.DS_Store' -o -path '*/vendor/bin/bottle.py' \) -print -quit | /usr/bin/grep -q .; then
  echo "构建已停止：应用包仍包含日志或无用命令副本。" >&2
  exit 3
fi

NOTARY_STATUS="未公证"
if [[ -n "$NOTARY_PROFILE" ]]; then
  if [[ "$SIGN_IDENTITY" == "-" ]]; then
    echo "构建已停止：Apple 公证要求 Developer ID 签名，不能使用 ad-hoc 签名。" >&2
    exit 4
  fi
  NOTARY_ARCHIVE="$STAGING_ROOT/notary-upload.zip"
  /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$STAGING_APP" "$NOTARY_ARCHIVE"
  /usr/bin/xcrun notarytool submit "$NOTARY_ARCHIVE" --keychain-profile "$NOTARY_PROFILE" --wait
  /usr/bin/xcrun stapler staple "$STAGING_APP"
  /usr/bin/xcrun stapler validate "$STAGING_APP"
  NOTARY_STATUS="已公证并装订票据"
fi

mkdir -p "$DIST"
if [[ -e "$FINAL_APP" && ! -d "$FINAL_APP" ]]; then
  echo "发布已停止：目标应用路径存在且不是目录。" >&2
  exit 3
fi
rm -rf -- "$FINAL_APP"
mv "$STAGING_APP" "$FINAL_APP"
rm -f -- "$ARCHIVE" "$CHECKSUM"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$FINAL_APP" "$ARCHIVE"
cd "$DIST"
/usr/bin/shasum -a 256 "$ARCHIVE_NAME" > "$CHECKSUM"

if [[ "$STAGING_ROOT" != "$ROOT/.build-staging.nosync" || -L "$STAGING_ROOT" ]]; then
  echo "发布已停止：构建暂存目录最终清理校验失败。" >&2
  exit 3
fi
rm -rf -- "$STAGING_ROOT"

echo "应用：$FINAL_APP"
echo "压缩包：$ARCHIVE"
echo "校验值：$CHECKSUM"
echo "签名：$SIGNING_DESCRIPTION"
echo "公证：$NOTARY_STATUS"
