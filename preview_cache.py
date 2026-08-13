"""Bounded preview cache and a tokenized loopback-only HTTP server."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlparse

from app_logging import PREVIEW_CACHE_DIR


PREVIEW_CACHE_SCHEME = "preview-cache"
PREVIEW_MAX_BYTES = 128 * 1024 * 1024
PREVIEW_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def cache_preview(data: bytes, suffix: str = ".jpg") -> str:
    PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    clean_suffix = suffix.lower() if suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"} else ".jpg"
    name = f"{hashlib.sha256(data).hexdigest()}{clean_suffix}"
    target = PREVIEW_CACHE_DIR / name
    if not target.exists():
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{name}.", suffix=".tmp", dir=PREVIEW_CACHE_DIR, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(data)
            os.replace(temporary, target)
            temporary = ""
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    os.utime(target, None)
    return f"{PREVIEW_CACHE_SCHEME}://{name}"


def prune_preview_cache() -> None:
    PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    files = [path for path in PREVIEW_CACHE_DIR.iterdir() if path.is_file() and not path.name.startswith(".")]
    for path in files:
        try:
            if now - path.stat().st_mtime > PREVIEW_MAX_AGE_SECONDS:
                path.unlink()
        except OSError:
            continue
    files = sorted(
        (path for path in PREVIEW_CACHE_DIR.iterdir() if path.is_file() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
            total += size
            if total > PREVIEW_MAX_BYTES:
                path.unlink()
        except OSError:
            continue


class PreviewServer:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        cache_root = PREVIEW_CACHE_DIR.resolve()
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
                parsed = urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 2 or parts[0] != token:
                    self.send_error(404)
                    return
                name = unquote(parts[1])
                if Path(name).name != name:
                    self.send_error(404)
                    return
                target = (cache_root / name).resolve()
                if target.parent != cache_root or not target.is_file():
                    self.send_error(404)
                    return
                try:
                    data = target.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="preview-cache")

    def start(self) -> None:
        prune_preview_cache()
        self._thread.start()

    def resolve(self, value: str) -> str:
        prefix = f"{PREVIEW_CACHE_SCHEME}://"
        if not value.startswith(prefix):
            return value
        name = value[len(prefix):]
        if Path(name).name != name:
            return ""
        host, port = self._server.server_address
        return f"http://{host}:{port}/{self.token}/{quote(name)}"

    def close(self) -> None:
        if not self._thread.is_alive():
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
