#!/usr/bin/env python3
"""Collect license texts from the exact interpreter and package environment."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_GROUPS = {
    "PyInstaller": ["PyInstaller"],
    "Pillow": ["Pillow"],
    "pywebview": ["pywebview"],
    "websocket-client": ["websocket-client"],
    "Bottle": ["bottle"],
    "typing-extensions": ["typing-extensions"],
    "PyObjC": [
        "pyobjc-core",
        "pyobjc-framework-Cocoa",
        "pyobjc-framework-Quartz",
        "pyobjc-framework-Security",
        "pyobjc-framework-WebKit",
        "pyobjc-framework-UniformTypeIdentifiers",
    ],
}
HOMEBREW_RUNTIME_LICENSES = {
    "mpdecimal": ["COPYRIGHT.txt"],
    "openssl@3": ["LICENSE.txt"],
    "xz": ["COPYING", "COPYING.0BSD", "COPYING.LGPLv2.1", "COPYING.GPLv2", "COPYING.GPLv3"],
}


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)


def license_files(distribution: metadata.Distribution) -> list[Path]:
    found: list[Path] = []
    for relative in distribution.files or []:
        name = Path(str(relative)).name.lower()
        if not name.startswith(("license", "copying", "notice", "copyright")):
            continue
        candidate = Path(distribution.locate_file(relative))
        if candidate.is_file() and ".dsym" not in str(candidate).lower():
            found.append(candidate)
    return sorted(found)


def python_license() -> Path:
    roots = [Path(sys.base_prefix).resolve(), Path(sys.base_prefix)]
    for root in roots:
        for directory in [root, *list(root.parents)[:6]]:
            for name in ("LICENSE", "LICENSE.txt"):
                candidate = directory / name
                if candidate.is_file():
                    return candidate
    raise RuntimeError(f"未找到 Python 许可证：{sys.base_prefix}")


def copy_unique(sources: list[Path], output: Path, prefix: str) -> int:
    seen: set[str] = set()
    count = 0
    for source in sources:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        suffix = source.suffix or ".txt"
        name = f"{safe_name(prefix)}-LICENSE{'' if count == 0 else f'-{count + 1}'}{suffix}"
        shutil.copyfile(source, output / name)
        count += 1
    return count


def collect(output: Path) -> None:
    base = Path(sys.base_prefix).resolve()
    if (base / "conda-meta").is_dir() or any(marker in str(base).lower() for marker in ("anaconda", "miniconda", "mambaforge")):
        raise RuntimeError(f"公开发行构建禁止使用 Conda/Anaconda 运行时：{base}")

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    copy_unique([python_license()], output, f"Python-{sys.version_info.major}.{sys.version_info.minor}")
    for group, package_names in PACKAGE_GROUPS.items():
        sources: list[Path] = []
        versions: list[str] = []
        for package_name in package_names:
            distribution = metadata.distribution(package_name)
            versions.append(distribution.version)
            sources.extend(license_files(distribution))
        if not sources:
            raise RuntimeError(f"未找到 {group} 的许可原文")
        copy_unique(sources, output, f"{group}-{versions[0]}")

    proxy_license = ROOT / "licenses" / "proxy-tools-LICENSE.txt"
    if not proxy_license.is_file():
        raise RuntimeError("缺少 proxy-tools 上游 BSD 许可证原文")
    proxy_version = metadata.version("proxy-tools")
    copy_unique([proxy_license], output, f"proxy-tools-{proxy_version}")

    # Homebrew CPython links these libraries into the frozen runtime. Preserve
    # their upstream notices whenever the build interpreter comes from brew.
    if str(base).startswith("/opt/homebrew/"):
        for formula, names in HOMEBREW_RUNTIME_LICENSES.items():
            root = Path("/opt/homebrew/opt") / formula
            sources = [root / name for name in names if (root / name).is_file()]
            if not sources:
                raise RuntimeError(f"未找到 Homebrew 运行时依赖 {formula} 的许可原文")
            copy_unique(sources, output, safe_name(formula))

    index = output / "README.txt"
    entries = sorted(path.name for path in output.iterdir() if path.name != index.name)
    index.write_text(
        "Third-party license texts collected from the exact release build environment.\n\n"
        + "\n".join(f"- {entry}" for entry in entries)
        + "\n",
        encoding="utf-8",
    )
    print(f"已收集 {len(entries)} 份发行许可文件：{output}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collect(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
