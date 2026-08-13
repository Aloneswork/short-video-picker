# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the self-contained macOS application.

Dependencies may come from a clean virtual environment or an optional local
``vendor`` mirror. The mirror is deliberately not required by the repository.
"""

import json
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path(os.environ["SHORT_VIDEO_PICKER_ROOT"]).resolve()
RUNTIME_VENDOR = Path(os.environ["SHORT_VIDEO_PICKER_VENDOR"]).resolve()
RELEASE_LICENSES = Path(os.environ["SHORT_VIDEO_PICKER_LICENSES"]).resolve()
if RUNTIME_VENDOR.is_dir():
    sys.path.insert(0, str(RUNTIME_VENDOR))

VERSION = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
DATAS = [
    (str(ROOT / "index.html"), "."),
    (str(ROOT / "version.json"), "."),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(RELEASE_LICENSES), "THIRD_PARTY_LICENSES"),
    (str(ROOT / "assets" / "AppIcon.icns"), "assets"),
    (str(ROOT / "assets" / "app-icon-macos.png"), "assets"),
]
DATAS += collect_data_files("webview", subdir="js")

HIDDEN_IMPORTS = [
    "AppKit",
    "CoreFoundation",
    "Foundation",
    "JavaScriptCore",
    "objc",
    "PyObjCTools.AppHelper",
    "Quartz",
    "Quartz.CoreGraphics",
    "Quartz.ImageIO",
    "Security",
    "UniformTypeIdentifiers",
    "WebKit",
    "bottle",
    "proxy_tools",
    "typing_extensions",
    "websocket",
    "webview.platforms.cocoa",
    "PIL",
    "PIL.Image",
    "PIL.AvifImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin",
    "PIL.WebPImagePlugin",
]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)] + ([str(RUNTIME_VENDOR)] if RUNTIME_VENDOR.is_dir() else []),
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=(
        [str(RUNTIME_VENDOR / "webview" / "__pyinstaller")]
        if (RUNTIME_VENDOR / "webview" / "__pyinstaller").is_dir()
        else []
    ),
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "OpenSSL",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "PIL.ImageQt",
        "PIL.ImageTk",
        "attrs",
        "bcrypt",
        "cefpython3",
        "cffi",
        "cryptography",
        "gi",
        "importlib_metadata",
        "jinja2",
        "kivy",
        "mako",
        "markupsafe",
        "numpy",
        "psutil",
        "pygments",
        "qtpy",
        "readline",
        "service_identity",
        "setuptools",
        "tkinter",
        "tornado",
        "twisted",
        "ujson",
        "uvloop",
        "yaml",
        "zope",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="视频资源整理",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="视频资源整理",
)
app = BUNDLE(
    coll,
    name="视频资源整理.app",
    icon=str(ROOT / "assets" / "AppIcon.icns"),
    bundle_identifier="com.longxinglin.shortvideopicker",
    version=str(VERSION["version"]),
    info_plist={
        "CFBundleDisplayName": "视频资源整理",
        "CFBundleName": "视频资源整理",
        "CFBundleShortVersionString": str(VERSION["version"]),
        "CFBundleVersion": str(VERSION["build"]),
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
