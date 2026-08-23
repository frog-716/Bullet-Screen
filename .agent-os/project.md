# Purpose

构建本机运行的直播弹幕观察看板项目，包含两个独立子项目：B 站直连看板与抖音 Live Intelligence 看板。两套链路分别维护前端、采集入口和本地 SQLite 数据，互不覆盖。

# Constraints

- 凭证只在本地服务进程内存中使用，不写入 SQLite、日志或 Git。
- 核心运行链路保持 Python 3.9+、零第三方依赖；`brotli` 仅为可选能力。
- 抖音真实浏览器采集的 Playwright/Chromium 是可选运行时；没有安装时必须明确报错或使用显式 demo 模式，不伪装成真实连接。
- 抖音事件 metadata 不保存 query、URL、token、sessionid、cookie 或原始请求头。
- 直播数据默认保留在本地 SQLite；运行时数据库和临时文件不进入 Git。
- `/api/status` 不得暴露凭证。

# State

- 当前阶段：项目已统一更名为 `bullet-screen`，并完成 B 站与抖音子项目归类；抖音 Live Intelligence MVP 已完成本地 demo/API 验收，并完成指定抖音房间的真实浏览器连接实测；B 站原有看板与 SQLite 数据已恢复到独立目录。
- 已有本地 HTTP/API 服务、信息密度优先的 Dashboard、`DouyinPublicAdapter`（Playwright/JSON/压缩二进制/DOM/demo）、统一事件模型、SQLite 事件与指标快照、窗口分析和运营信号。
- `python3 server.py --self-test`、fixture-test、static-check、e2e-test 均已通过；已额外验证房间 `318653495382` 和 `50828500437` 的真实页面连接。后者页面可见且显示“需先登录，才能开始聊天”，但匿名采集 30 秒内未产生互动事件；`connected/online` 目前仅代表页面会话已打开。
- 采集器支持 `PLAYWRIGHT_EXECUTABLE_PATH` 指定完整 Chromium，`brotli` 为可选解码能力；已修复未知压缩 WebSocket 帧中断监听的问题。下一步是获取合规登录态/真实互动样本后，统计当前轮询与私有协议的事件覆盖率，再评估直接 WebSocket/Protobuf adapter。

# Sources

- 项目总览与运行方式：`README.md`
- B 站子项目：`bilibili/README.md`、`bilibili/server.py`、`bilibili/index.html`、`bilibili/app.js`、`bilibili/styles.css`
- B 站本地运行数据：`bilibili/data/danmaku.sqlite3`（被 Git 忽略）
- 抖音子项目：`douyin/README.md`、`douyin/server.py`、`douyin/douyin_adapter.py`、`douyin/live_intelligence.py`
- 抖音技术探索、系统设计和阶段计划：`douyin/docs/douyin-exploration.md`
- 抖音前端：`douyin/index.html`、`douyin/app.js`、`douyin/styles.css`
