# bullet-screen

本项目包含两套互不覆盖的直播看板子项目：

```text
bullet-screen/
├── bilibili/   # B 站直播直连看板、协议采集与历史 SQLite
└── douyin/     # 抖音 Live Intelligence 看板、采集 Adapter 与分析链路
```

## macOS 启动程序

在 macOS 上可构建并双击统一启动程序：

```bash
./macos/build-app.sh
open dist/BulletScreen.app
```

启动窗口中选择 `Bilibili` 或 `抖音`，程序会自动寻找可用端口、启动对应子项目服务并打开看板。抖音还可选择本地演示模式；真实采集仍需要本机已安装 Playwright/Chromium。若 macOS 没有识别签名，可在终端运行 `open dist/BulletScreen.app`，或在“系统设置 → 隐私与安全性”中允许打开。

如果 App 被移动到项目目录之外，启动时会弹出目录选择器；选择同时包含 `bilibili/server.py` 和 `douyin/server.py` 的项目根目录即可。

## B 站看板

```bash
cd bilibili
python3 server.py --self-test
python3 server.py
```

打开 <http://127.0.0.1:4173/>，连接设置与数据库均属于 `bilibili/` 子项目。

## 抖音看板

```bash
cd douyin
python3 server.py --self-test
python3 server.py --mode demo
```

抖音真实浏览器采集需要额外安装 Playwright；详见 [`douyin/README.md`](douyin/README.md)。两套服务默认使用同一个端口，请勿同时启动，或为其中一个指定其他 `--port`。

## 数据边界

本地 SQLite 位于各子项目自己的 `data/` 目录，并被 Git 忽略。Cookie 等凭证只在对应本地服务进程内存中使用，不写入数据库、日志或仓库。

## 数据库治理

当前数据库只保留看板实际读取的字段：B 站事件移除了未使用的 `received_at`、`raw_json`；抖音事件移除了未使用的 `received_at`。早期误写入 B 站库的 `live_*` 抖音表已迁移到 `douyin/data/danmaku.sqlite3`，再从 B 站库删除。迁移脚本位于 [`scripts/govern_databases.py`](scripts/govern_databases.py)，后续 schema 变更必须先备份并通过该脚本验证。

B 站实时看板不会读取断开前的 SQLite 会话；只有当前已连接 WebSocket 的 `session_id` 会进入页面。现有 B 站旧数据已清空，抖音库中明确标记 `source=demo` 的演示会话已删除。
