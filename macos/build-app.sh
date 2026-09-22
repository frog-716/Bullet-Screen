#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$PROJECT_ROOT/dist/Bullet-Screen.app"
CONTENTS_DIR="$APP_DIR/Contents"

rm -rf "$APP_DIR"
mkdir -p "$CONTENTS_DIR/MacOS" "$CONTENTS_DIR/Resources"
cp "$SCRIPT_DIR/Info.plist" "$CONTENTS_DIR/Info.plist"

swiftc -O -framework Cocoa \
  -o "$CONTENTS_DIR/MacOS/BulletScreenLauncher" \
  "$SCRIPT_DIR/LauncherLifecycle.swift" \
  "$SCRIPT_DIR/BulletScreenLauncher.swift"

codesign --force --deep --sign - "$APP_DIR" >/dev/null
echo "Built: $APP_DIR"
