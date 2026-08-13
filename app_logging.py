"""Rotating, privacy-conscious application logging and diagnostics export."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
import zipfile

from app_config import APP_BUILD, APP_VERSION, resource_dir


APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "视频资源整理"
LOG_DIR = Path.home() / "Library" / "Logs" / "视频资源整理"
LOG_PATH = LOG_DIR / "app.log"
ACTIVE_LOG_PATH = LOG_PATH
CACHE_DIR = Path.home() / "Library" / "Caches" / "视频资源整理"
PREVIEW_CACHE_DIR = CACHE_DIR / "previews"

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def redact_text(value: object) -> str:
    """Remove signed queries and unrelated URL path details from log messages."""
    text = str(value)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        parsed = urlparse(raw)
        item = re.search(r"/(?:video|note|slides)/(\d{10,24})", parsed.path)
        path = f"/work/{item.group(1)}" if item else "/…"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    return _URL_RE.sub(replace, text)


class _PrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_logging(component: str = "desktop") -> logging.Logger:
    global ACTIVE_LOG_PATH
    logger = logging.getLogger("short_video_picker")
    if not logger.handlers:
        log_path = LOG_PATH
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        except OSError:
            fallback_dir = Path(tempfile.gettempdir()) / "视频资源整理-logs"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            log_path = fallback_dir / "app.log"
            handler = RotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        ACTIVE_LOG_PATH = log_path
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(process)d %(name)s %(message)s")
        )
        handler.addFilter(_PrivacyFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    child = logger.getChild(component)
    child.info("component_start version=%s build=%s", APP_VERSION, APP_BUILD)
    return child


def _command_version(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return ""
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0][:200] if output else ""


def _bundled_ffmpeg_path() -> str:
    roots = [resource_dir()]
    if getattr(sys, "_MEIPASS", ""):
        roots.append(Path(sys._MEIPASS))
    roots.append(Path(sys.executable).resolve().parent)
    for root in roots:
        for candidate in (root / "bin" / "ffmpeg", root / "ffmpeg"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return ""


def runtime_diagnostics() -> dict[str, object]:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, level: str, detail: str, guidance: str = "") -> None:
        checks.append(
            {"name": name, "ok": bool(ok), "level": level, "detail": detail, "guidance": guidance}
        )

    frozen = bool(getattr(sys, "frozen", False))
    add(
        "内置 Python",
        frozen,
        "error" if not frozen else "ok",
        f"Python {platform.python_version()} · {platform.machine()} · {'应用内置' if frozen else '开发环境'}",
        "正式分发请使用自包含构建包。" if not frozen else "",
    )
    try:
        from PIL import Image

        add("Pillow", True, "ok", f"{getattr(Image, '__version__', '可用')}")
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary.
        add("Pillow", False, "error", type(exc).__name__, "图片预览与 JPG 转换不可用，请重新安装应用。")

    try:
        import AppKit  # noqa: F401
        import webview  # noqa: F401

        add("macOS WebView", True, "ok", "PyObjC/pywebview 可用")
    except Exception as exc:  # noqa: BLE001
        add("macOS WebView", False, "error", type(exc).__name__, "桌面界面运行时损坏，请重新安装应用。")

    edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    edge_ok = os.path.isfile(edge) and os.access(edge, os.X_OK)
    add(
        "Microsoft Edge",
        edge_ok,
        "warning" if not edge_ok else "ok",
        _command_version([edge, "--version"]) if edge_ok else "未安装",
        "复杂作品只能使用公开分享页兜底；安装 Edge 后可提高解析覆盖率。" if not edge_ok else "",
    )

    ffmpeg = _bundled_ffmpeg_path() or shutil.which("ffmpeg") or ""
    if not ffmpeg:
        ffmpeg = next(
            (
                candidate
                for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK)
            ),
            "",
        )
    ffmpeg_ok = bool(ffmpeg and os.path.isfile(ffmpeg))
    add(
        "ffmpeg",
        ffmpeg_ok,
        "warning" if not ffmpeg_ok else "ok",
        _command_version([ffmpeg, "-version"]) if ffmpeg_ok else "未找到",
        "无公开封面的视频不会生成首帧预览；可安装 ffmpeg 后重启应用。" if not ffmpeg_ok else "",
    )

    errors = [entry for entry in checks if not entry["ok"] and entry["level"] == "error"]
    warnings = [entry for entry in checks if not entry["ok"] and entry["level"] == "warning"]
    return {
        "ok": not errors,
        "version": APP_VERSION,
        "build": APP_BUILD,
        "python": sys.version.split()[0],
        "architecture": platform.machine(),
        "frozen": frozen,
        "executable": str(Path(sys.executable).resolve()),
        "checks": checks,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "log_path": str(ACTIVE_LOG_PATH),
        "preview_cache_path": str(PREVIEW_CACHE_DIR),
    }


def export_diagnostic_bundle(destination_dir: str | Path) -> Path:
    destination = Path(destination_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = destination / f"视频资源整理-诊断-{timestamp}.zip"
    report = json.dumps(runtime_diagnostics(), ensure_ascii=False, indent=2)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("runtime.json", report)
        bundle.writestr(
            "README.txt",
            "诊断包不包含 Cookie、浏览器配置或媒体文件。日志中的 URL 查询参数已脱敏。\n",
        )
        log_candidates = [ACTIVE_LOG_PATH]
        log_candidates.extend(sorted(ACTIVE_LOG_PATH.parent.glob("app.log.*")))
        for index, path in enumerate(log_candidates):
            if path.is_file():
                bundle.writestr(f"logs/app-{index}.log", redact_text(path.read_text(errors="replace")))
    return archive
