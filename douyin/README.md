# Live Intelligence · 抖音公开直播实时观察 MVP

这是一个本机运行的直播智能观察系统：输入抖音公开直播间 URL 或 `room_id`，由采集 Adapter 观察页面网络事件，统一成事件流，再做实时指标、规则 NLP 和运营信号分析。

```text
抖音公开直播间
      ↓
DouyinPublicAdapter（Playwright `/webcast/im/fetch/` protobuf / DOM fallback）
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

`demo` 服务使用内存数据库，退出即清空；真实服务拒绝临时切换到 `demo`，演示事件不会再混入 `data/danmaku.sqlite3`。

真实浏览器采集需要项目根目录的 `.venv` 安装 Playwright：

```bash
.venv/bin/python -m pip install playwright brotli playwright-stealth
.venv/bin/python -m playwright install chromium
# 推荐：提升抖音页面在自动化浏览器中的可见内容稳定性
.venv/bin/python douyin/login.py
.venv/bin/python douyin/server.py --mode auto
```

首次运行先执行 `douyin/login.py`。它会打开抖音直播首页；请在该窗口完成扫码或手机号登录，然后关闭整个浏览器窗口。登录态只保存在本机 `douyin/data/browser-profile/`，该目录被 Git 忽略，不需要复制或发送 Cookie。采集器默认复用此登录目录并显示浏览器窗口。

该 Profile 会占用本地空间；当前实测约 126 MB，主要是 Chromium 配置和可删除缓存，Cookie 与登录状态本身很小。启动参数已把后续磁盘缓存限制在约 50 MB、媒体缓存限制在约 10 MB。目录不需要手工打开，只有登录失效时才重新运行 `login.py`；但采集期间浏览器引擎和目标直播页必须保持运行。当前抖音页面在本机 `headless` 模式会停止持续拉取事件，因此真实验收默认使用可见窗口，断开采集后窗口自动关闭。

在看板中选择“自动 · 浏览器优先”或“浏览器监听”。如需覆盖默认行为，可显式指定：

```bash
DOUYIN_HEADLESS=0 DOUYIN_PROFILE_DIR=/tmp/douyin-live-profile .venv/bin/python douyin/server.py --mode playwright
```

如果 Playwright 只安装了完整 Chromium、没有安装 headless shell，可显式指定浏览器可执行文件：

```bash
PLAYWRIGHT_EXECUTABLE_PATH="/path/to/Google Chrome for Testing" .venv/bin/python douyin/server.py --mode playwright
```

`brotli` 为可选依赖；安装后可解析页面返回的 Brotli 压缩 JSON：`python3 -m pip install brotli`。

真实采集会先访问抖音直播首页建立页面上下文，再进入房间；房间链接中的 `show_type=live_cover/highlight` 会保留，签名或会话参数会被丢弃。可通过以下环境变量调节：

```bash
DOUYIN_WARMUP_SECONDS=8 \
DOUYIN_USER_AGENT="Mozilla/5.0 ... Chrome/148.0.0.0 Safari/537.36" \
.venv/bin/python douyin/server.py --mode playwright
```

如果页面只停留在骨架页，采集器会返回明确错误，不再伪装成已连接；需要时可使用 `DOUYIN_HEADLESS=0` 和 `DOUYIN_PROFILE_DIR` 复用本机浏览器环境。

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
- 当前真实 Adapter 的主路径是 Playwright 监听页面 `/webcast/im/fetch/`，按页面实际传输 schema 解码 `application/protobuffer` 的 Response/Message 信封，再按 `WebcastChatMessage`、`MemberMessage`、`GiftMessage`、`LikeMessage`、`SocialMessage`、`RoomStatsMessage` 等稳定字段输出事件。平台 `msg_id` 直接作为幂等事件 ID，不再从二进制中猜字符串。
- 首个协议响应可能混有历史消息：适配器用 Common/Response 的平台时间与连接开始时间比较，旧消息只播种去重；缺失平台时间时明确标记 `timestamp_source=collector_clock`，不伪装成平台事件时间。
- 页面 DOM fallback 识别抖音当前的 `webcast-chatroom___item`，只提取连接后新增且当前可见的弹幕与系统提示；首屏已有消息只用于建立去重基线，不计入实时指标。
- 协议可用时 DOM 只做对照验证，不会混入评论/礼物/点赞流；协议不可用超过 20 秒才降级为 DOM，并在 metadata 中标记 `source=dom`、`complete=false`。点赞按服务端批次统计，礼物连击数可能是累计值，Dashboard 不把它们冒充平台累计总量。
- 采集期间持续校验目标房间 URL，自动跳转到其他房间会立即停止；断开后 API 不返回上一会话数据。
- 这是对公开页面私有协议的观察实现，平台改版可能导致 schema 失效；后续可新增官方 Adapter 或 `DouyinSignedWebSocketAdapter`，只替换采集层，不改事件流水线、分析器和 Dashboard。

## 项目归类

B 站看板已独立归档到同级的 `../bilibili/` 子项目；本目录只负责抖音看板及其采集、分析链路。

## 文件入口

- `server.py`：抖音本地 HTTP 服务与 provider 入口。
- `douyin_adapter.py`：抖音 URL 解析、Playwright/DOM/demo Adapter、Collector。
- `live_intelligence.py`：统一事件、SQLite Store、窗口指标、规则分析和 Signal Engine。
- `index.html`、`app.js`、`styles.css`：信息密度优先的 Dashboard。
- `docs/douyin-exploration.md`：技术探索、系统设计、阶段计划和当前限制。
