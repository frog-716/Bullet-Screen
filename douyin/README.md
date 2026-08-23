# Live Intelligence · 抖音公开直播实时观察 MVP

这是一个本机运行的直播智能观察系统：输入抖音公开直播间 URL 或 `room_id`，由采集 Adapter 观察页面网络事件，统一成事件流，再做实时指标、规则 NLP 和运营信号分析。

```text
抖音公开直播间
      ↓
DouyinPublicAdapter（Playwright WebSocket / JSON / DOM fallback）
      ↓
统一 Event Schema → SQLite 会话与事件
      ↓
10s / 60s / 5m 指标 → topic / intent / sentiment → Signal Engine
      ↓
本地 HTTP API → Web Dashboard
```

## 快速运行

Python 3.9+、核心链路零第三方依赖：

```bash
python3 server.py --self-test
python3 server.py --mode demo
```

打开 <http://127.0.0.1:4173/>，在“观察设置”输入任意数字 room_id，选择“本地演示”，即可离线看到评论、点赞、礼物、关注、分享、在线人数、热词、情绪和运营信号。

真实浏览器采集是可选能力：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
python3 server.py --mode auto
```

在看板中选择“自动 · 浏览器优先”或“浏览器监听”。如果需要使用登录环境，可以在本地设置 Cookie，或使用单独的持久化浏览器目录：

```bash
DOUYIN_HEADLESS=0 DOUYIN_PROFILE_DIR=/tmp/douyin-live-profile python3 server.py --mode playwright
```

如果 Playwright 只安装了完整 Chromium、没有安装 headless shell，可显式指定浏览器可执行文件：

```bash
PLAYWRIGHT_EXECUTABLE_PATH="/path/to/Google Chrome for Testing" python3 server.py --mode playwright
```

`brotli` 为可选依赖；安装后可解析页面返回的 Brotli 压缩 JSON：`python3 -m pip install brotli`。

Cookie 只在本地服务进程内存中使用，不写入 SQLite、日志或 `/api/status`；请自行遵守抖音服务条款、账号权限和适用法律法规。

## API

```bash
curl http://127.0.0.1:4173/api/health
curl -X POST http://127.0.0.1:4173/api/connect \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://live.douyin.com/123456","mode":"demo"}'
curl http://127.0.0.1:4173/api/status
curl http://127.0.0.1:4173/api/metrics
curl 'http://127.0.0.1:4173/api/events?limit=100'
curl -X POST http://127.0.0.1:4173/api/disconnect -H 'Content-Type: application/json' -d '{}'
```

`/api/events` 的事件会包含 `event_id、room_id、timestamp、type、user、content、metadata、topic、intent、sentiment、purchase_intent`。当前分析为规则优先，分析器和 Store 都是可替换边界，后续可以接 LLM、Redis、PostgreSQL 或 SSE/WebSocket。

## 采集边界

- 官方直播互动数据能力主要服务于已挂载直播玩法的房间，通常需要应用申请能力并启动推送任务，不能直接当作任意公开房间的通用观察 API。
- 当前真实 Adapter 优先监听 Playwright 页面 WebSocket 帧和 JSON 响应；对压缩二进制帧做通用 protobuf 字符串提取，并用 DOM 可见评论作兜底。
- 这是一套稳定的数据观察降级链路，不承诺拿到所有私有协议字段；礼物、关注、分享和在线人数是否出现取决于页面会话、登录态和平台当前推送。
- 后续若需要更高覆盖率，可以新增 `DouyinSignedWebSocketAdapter`，只替换采集层，不改事件流水线、分析器和 Dashboard。

## 项目归类

B 站看板已独立归档到同级的 `../bilibili/` 子项目；本目录只负责抖音看板及其采集、分析链路。

## 文件入口

- `server.py`：抖音本地 HTTP 服务与 provider 入口。
- `douyin_adapter.py`：抖音 URL 解析、Playwright/DOM/demo Adapter、Collector。
- `live_intelligence.py`：统一事件、SQLite Store、窗口指标、规则分析和 Signal Engine。
- `index.html`、`app.js`、`styles.css`：信息密度优先的 Dashboard。
- `docs/douyin-exploration.md`：技术探索、系统设计、阶段计划和当前限制。
