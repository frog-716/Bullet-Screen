# Purpose

构建可在 macOS 上运行的直播弹幕观察程序：由统一启动器选择 B 站或抖音，再启动对应的独立看板、采集入口和本地 SQLite 数据链路。

# Constraints

- 凭证只在本地服务进程内存中使用，不写入 SQLite、日志或 Git。
- 核心运行链路保持 Python 3.9+、零第三方依赖；`brotli` 仅为可选能力。
- 抖音真实浏览器采集的 Playwright/Chromium 是可选运行时；没有安装时必须明确报错或使用显式 demo 模式，不伪装成真实连接。
- 抖音事件 metadata 不保存 query、URL、token、sessionid、cookie 或原始请求头。
- 直播数据默认保留在本地 SQLite；运行时数据库和临时文件不进入 Git。
- `/api/status` 不得暴露凭证。

# State

- 当前阶段：项目已统一更名为 `bullet-screen`，完成 B 站与抖音子项目归类，并完成原生 macOS 启动器；启动器可选择平台、自动寻找可用端口、拉起对应服务并打开看板。抖音 Live Intelligence MVP 已完成本地 demo/API 验收，并在当前在线房间完成有效数据实测；B 站原有看板与 SQLite 数据已恢复到独立目录。
- 已完成 SQLite 治理：B 站早期遗留的抖音 `live_*` 表已迁移到抖音库；删除 B 站事件未使用的 `received_at`、`raw_json`，删除抖音事件未使用的 `received_at`；迁移前备份已保存在临时目录。
- macOS 启动器已支持 App 脱离项目目录后通过目录选择器重新绑定 `bullet-screen` 项目根目录。
- B 站旧会话数据已按用户授权清空；抖音明确标记 `source=demo` 的演示会话已删除。B 站 API 与前端现在只展示当前已连接 `session_id` 且来源为 `bilibili_websocket` 的数据，断开时返回空数据，并用文件锁阻止多个服务同时写同一 SQLite；该 B 站版本已由用户完成登录态真实连接测试并确认通过。
- 已有本地 HTTP/API 服务、信息密度优先的 Dashboard、`DouyinPublicAdapter`（Playwright/JSON/压缩二进制/DOM/demo）、统一事件模型、SQLite 事件与指标快照、窗口分析和运营信号。
- `python3 douyin/server.py --self-test`、DOM fixture-test、static-check、e2e-test 均已通过；房间 `119601611923` 修复后真实回归最近 60 秒得到约 3559 在线、35 条 comment、7 条 like、36 个活跃用户，累计缓冲还观察到 1 条 gift，指标已进入 Dashboard 数据链路。此前的 `318653495382` 已结束，`50828500437` 页面显示需登录且未产生互动。
- 采集器支持首页 warm-up、`show_type` 安全保留、`PLAYWRIGHT_EXECUTABLE_PATH`/`DOUYIN_USER_AGENT` 配置、Brotli 和可选 stealth；已修复未知压缩帧、骨架页误连、DOM 选择器漏采集等问题。下一步是继续覆盖 gift/follow/share 事件，并评估直接 WebSocket/Protobuf adapter。

# Sources

- 项目总览与运行方式：`README.md`
- macOS 统一启动器：`macos/BulletScreenLauncher.swift`、`macos/build-app.sh`、`macos/Info.plist`
- B 站子项目：`bilibili/README.md`、`bilibili/server.py`、`bilibili/index.html`、`bilibili/app.js`、`bilibili/styles.css`
- B 站本地运行数据：`bilibili/data/danmaku.sqlite3`（被 Git 忽略）
- 抖音子项目：`douyin/README.md`、`douyin/server.py`、`douyin/douyin_adapter.py`、`douyin/live_intelligence.py`
- 抖音技术探索、系统设计和阶段计划：`douyin/docs/douyin-exploration.md`
- 抖音前端：`douyin/index.html`、`douyin/app.js`、`douyin/styles.css`
- 数据库治理与迁移：`scripts/govern_databases.py`
