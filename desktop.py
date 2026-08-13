"""Native WebView desktop shell for Short Video Picker.

The interface is local HTML/CSS rendered by macOS WebKit. Parsing and downloads
remain in Python. Pywebview and the preview cache use loopback-only HTTP servers
with no externally reachable listener.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
from dataclasses import fields
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from app_config import APP_BUILD, APP_VERSION, HISTORY_LIMIT, MAX_BATCH_LINKS, resource_dir
from app_logging import configure_logging, export_diagnostic_bundle, runtime_diagnostics
from preview_cache import PREVIEW_CACHE_SCHEME, PreviewServer

BASE_DIR = resource_dir()
VENDOR_DIR = BASE_DIR / "vendor"
# PyInstaller's macOS bundle keeps real payload files under Contents/Resources
# while Contents/Frameworks (sys._MEIPASS) exposes them through symlinks.
# WKWebView refuses file:// URLs whose target is a symlink ("Frame load
# interrupted", WebKitErrorDomain 102), so resolve to the real file before
# building the file:// URI. Symlinks elsewhere still work for plain reads.
INDEX_URL = (BASE_DIR / "index.html").resolve().as_uri()
ICON_PATH = (BASE_DIR / "assets" / "AppIcon.icns").resolve()
# Downloads are network-bound, so a small pool overlaps their latency without
# hammering the CDN. Same-name files are still serialised by a per-name lock so
# two saves for one resource cannot both decide the target is missing.
DOWNLOAD_WORKERS = 4
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

import webview

from core import PARSE_ERROR_CATALOG, MediaItem, download_item_with_details, extract_links


LOGGER = configure_logging("desktop")
MEDIA_ITEM_FIELDS = {field.name for field in fields(MediaItem)}
ALLOWED_MEDIA_KINDS = {"video", "image"}
ALLOWED_URL_SCHEMES = {"http", "https"}


def _apply_dock_badge(label: str, only_when_inactive: bool = False) -> None:
    """Update the native Dock badge on AppKit's main thread."""
    try:
        from AppKit import NSApp

        app = NSApp()
        if app is None or (only_when_inactive and app.isActive()):
            return
        app.dockTile().setBadgeLabel_(label or None)
    except Exception:
        # The API is also imported by tests and non-macOS tooling.
        return


def _schedule_dock_badge(label: str, only_when_inactive: bool = False) -> None:
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_apply_dock_badge, label, only_when_inactive)
    except Exception:
        return


def _application_path() -> str:
    """Return the actual .app bundle, or the source directory in dev mode."""
    for candidate in (BASE_DIR, *BASE_DIR.parents):
        if candidate.suffix.lower() == ".app":
            return str(candidate)
    return str(BASE_DIR)


