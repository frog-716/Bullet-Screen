#!/usr/bin/env python3
"""Read-only local environment diagnostics for Bullet-Screen."""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


MIN_PYTHON = (3, 9)
REQUIRED_ENTRIES = (
    "bilibili/server.py",
    "douyin/server.py",
    "macos/BulletScreenLauncher.swift",
    "macos/LauncherLifecycle.swift",
)


def python_version_supported(version: Sequence[int]) -> bool:
    return tuple(version[:2]) >= MIN_PYTHON


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def resolve_python(root: Path, env: Optional[Mapping[str, str]] = None, path: Optional[str] = None) -> Optional[Path]:
    environment = dict(env or os.environ)
    configured = environment.get("BULLET_SCREEN_PYTHON", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((root / ".venv/bin/python3", root / ".venv/bin/python"))
    search_path = environment.get("PATH", os.defpath) if path is None else path
    for command in ("python3", "python"):
        resolved = shutil.which(command, path=search_path)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if _is_executable(candidate):
            # Keep the venv launcher path instead of resolving its symlink to
            # the system interpreter; the site-packages live beside the venv.
            return candidate.absolute()
    return None


def _run_python(python_executable: Path, code: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python_executable), "-c", code],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=8,
        check=False,
    )


def _result(ok: bool, detail: str) -> Dict[str, Any]:
    return {"ok": bool(ok), "detail": str(detail)}


def _probe_module(python_executable: Optional[Path], module: str, root: Path) -> Dict[str, Any]:
    if python_executable is None:
        return _result(False, "没有可用 Python")
    code = "import importlib.util; raise SystemExit(0 if importlib.util.find_spec(%r) else 1)" % module
    try:
        completed = _run_python(python_executable, code, root)
    except (OSError, subprocess.SubprocessError) as error:
        return _result(False, type(error).__name__)
    return _result(completed.returncode == 0, "已安装" if completed.returncode == 0 else "未安装")


def _probe_modules(python_executable: Optional[Path], modules: Sequence[str], root: Path) -> Dict[str, Any]:
    if python_executable is None:
        return _result(False, "没有可用 Python")
    code = (
        "import importlib.util; names=%r; "
        "raise SystemExit(0 if all(importlib.util.find_spec(name) for name in names) else 1)"
    ) % list(modules)
    try:
        completed = _run_python(python_executable, code, root)
    except (OSError, subprocess.SubprocessError) as error:
        return _result(False, type(error).__name__)
    return _result(completed.returncode == 0, "核心入口可导入" if completed.returncode == 0 else "项目模块不可导入")


def _probe_chromium(python_executable: Optional[Path], root: Path) -> Dict[str, Any]:
    if python_executable is None:
        return _result(False, "没有可用 Python")
    code = (
        "import os; from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); path=p.chromium.executable_path; p.stop(); "
        "raise SystemExit(0 if os.path.isfile(path) else 1)"
    )
    try:
        completed = _run_python(python_executable, code, root)
    except (OSError, subprocess.SubprocessError) as error:
        return _result(False, type(error).__name__)
    return _result(completed.returncode == 0, "可用" if completed.returncode == 0 else "未安装")


def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    if port == 0:
        return True
    descriptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        descriptor.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        descriptor.close()


def check_environment(
    root: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    path: Optional[str] = None,
    platform_name: Optional[str] = None,
    python_executable: Optional[Path] = None,
    executable_lookup: Optional[Callable[[str], Optional[str]]] = None,
    port: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    root = Path(root).resolve()
    environment = dict(env or os.environ)
    lookup = executable_lookup or (lambda name: shutil.which(name, path=path or environment.get("PATH", os.defpath)))
    python = python_executable or resolve_python(root, environment, path)
    python_check = _result(False, "未找到 Python 3")
    if python is not None:
        try:
            completed = _run_python(python, "import sys; print('.'.join(map(str, sys.version_info[:3])))", root)
            version = completed.stdout.strip() if completed.returncode == 0 else "无法运行"
            parts = tuple(int(item) for item in version.split(".")[:2]) if version.count(".") >= 1 and version != "无法运行" else ()
            python_check = _result(completed.returncode == 0 and python_version_supported(parts), version if version else "无法运行")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            python_check = _result(False, type(error).__name__)

    venv_python = root / ".venv/bin/python"
    entries_ok = all((root / entry).is_file() for entry in REQUIRED_ENTRIES)
    writable = os.access(str(root), os.W_OK)
    raw_port = port if port is not None else environment.get("BULLET_SCREEN_PORT", "4173") or 4173
    try:
        selected_port = int(raw_port)
        port_available = is_port_available(selected_port)
        port_check = _result(
            port_available,
            f"127.0.0.1:{selected_port} 可用" if port_available else f"127.0.0.1:{selected_port} 已占用",
        )
    except (TypeError, ValueError):
        selected_port = raw_port
        port_check = _result(False, f"端口配置无效：{raw_port}")
    swiftc_path = lookup("swiftc")
    codesign_path = lookup("codesign")
    results = {
        "macos": _result((platform_name or platform.system()) == "Darwin", "macOS" if (platform_name or platform.system()) == "Darwin" else "需要 macOS"),
        "python": python_check,
        "venv": _result(_is_executable(venv_python), "已创建" if _is_executable(venv_python) else "未创建 .venv"),
        "core_dependencies": _probe_modules(python, ("schema_v4", "bilibili.server", "douyin.server"), root),
        "playwright": _probe_module(python, "playwright", root),
        "chromium": _probe_chromium(python, root),
        "swiftc": _result(bool(swiftc_path), "可用" if swiftc_path else "未找到 Xcode Command Line Tools"),
        "codesign": _result(bool(codesign_path), "可用" if codesign_path else "未找到 macOS codesign"),
        "entries": _result(entries_ok, "入口文件完整" if entries_ok else "缺少项目入口文件"),
        "data_permissions": _result(writable, "项目目录可写" if writable else "项目目录不可写"),
        "port": port_check,
    }
    return results


def format_report(results: Mapping[str, Mapping[str, Any]]) -> str:
    labels = {
        "macos": "macOS",
        "python": "Python",
        "venv": "venv",
        "core_dependencies": "核心依赖",
        "playwright": "Playwright",
        "chromium": "Chromium",
        "swiftc": "Swift compiler",
        "codesign": "codesign（本地 ad-hoc 构建）",
        "entries": "项目入口",
        "data_permissions": "数据目录权限",
        "port": "端口",
    }
    lines = []
    for key, result in results.items():
        marker = "✓" if result.get("ok") else "✗"
        lines.append(f"{marker} {labels.get(key, key)}：{result.get('detail', '')}")
    return "\n".join(lines)


def doctor_passes(results: Mapping[str, Mapping[str, Any]]) -> bool:
    return all(bool(result.get("ok")) for result in results.values())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查 Bullet-Screen 本机运行环境")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    results = check_environment(args.root, port=args.port)
    print(format_report(results))
    return 0 if doctor_passes(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
