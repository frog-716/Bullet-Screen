#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$PROJECT_ROOT/dist/Bullet-Screen.app"
CONTENTS_DIR="$APP_DIR/Contents"
PYTHON_BIN="${BULLET_SCREEN_PYTHON:-$PROJECT_ROOT/.venv/bin/python3}"

if [[ "${1:-}" == "--help" ]]; then
  cat <<'EOF'
用法：./macos/build-app.sh

前提：macOS、Xcode Command Line Tools、已完成 ./scripts/setup.sh。
输出：dist/Bullet-Screen.app
当前仅执行 ad-hoc signing，不是 Developer ID 签名，也未公证。
可用 BULLET_SCREEN_PYTHON 指定用于版本检查的 Python。
EOF
  exit 0
fi

die() {
  print -u2 "构建失败：$1"
  exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || die "当前构建入口只支持 macOS。"
command -v swiftc >/dev/null 2>&1 || die "找不到 swiftc。请先运行 xcode-select --install。"
command -v codesign >/dev/null 2>&1 || die "找不到 codesign。请先安装 Xcode Command Line Tools。"
[[ -x "$PYTHON_BIN" ]] || die "找不到项目 Python：$PYTHON_BIN。请先运行 ./scripts/setup.sh，或设置 BULLET_SCREEN_PYTHON。"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "项目 Python 版本低于 3.9。"
[[ -f "$SCRIPT_DIR/Info.plist" ]] || die "缺少 macos/Info.plist。"
[[ -f "$SCRIPT_DIR/LauncherLifecycle.swift" ]] || die "缺少 macos/LauncherLifecycle.swift。"
[[ -f "$SCRIPT_DIR/BulletScreenLauncher.swift" ]] || die "缺少 macos/BulletScreenLauncher.swift。"

rm -rf "$APP_DIR"
mkdir -p "$CONTENTS_DIR/MacOS" "$CONTENTS_DIR/Resources"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS_DIR/Info.plist"

swiftc -O -framework Cocoa \
  -o "$CONTENTS_DIR/MacOS/BulletScreenLauncher" \
  "$SCRIPT_DIR/LauncherLifecycle.swift" \
  "$SCRIPT_DIR/BulletScreenLauncher.swift"

codesign --force --deep --sign - "$APP_DIR" >/dev/null
echo "Built: $APP_DIR"
echo "签名：ad-hoc（本地运行用途；不是 Developer ID，也未公证）"
