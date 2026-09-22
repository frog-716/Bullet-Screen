#!/usr/bin/env python3
"""Create the project virtual environment and install controlled dependencies."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from doctor import MIN_PYTHON, check_environment, doctor_passes, format_report, python_version_supported


def create_venv(root: Path, python_executable: str, runner: Callable = subprocess.run) -> bool:
    target = Path(root) / ".venv"
    if (target / "bin" / "python").is_file():
        return False
    runner([python_executable, "-m", "venv", str(target)], check=True)
    return True


def install_requirements(python_executable: str, requirement_files: Tuple[Path, ...], runner: Callable = subprocess.run) -> None:
    for requirement in requirement_files:
        runner([python_executable, "-m", "pip", "install", "--requirement", str(requirement)], check=True)


def _python_version(python_executable: str) -> Tuple[int, int, int]:
    completed = subprocess.run(
        [python_executable, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(int(item) for item in completed.stdout.strip().split("."))  # type: ignore[return-value]


def _fail(message: str) -> int:
    print(f"安装未完成：{message}", file=sys.stderr)
    return 1


def run_setup(root: Path, python_executable: str, dry_run: bool = False) -> int:
    root = Path(root).resolve()
    if platform.system() != "Darwin":
        return _fail("本批安装入口面向 macOS；请在 macOS 上运行。")
    if not shutil.which("swiftc") or not shutil.which("codesign"):
        return _fail("缺少 Xcode Command Line Tools。请先运行 xcode-select --install，完成后重新运行 setup。")
    try:
        version = _python_version(python_executable)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return _fail(f"无法运行 Python：{error}")
    if not python_version_supported(version):
        return _fail(f"Python 版本 {'.'.join(map(str, version))} 不受支持，需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高版本。")
    venv = root / ".venv" / "bin" / "python"
    requirements = (root / "requirements.txt", root / "requirements-douyin.txt")
    if any(not path.is_file() for path in requirements):
        return _fail("依赖清单缺失，请确认项目下载完整。")
    if dry_run:
        print(f"Python: {python_executable}")
        print(f"venv: {root / '.venv'}")
        print("将安装 requirements.txt、requirements-douyin.txt，并执行 playwright install chromium。")
        print("将运行 doctor，不会读取 Cookie、token、Profile 或数据库内容。")
        return 0
    try:
        created = create_venv(root, python_executable)
        if created:
            print(f"已创建 {root / '.venv'}")
        install_requirements(str(venv), requirements)
        subprocess.run([str(venv), "-m", "playwright", "install", "chromium"], check=True)
    except (OSError, subprocess.SubprocessError) as error:
        return _fail(f"依赖安装失败：{error}")
    results = check_environment(root, python_executable=venv)
    print(format_report(results))
    if not doctor_passes(results):
        return _fail("doctor 未通过，请根据上面的 ✗ 处理后重新运行。")
    print("安装完成。下一步：./macos/build-app.sh，然后运行 .venv/bin/python scripts/smoke_test.py。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="安装 Bullet-Screen 的受控 Python 依赖")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=os.environ.get("BULLET_SCREEN_PYTHON", "python3"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return run_setup(args.root, args.python, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
