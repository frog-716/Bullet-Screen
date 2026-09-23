#!/usr/bin/env python3
"""Open Douyin in the same persistent browser profile used by the collector."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_PROFILE_DIR = ROOT / "data" / "browser-profile"
LOGIN_URL = "https://live.douyin.com/"


def main() -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("缺少 Playwright。请先在项目根运行：.venv/bin/python -m pip install playwright", file=sys.stderr)
        return 1

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from scripts.data_lifecycle import (
            LifecycleError,
            acquire_profile_lock,
            assert_no_pending_lifecycle,
            release_lifecycle_lock,
            resolve_profile_path,
        )
        profile_override = Path(os.environ["DOUYIN_PROFILE_DIR"]).expanduser() if os.environ.get("DOUYIN_PROFILE_DIR") else None
        profile_dir = resolve_profile_path(PROJECT_ROOT, profile_override)
        assert_no_pending_lifecycle(PROJECT_ROOT)
        profile_lock = acquire_profile_lock(profile_dir)
    except LifecycleError as error:
        print("暂时不能打开登录浏览器：" + str(error), file=sys.stderr)
        return 1
    profile_dir.mkdir(parents=True, exist_ok=True)
    print("正在打开抖音登录窗口。请完成登录后关闭整个浏览器窗口。", flush=True)
    print("登录态只保存在本机专用目录，不需要复制 Cookie。", flush=True)

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 1360, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()
            try:
                while context.pages:
                    context.pages[0].wait_for_timeout(1000)
            except PlaywrightError:
                pass
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
    except PlaywrightError as error:
        print(f"无法打开登录浏览器：{error}", file=sys.stderr)
        return 1
    finally:
        release_lifecycle_lock(profile_lock)

    print("登录浏览器已关闭，采集器将复用本机登录态。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
