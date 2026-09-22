#!/usr/bin/env python3
"""Run a safe local install smoke test using temporary data and demo mode."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")


def _start(command: Sequence[str], cwd: Path, env: Optional[Dict[str, str]] = None) -> Tuple[subprocess.Popen, int]:
    merged = os.environ.copy()
    merged.update(env or {})
    merged["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(command, cwd=str(cwd), env=merged, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    deadline = time.monotonic() + 10
    lines = []
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("服务在 ready 前退出：" + "".join(lines))
        remaining = max(0.0, deadline - time.monotonic())
        ready = selector.select(min(0.2, remaining))
        if not ready:
            continue
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            continue
        lines.append(line)
        match = URL_RE.search(line)
        if match:
            selector.close()
            return process, int(match.group(1))
    selector.close()
    raise RuntimeError("服务没有在规定时间内宣布端口：" + "".join(lines))


def _request(port: int, path: str, token: str = "", method: str = "GET", payload: Optional[dict] = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Host": f"127.0.0.1:{port}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Bullet-Screen-Token"] = token
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _check_service(command: Sequence[str], cwd: Path) -> None:
    process, port = _start(command, cwd)
    try:
        status, health = _request(port, "/api/health")
        if status != 200 or not health.get("ok"):
            raise RuntimeError("health 检查失败")
        _, bootstrap = _request(port, "/api/bootstrap")
        token = bootstrap.get("token", "")
        if not token:
            raise RuntimeError("bootstrap 没有返回临时调用令牌")
        status, snapshot = _request(port, "/api/snapshot", token)
        if status != 200 or snapshot.get("provider") not in {"bilibili", "douyin"}:
            raise RuntimeError("snapshot 检查失败")
        if snapshot.get("provider") == "douyin":
            status, _ = _request(port, "/api/connect", token, "POST", {"room_id": "123456", "mode": "demo"})
            if status not in {200, 202}:
                raise RuntimeError("demo connect 检查失败")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, current = _request(port, "/api/snapshot", token)
                if current.get("events", {}).get("items"):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("demo 没有产生内存事件")
            _request(port, "/api/disconnect", token, "POST", {})
    finally:
        _stop(process)


def run_smoke(python_executable: str = sys.executable) -> None:
    with tempfile.TemporaryDirectory(prefix="bullet-screen-smoke-") as directory:
        temporary = Path(directory)
        bilibili_db = temporary / "bilibili.sqlite3"
        _check_service(
            [python_executable, str(ROOT / "bilibili/server.py"), "--port", "0", "--db", str(bilibili_db)],
            ROOT / "bilibili",
        )
        _check_service(
            [python_executable, str(ROOT / "douyin/server.py"), "--port", "0", "--mode", "demo"],
            ROOT / "douyin",
        )
    print("install smoke: PASS (temporary Bilibili database + Douyin memory demo)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行不触碰真实数据的本机安装 smoke test")
    parser.add_argument("--python", default=str(ROOT / ".venv/bin/python"))
    args = parser.parse_args(argv)
    run_smoke(args.python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
