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

- 当前阶段：项目已统一更名为 `Bullet-Screen`，完成 B 站与抖音子项目归类，并完成原生 macOS 启动器；启动器可选择平台、自动寻找可用端口、拉起对应服务并打开看板。抖音 Live Intelligence MVP 已接入公开页面 `/webcast/im/fetch/` protobuf 长轮询并完成登录态协议级验收；它能证明收到页面实际事件，但不宣称是官方完整总量。B 站原有看板与 SQLite 数据已恢复到独立目录。
- 已完成 SQLite 治理：B 站早期遗留的抖音 `live_*` 表已迁移到抖音库；删除 B 站事件未使用的 `received_at`、`raw_json`，删除抖音事件未使用的 `received_at`；迁移前备份已保存在临时目录。
- macOS 启动器已支持 App 脱离项目目录后通过目录选择器重新绑定 `Bullet-Screen` 项目根目录。
- B 站旧会话数据已按用户授权清空；抖音明确标记 `source=demo` 的演示会话已删除。B 站 API 与前端现在只展示当前已连接 `session_id` 且来源为 `bilibili_websocket` 的数据，断开时返回空数据，并用文件锁阻止多个服务同时写同一 SQLite；该 B 站版本已由用户完成登录态真实连接测试并确认通过。
- B 站连接已修复目标房间被旧后端状态覆盖的问题，目标短号与解析后的活动房间分开维护；短号 `7777` 已真实解析到房间 `545068` 并通过 WebSocket 事件回归。HTTP GET 增加瞬时网络重试，未开播房间会明确报错。
- 已有本地 HTTP/API 服务、信息密度优先的 Dashboard、`DouyinPublicAdapter`（Playwright fetch protobuf/JSON/WebSocket 兼容/DOM 降级/demo）、统一事件模型、SQLite 事件与指标快照、窗口分析和运营信号。
- 2026-08-24 已按用户要求清空抖音库全部历史测试数据：`live_sessions=0`、`live_events=0`、`live_metric_snapshots=0`。此前房间 `119601611923` 的 DOM 记录不再作为真实验收证据；`318653495382`、`50828500437` 也不保留测试会话。
- 用户已于 2026-08-24 在项目专用 Chromium Profile 完成登录。随后对抗审查发现旧 DOM 实现把“来了”进场提示误算为评论、回填首屏历史消息、把页面点赞提示误称为总点赞，并在断开后继续展示旧会话；受影响的 3 个会话已全部清空。
- 2026-08-24 对房间 `174657918755` 从空库做最终协议验收：32 条事件中有 28 条 `fetch_protobuf` 互动（comment=3、entry=20、gift=2、like=1、viewer_change=2），平台 msg_id 全部唯一；评论/礼物/进场/点赞无 DOM 混入，并有 2 条协议评论与页面用户名+正文精确匹配。API 北京时间 `+08:00` 与保留的 UTC 表示同一时刻，静态资源禁止缓存。
- 显式 `demo` 服务使用内存数据库，真实服务拒绝切换到 `demo`，防止演示事件再次混入真实 SQLite。Dashboard 明确使用评论事件、点赞批次、礼物事件和协议在线人数口径；协议事件均标记 `complete=false`，后续只有官方能力才能给出平台总量语义。当前本机 headless 页面会停止持续请求，真实采集需维持可见 Chromium；Profile 可复用且当前约 126 MB，断开后浏览器自动关闭。

# Sources

- 项目总览与运行方式：`README.md`
- macOS 统一启动器：`macos/BulletScreenLauncher.swift`、`macos/build-app.sh`、`macos/Info.plist`
- B 站子项目：`bilibili/README.md`、`bilibili/server.py`、`bilibili/index.html`、`bilibili/app.js`、`bilibili/styles.css`
- B 站本地运行数据：`bilibili/data/danmaku.sqlite3`（被 Git 忽略）
- 抖音子项目：`douyin/README.md`、`douyin/server.py`、`douyin/douyin_adapter.py`、`douyin/live_intelligence.py`
- 抖音技术探索、系统设计和阶段计划：`douyin/docs/douyin-exploration.md`
- 抖音前端：`douyin/index.html`、`douyin/app.js`、`douyin/styles.css`
- 数据库治理与迁移：`scripts/govern_databases.py`