class PickerApi:
    def __init__(
        self,
        history_path: str | Path | None = None,
        preview_server: PreviewServer | None = None,
    ) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._parse_jobs: dict[str, dict[str, Any]] = {}
        self._parse_processes: dict[str, subprocess.Popen[str]] = {}
        self._jobs_lock = threading.Lock()
        # A per-filename lock so a bulk save and a card save for the *same*
        # resource cannot race and make a duplicate before either one sees the
        # target file. Downloads of different files run concurrently.
        self._name_locks: dict[str, threading.Lock] = {}
        self._name_locks_guard = threading.Lock()
        self._result_presence = False
        self._preview_server = preview_server
        self._history_path = (
            Path(history_path).expanduser()
            if history_path is not None
            else Path.home() / "Library" / "Application Support" / "视频资源整理" / "parse_history.sqlite3"
        )

    def start_parse(self, text: str) -> dict[str, Any]:
        links = extract_links(text)
        if not links:
            return self._parse_api_error("INPUT-NO-LINK")
        truncated = len(links) > MAX_BATCH_LINKS
        links = links[:MAX_BATCH_LINKS]
        job_id = uuid4().hex
        job: dict[str, Any] = {
            "ok": True,
            "job_id": job_id,
            "total": len(links),
            "completed": 0,
            "current": "正在准备解析…",
            "done": False,
            "cancelled": False,
            "cancel_requested": False,
            "truncated": truncated,
            "results": [],
            "message": "",
            "error_code": "",
        }
        with self._jobs_lock:
            if any(not existing.get("done") for existing in self._parse_jobs.values()):
                return self._parse_api_error("PARSE-BUSY")
            self._parse_jobs[job_id] = job
        _schedule_dock_badge("")
        threading.Thread(
            target=self._run_parse_job,
            args=(job_id, links),
            daemon=True,
            name=f"parse-{job_id[:8]}",
        ).start()
        return {"ok": True, "job_id": job_id, "total": len(links), "truncated": truncated}

    def parse_status(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._parse_jobs.get(job_id)
            if not job:
                return {"ok": False, "message": "解析任务已失效。", "done": True, "results": []}
            snapshot = dict(job)
            snapshot["results"] = self._resolve_preview_urls(list(job.get("results") or []))
            return snapshot

    def cancel_parse(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._parse_jobs.get(job_id)
            if not job:
                return {"ok": False, "message": "解析任务已失效。"}
            if job.get("done"):
                return {"ok": True, "message": "解析任务已经结束。"}
            job["cancel_requested"] = True
            job["current"] = "正在终止解析…"
            process = self._parse_processes.get(job_id)
        if process is not None:
            self._signal_parse_process(process, signal.SIGTERM)
        return {"ok": True, "message": "已请求终止解析。"}

    def clear_notification(self) -> dict[str, Any]:
        _schedule_dock_badge("")
        return {"ok": True}

    def set_result_presence(self, has_results: bool) -> dict[str, Any]:
        """Mirror the current front-end workspace state for native close protection."""
        with self._jobs_lock:
            self._result_presence = bool(has_results)
        return {"ok": True, "has_results": bool(has_results)}

    def has_results(self) -> bool:
        with self._jobs_lock:
            return self._result_presence

    def get_parse_history(self, query: str = "", result_filter: str = "all") -> dict[str, Any]:
        """Return the newest local parse records, optionally filtered."""
        normalized_filter = result_filter if result_filter in {"all", "success", "failed"} else "all"
        clauses: list[str] = []
        parameters: list[str] = []
        if normalized_filter == "success":
            clauses.append("result_status = 'success'")
        elif normalized_filter == "failed":
            clauses.append("result_status IN ('failed', 'cancelled')")
        normalized_query = str(query or "").strip()
        if normalized_query:
            clauses.append("(title LIKE ? OR author LIKE ? OR input_url LIKE ? OR source_url LIKE ?)")
            pattern = f"%{normalized_query}%"
            parameters.extend([pattern, pattern, pattern, pattern])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            connection = self._history_connection()
            try:
                rows = connection.execute(
                    "SELECT id, parsed_at, input_url, source_url, title, author, result_status, "
                    "error_code, item_count, content_type, content_label "
                    f"FROM parse_history{where} ORDER BY id DESC LIMIT ?",
                    [*parameters, HISTORY_LIMIT],
                ).fetchall()
                total = int(connection.execute("SELECT COUNT(*) FROM parse_history").fetchone()[0])
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            return {"ok": False, "message": f"无法读取解析记录：{exc}", "records": [], "total": 0}
        return {
            "ok": True,
            "records": [dict(row) for row in rows],
            "total": total,
            "filter": normalized_filter,
        }

    def export_parse_history(self, file_format: str = "csv") -> dict[str, Any]:
        normalized = str(file_format or "csv").lower()
        if normalized not in {"csv", "txt"}:
            return {"ok": False, "message": "只支持导出 CSV 或 TXT。"}
        try:
            connection = self._history_connection()
            try:
                rows = connection.execute(
                    "SELECT parsed_at, input_url, source_url, title, author, result_status, "
                    "error_code, item_count, content_type, content_label "
                    "FROM parse_history ORDER BY id DESC"
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            LOGGER.exception("history_export_read_failed")
            return {"ok": False, "message": f"无法读取解析记录：{exc}"}
        suffix = ".csv" if normalized == "csv" else ".txt"
        default_name = f"视频资源整理-解析记录-{datetime.now():%Y%m%d-%H%M%S}{suffix}"
        paths = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(Path.home() / "Downloads"),
            save_filename=default_name,
            file_types=("CSV 文件 (*.csv)",) if normalized == "csv" else ("文本文件 (*.txt)",),
        )
        if not paths:
            return {"ok": False, "cancelled": True, "message": "已取消导出。"}
        target = Path(paths[0]).expanduser()
        if target.suffix.lower() != suffix:
            target = target.with_suffix(suffix)
        headers = [
            "解析时间", "输入链接", "来源链接", "标题", "作者", "结果", "错误码", "资源数", "内容类型", "内容标签"
        ]
        values = [tuple(row) for row in rows]
        try:
            if normalized == "csv":
                with target.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(headers)
                    writer.writerows(values)
            else:
                with target.open("w", encoding="utf-8") as handle:
                    handle.write("\t".join(headers) + "\n")
                    for row in values:
                        handle.write("\t".join(str(value or "") for value in row) + "\n")
        except OSError as exc:
            LOGGER.exception("history_export_write_failed path=%s", target)
            return {"ok": False, "message": f"无法写入导出文件：{exc}"}
        LOGGER.info("history_exported format=%s rows=%s", normalized, len(values))
        return {"ok": True, "path": str(target), "count": len(values), "message": f"已导出 {len(values)} 条记录。"}

    def get_runtime_diagnostics(self) -> dict[str, object]:
        return runtime_diagnostics()

    def export_diagnostics(self) -> dict[str, Any]:
        paths = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=str(Path.home() / "Downloads"),
        )
        if not paths:
            return {"ok": False, "cancelled": True, "message": "已取消导出。"}
        try:
            archive = export_diagnostic_bundle(paths[0])
        except OSError as exc:
            LOGGER.exception("diagnostic_export_failed")
            return {"ok": False, "message": f"诊断包导出失败：{exc}"}
        return {"ok": True, "path": str(archive), "message": "诊断包已导出。"}

    def shutdown(self) -> None:
        """Terminate active parser groups before the desktop process exits."""
        with self._jobs_lock:
            processes = list(self._parse_processes.values())
            for job in self._parse_jobs.values():
                if not job.get("done"):
                    job["cancel_requested"] = True
        for process in processes:
            self._signal_parse_process(process, signal.SIGTERM)

    def close(self) -> None:
        self.shutdown()
        if self._preview_server is not None:
            self._preview_server.close()

    def _history_connection(self) -> sqlite3.Connection:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self._history_path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS parse_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parsed_at TEXT NOT NULL,
                input_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                result_status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '',
                item_count INTEGER NOT NULL DEFAULT 0,
                content_type TEXT NOT NULL DEFAULT '',
                content_label TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_parse_history_status ON parse_history(result_status, id DESC)"
        )
        connection.commit()
        return connection

    def _record_parse_history(
        self,
        results: list[dict[str, Any]],
        *,
        parsed_at: str | None = None,
    ) -> None:
        if not results:
            return
        timestamp = parsed_at or datetime.now().astimezone().isoformat(timespec="seconds")
        rows: list[tuple[Any, ...]] = []
        for result in results:
            items = result.get("items") if isinstance(result.get("items"), list) else []
            code = str(result.get("error_code") or "")
            if items:
                result_status = "success"
            elif code == "PARSE-CANCELLED":
                result_status = "cancelled"
            else:
                result_status = "failed"
            source_url = str(result.get("source_url") or result.get("page_url") or "")
            input_url = str(
                result.get("input_url")
                or (result.get("profile_url") if result.get("origin_type") == "profile" else "")
                or source_url
            )
            rows.append(
                (
                    timestamp,
                    input_url,
                    source_url,
                    str(result.get("title") or "")[:500],
                    str(result.get("author") or "")[:200],
                    result_status,
                    code[:120],
                    len(items),
                    str(result.get("content_type") or "")[:80],
                    str(result.get("content_label") or "")[:120],
                )
            )
        connection = self._history_connection()
        try:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO parse_history (
                        parsed_at, input_url, source_url, title, author,
                        result_status, error_code, item_count, content_type, content_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                connection.execute(
                    "DELETE FROM parse_history WHERE id NOT IN "
                    "(SELECT id FROM parse_history ORDER BY id DESC LIMIT ?)",
                    (HISTORY_LIMIT,),
                )
        finally:
            connection.close()

    def _record_parse_job_history(self, job_id: str) -> None:
        with self._jobs_lock:
            job = self._parse_jobs.get(job_id)
            if not job or not job.get("done") or job.get("history_recorded"):
                return
            job["history_recorded"] = True
            results = [dict(result) for result in (job.get("results") or [])]
        try:
            self._record_parse_history(results)
        except (OSError, sqlite3.Error) as exc:
            # Parsing must still complete even if the local history database is
            # temporarily unavailable. Expose diagnostics through parse_status.
            with self._jobs_lock:
                current = self._parse_jobs.get(job_id)
                if current is not None:
                    current["history_error"] = f"解析已完成，但记录写入失败：{exc}"

    def _run_parse_job(self, job_id: str, links: list[str]) -> None:
        process: subprocess.Popen[str] | None = None
        final_result: dict[str, Any] | None = None
        stderr = ""
        try:
            LOGGER.info("parse_job_started job=%s links=%s", job_id[:8], len(links))
            parser_command = (
                [sys.executable, "--parser-assistant", "--stream"]
                if getattr(sys, "frozen", False)
                else [sys.executable, str(BASE_DIR / "parser_assistant.py"), "--stream"]
            )
            process = subprocess.Popen(
                parser_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=BASE_DIR,
                start_new_session=True,
            )
            with self._jobs_lock:
                self._parse_processes[job_id] = process
                cancel_requested = bool(self._parse_jobs[job_id].get("cancel_requested"))
            if cancel_requested:
                self._signal_parse_process(process, signal.SIGTERM)
            elif process.stdin is not None:
                process.stdin.write(json.dumps({"links": links}, ensure_ascii=False))
                process.stdin.close()

            if process.stdout is not None:
                for raw_line in process.stdout:
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "progress":
                        with self._jobs_lock:
                            job = self._parse_jobs[job_id]
                            job["completed"] = int(event.get("completed") or 0)
                            job["total"] = int(event.get("total") or len(links))
                            job["current"] = str(event.get("current") or "正在解析…")
                            job["results"].extend(event.get("results") or [])
                    elif event.get("event") == "complete" and isinstance(event.get("result"), dict):
                        final_result = event["result"]

            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal_parse_process(process, signal.SIGKILL)
                return_code = process.wait(timeout=5)
            if process.stderr is not None:
                stderr = process.stderr.read().strip()
                if stderr:
                    LOGGER.warning("parser_stderr job=%s detail=%s", job_id[:8], stderr[-1000:])

            with self._jobs_lock:
                cancelled = bool(self._parse_jobs[job_id].get("cancel_requested"))
            if cancelled:
                self._finish_cancelled_parse(job_id, links)
            elif final_result is not None and return_code == 0:
                with self._jobs_lock:
                    truncated = bool(self._parse_jobs[job_id].get("truncated"))
                if truncated:
                    final_result["message"] = (
                        f"{final_result.get('message', '')}。单批最多 {MAX_BATCH_LINKS} 条，剩余链接未执行"
                    )
                with self._jobs_lock:
                    job = self._parse_jobs[job_id]
                    job.update(final_result)
                    job["total"] = len(links)
                    job["completed"] = len(links)
                    job["current"] = "解析完成"
                    job["done"] = True
                _schedule_dock_badge("1", only_when_inactive=True)
            else:
                code = "PARSER-CRASH" if return_code else "PARSER-BAD-OUTPUT"
                self._finish_failed_parse(job_id, links, code, stderr[-240:] if stderr else "")
                _schedule_dock_badge("1", only_when_inactive=True)
            LOGGER.info("parse_job_finished job=%s return_code=%s", job_id[:8], return_code)
        except (OSError, BrokenPipeError) as exc:
            LOGGER.exception("parse_job_failed job=%s", job_id[:8])
            with self._jobs_lock:
                cancelled = bool(self._parse_jobs[job_id].get("cancel_requested"))
            if cancelled:
                self._finish_cancelled_parse(job_id, links)
            else:
                self._finish_failed_parse(job_id, links, "PARSER-CRASH", str(exc))
                _schedule_dock_badge("1", only_when_inactive=True)
        finally:
            with self._jobs_lock:
                self._parse_processes.pop(job_id, None)
            self._record_parse_job_history(job_id)

    def _finish_failed_parse(
        self,
        job_id: str,
        links: list[str],
        code: str,
        detail: str = "",
    ) -> None:
        message, hint, retryable = PARSE_ERROR_CATALOG.get(code, PARSE_ERROR_CATALOG["PARSE-UNEXPECTED"])
        with self._jobs_lock:
            job = self._parse_jobs[job_id]
            completed = min(int(job.get("completed") or 0), len(links))
            results = list(job.get("results") or [])
            pending = links[completed:]
            if not results and not pending:
                pending = links
            results.extend(
                {
                    "source_url": link,
                    "input_url": link,
                    "page_url": link,
                    "title": "分享链接",
                    "author": "",
                    "status": "error",
                    "error": message,
                    "error_code": code,
                    "error_hint": hint,
                    "retryable": retryable,
                    "content_type": "",
                    "content_label": "",
                    "debug": [f"解析助手异常结束：{detail}"] if detail else [],
                    "items": [],
                }
                for link in pending
            )
            api_message = f"{message} {hint}"
            if detail:
                api_message += f"（{detail}）"
            job.update(
                ok=any(entry.get("items") for entry in results),
                message=api_message,
                error_code=code,
                results=results,
                current="解析异常结束",
                done=True,
            )

    def _finish_cancelled_parse(self, job_id: str, links: list[str]) -> None:
        message, hint, retryable = PARSE_ERROR_CATALOG["PARSE-CANCELLED"]
        with self._jobs_lock:
            job = self._parse_jobs[job_id]
            completed = min(int(job.get("completed") or 0), len(links))
            pending = links[completed:]
            job["results"].extend(
                {
                    "source_url": link,
                    "input_url": link,
                    "page_url": link,
                    "title": "分享链接",
                    "author": "",
                    "status": "cancelled",
                    "error": message,
                    "error_code": "PARSE-CANCELLED",
                    "error_hint": hint,
                    "retryable": retryable,
                    "content_type": "",
                    "content_label": "",
                    "debug": ["用户主动终止了本次批量解析。"],
                    "items": [],
                }
                for link in pending
            )
            job.update(
                ok=any(entry.get("items") for entry in job["results"]),
                message=f"已终止解析：完成 {completed}/{len(links)} 条，其余 {len(pending)} 条未执行。",
                error_code="PARSE-CANCELLED",
                current="已终止解析",
                done=True,
                cancelled=True,
            )

    @staticmethod
    def _signal_parse_process(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass

    @staticmethod
    def _parse_api_error(code: str) -> dict[str, Any]:
        message, hint, _retryable = PARSE_ERROR_CATALOG[code]
        return {"ok": False, "message": f"{message} {hint}", "error_code": code, "results": []}

    def clipboard_text(self) -> dict[str, Any]:
        """Read the macOS clipboard outside WebKit's local-file restrictions."""
        try:
            completed = subprocess.run(
                ["/usr/bin/pbpaste"],
                text=True,
                capture_output=True,
                timeout=3,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "text": "", "message": f"无法读取剪贴板：{exc}"}
        if completed.returncode != 0:
            return {"ok": False, "text": "", "message": "无法读取剪贴板，请手动粘贴。"}
        return {"ok": True, "text": completed.stdout}

    def choose_folder(self) -> str:
        paths = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        return paths[0] if paths else ""

    def start_download(self, values: list[dict[str, Any]], folder: str) -> dict[str, Any]:
        if not values:
            return {"ok": False, "message": "请先选择至少一个资源。"}
        if not folder:
            return {"ok": False, "message": "请先选择保存位置。"}
        try:
            safe_values = [self._validated_media_value(value) for value in values]
            safe_folder = self._validated_folder(folder)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return self._start_job(safe_values, safe_folder)

    def start_download_one(self, value: dict[str, Any], folder: str) -> dict[str, Any]:
        if not folder:
            return {"ok": False, "message": "请先选择保存位置。"}
        return self.start_download([value], folder)

    def retry_download(self, job_id: str, folder: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            failed_values = [dict(value) for value in (job.get("failed_values") or [])] if job else []
        if not failed_values:
            return {"ok": False, "message": "没有可重试的失败项目。"}
        return self.start_download(failed_values, folder)

    def download_status(self, job_id: str) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else {"ok": False, "message": "保存任务已失效。", "done": True}

    def _start_job(self, values: list[dict[str, Any]], folder: str) -> dict[str, Any]:
        job_id = uuid4().hex
        job: dict[str, Any] = {
            "ok": True,
            "job_id": job_id,
            "total": len(values),
            "completed": 0,
            "saved": 0,
            "existing": 0,
            "failed": 0,
            "renamed": 0,
            "current": "正在准备保存…",
            "done": False,
            "results": [],
            "failed_values": [],
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run_download_job, args=(job_id, values, folder), daemon=True)
        thread.start()
        return {"ok": True, "job_id": job_id}

    def _run_download_job(self, job_id: str, values: list[dict[str, Any]], folder: str) -> None:
        workers = min(DOWNLOAD_WORKERS, len(values)) or 1
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download") as pool:
            for _ in pool.map(lambda value: self._download_one(job_id, value, folder), values):
                pass
        self._update_job(job_id, done=True, current="保存完成")

    def _download_one(self, job_id: str, value: dict[str, Any], folder: str) -> None:
        name = str(value.get("suggested_name") or "资源")
        media_url = str(value.get("media_url") or "")
        self._update_job(job_id, current=name)
        try:
            item = MediaItem(**value)
            with self._name_lock(folder, name):
                details = download_item_with_details(item, folder)
            outcome = str(details["status"])
            result = {"name": name, "media_url": media_url, **details}
        except Exception as exc:  # noqa: BLE001 - preserve per-item failure detail.
            LOGGER.exception("download_failed name=%s url=%s", name, media_url)
            outcome = "failed"
            result = {"name": name, "media_url": media_url, "status": "failed", "error": str(exc)}
        with self._jobs_lock:
            job = self._jobs[job_id]
            job[outcome] = int(job.get(outcome, 0)) + 1
            if result.get("renamed_due_to_collision"):
                job["renamed"] = int(job.get("renamed", 0)) + 1
            job["completed"] += 1
            job["results"].append(result)
            if outcome == "failed":
                job["failed_values"].append(value)

    def _name_lock(self, folder: str, name: str) -> threading.Lock:
        key = os.path.join(folder, name)
        with self._name_locks_guard:
            lock = self._name_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._name_locks[key] = lock
            return lock

    def _update_job(self, job_id: str, **changes: Any) -> None:
        with self._jobs_lock:
            self._jobs[job_id].update(changes)

    def get_initial_state(self) -> dict[str, Any]:
        diagnostics = runtime_diagnostics()
        return {
            "folder": os.path.expanduser("~/Downloads/ShortVideoPicker"),
            "version": APP_VERSION,
            "build": APP_BUILD,
            "app_path": _application_path(),
            "max_batch_links": MAX_BATCH_LINKS,
            "history_limit": HISTORY_LIMIT,
            "runtime": diagnostics,
        }

    def _resolve_preview_urls(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for result in results:
            copied = dict(result)
            items = []
            for value in result.get("items") or []:
                item = dict(value)
                preview = str(item.get("preview_url") or "")
                if preview.startswith(f"{PREVIEW_CACHE_SCHEME}://"):
                    item["preview_url"] = self._preview_server.resolve(preview) if self._preview_server else ""
                items.append(item)
            copied["items"] = items
            output.append(copied)
        return output

    @staticmethod
    def _validated_folder(folder: str) -> str:
        target = Path(str(folder or "")).expanduser()
        if not target.is_absolute():
            raise ValueError("保存位置必须是本机绝对路径。")
        resolved = target.resolve(strict=False)
        if resolved == Path("/"):
            raise ValueError("不能把系统根目录作为保存位置。")
        return str(resolved)

    @staticmethod
    def _validated_media_value(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("资源参数格式无效。")
        unknown = set(value) - MEDIA_ITEM_FIELDS
        if unknown:
            raise ValueError(f"资源包含不支持的字段：{', '.join(sorted(unknown))}")
        copied = {key: value[key] for key in MEDIA_ITEM_FIELDS if key in value}
        kind = str(copied.get("kind") or "")
        if kind not in ALLOWED_MEDIA_KINDS:
            raise ValueError("资源类型必须是视频或图片。")
        media_url = str(copied.get("media_url") or "")
        if urlparse(media_url).scheme.lower() not in ALLOWED_URL_SCHEMES:
            raise ValueError("资源地址必须使用 http 或 https。")
        copied["media_url"] = media_url
        copied["suggested_name"] = os.path.basename(str(copied.get("suggested_name") or ""))[:240]
        headers = copied.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("资源请求头格式无效。")
        copied["headers"] = {
            key: str(value)[:1000]
            for key, value in (headers or {}).items()
            if key in {"User-Agent", "Referer", "Accept"}
        }
        options = copied.get("quality_options")
        safe_options: list[dict[str, Any]] = []
        if isinstance(options, list):
            for option in options[:20]:
                if not isinstance(option, dict):
                    continue
                option_url = str(option.get("url") or "")
                if urlparse(option_url).scheme.lower() not in ALLOWED_URL_SCHEMES:
                    continue
                try:
                    bitrate = max(0, int(option.get("bitrate") or 0))
                except (TypeError, ValueError):
                    bitrate = 0
                safe_options.append(
                    {
                        "label": str(option.get("label") or "清晰度")[:80],
                        "url": option_url,
                        "bitrate": bitrate,
                    }
                )
        copied["quality_options"] = safe_options
        copied["quality_label"] = str(copied.get("quality_label") or "")[:80]
        return copied


def _prepare_close(window: Any, api: PickerApi) -> None:
    """Enable native confirmation only while visible parse cards would be lost."""
    window.confirm_close = api.has_results()


def main() -> None:
    preview_server = PreviewServer()
    preview_server.start()
    api = PickerApi(preview_server=preview_server)
    window = webview.create_window(
        "视频资源整理",
        url=INDEX_URL,
        js_api=api,
        width=820,
        height=600,
        min_size=(520, 300),
        background_color="#fbfbfa",
        localization={
            "global.quitConfirmation": "现在已经有解析内容，是否确认退出？",
            "global.quit": "确认退出",
            "global.cancel": "取消",
        },
    )
    window.events.closing += lambda: _prepare_close(window, api)

    def close_resources() -> None:
        api.close()

    window.events.closed += close_resources
    debug_enabled = os.environ.get("SHORT_VIDEO_PICKER_WEBVIEW_DEBUG") == "1"
    window.events.shown += lambda: LOGGER.info("webview_event shown")
    window.events.before_load += lambda: LOGGER.info("webview_event before_load")
    window.events.loaded += lambda: LOGGER.info("webview_event loaded")
    if debug_enabled:
        os.environ["PYWEBVIEW_LOG"] = "DEBUG"
    LOGGER.info(
        "desktop_start path=%s python=%s arch=%s debug=%s",
        _application_path(), sys.version.split()[0], platform.machine(), debug_enabled,
    )
    try:
        webview.start(
            gui="cocoa",
            private_mode=False,
            icon=str(ICON_PATH),
            debug=debug_enabled,
        )
    except BaseException:
        LOGGER.exception("desktop_fatal")
        raise


if __name__ == "__main__":
    main()
