"""Entrypoint for the desktop app and its frozen helper modes."""

import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen


def _restore_standard_streams() -> None:
    """Restore pipe-backed streams for a PyInstaller windowed child process."""
    for name, descriptor, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        if getattr(sys, name) is not None:
            continue
        try:
            stream = os.fdopen(os.dup(descriptor), mode, encoding="utf-8", buffering=1)
        except OSError:
            continue
        setattr(sys, name, stream)


def _runtime_probe(destination: str = "") -> int:
    from app_logging import runtime_diagnostics

    payload = json.dumps(runtime_diagnostics(), ensure_ascii=False, indent=2)
    if destination:
        Path(destination).write_text(payload + "\n", encoding="utf-8")
    else:
        _restore_standard_streams()
        if sys.stdout is None:
            return 2
        print(payload, flush=True)
    return 0


def _loopback_probe(destination: str) -> int:
    """Exercise the packaged preview cache over a real tokenized loopback URL."""
    from preview_cache import PreviewServer, cache_preview

    server = PreviewServer()
    try:
        server.start()
        reference = cache_preview(b"short-video-picker-loopback-probe", ".jpg")
        resolved = server.resolve(reference)
        with urlopen(resolved, timeout=4) as response:
            payload = response.read()
        result = {
            "ok": payload == b"short-video-picker-loopback-probe",
            "loopback": resolved.startswith("http://127.0.0.1:"),
            "tokenized": server.token in resolved,
            "content_length": len(payload),
        }
    finally:
        server.close()
    Path(destination).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all((result["ok"], result["loopback"], result["tokenized"])) else 1


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "--runtime-probe" in arguments:
        position = arguments.index("--runtime-probe")
        destination = arguments[position + 1] if position + 1 < len(arguments) else ""
        raise SystemExit(_runtime_probe(destination))
    if "--loopback-probe" in arguments:
        position = arguments.index("--loopback-probe")
        if position + 1 >= len(arguments):
            raise SystemExit(2)
        raise SystemExit(_loopback_probe(arguments[position + 1]))
    if "--parser-assistant" in arguments:
        _restore_standard_streams()
        from parser_assistant import main
    else:
        # A windowed PyInstaller process starts with None streams. Restore them
        # (or /dev/null) so third-party loggers and native bridges never have to
        # write through a None stream; harmless when launched from a terminal.
        _restore_standard_streams()
        from desktop import main

    main()
