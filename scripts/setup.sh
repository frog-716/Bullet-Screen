#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${BULLET_SCREEN_PYTHON:-python3}"

if [[ "${1:-}" == "--help" ]]; then
  cat <<'EOF'
用法：./scripts/setup.sh [--dry-run]

创建项目 .venv，安装受控 Python 依赖和 Playwright Chromium，并运行只读 doctor。
不会 sudo、不会安装 Homebrew、不会修改 shell profile、不会读取业务数据。
EOF
  exit 0
fi

if [[ "$PYTHON_BIN" == */* ]]; then
  [[ -x "$PYTHON_BIN" ]] || { print -u2 "安装失败：找不到 Python：$PYTHON_BIN。请安装 Python 3.9+，或设置 BULLET_SCREEN_PYTHON。"; exit 1; }
else
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || { print -u2 "安装失败：找不到 $PYTHON_BIN。请安装 Python 3.9+，或设置 BULLET_SCREEN_PYTHON。"; exit 1; }
fi

exec "$PYTHON_BIN" "$ROOT/scripts/setup.py" --root "$ROOT" --python "$PYTHON_BIN" "$@"
