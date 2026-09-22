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

首次运行先执行 `douyin/login.py`。它会打开抖音直播首页；请在该窗口完成扫码或手机号登录，然后关闭整个浏览器窗口。登录态只保存在本机 `douyin/data/browser-profile/`，该目录被 Git 忽略，不需要复制或发送 Cookie。采集器默认复用此登录目录并显示浏览器窗口。这个持久 Profile 会保留浏览器登录态和 Cookie；它与页面中手工输入的 Cookie 只在服务进程内存中使用是两件事，停止采集不会自动清除 Profile。

该 Profile 可能占用 100 MB 以上的本地空间，主要是可重建的 Chromium 缓存；Cookie 与登录状态本身很小。启动参数已把后续磁盘缓存限制在约 50 MB、媒体缓存限制在约 10 MB。目录不需要手工打开，只有登录失效时才重新运行 `login.py`；但采集期间浏览器引擎和目标直播页必须保持运行。当前抖音页面在本机 `headless` 模式会停止持续拉取事件，因此真实验收默认使用可见窗口，断开采集后窗口自动关闭。

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

页面中手工输入的 Cookie 只在本地服务进程内存中使用，不写入 SQLite、日志或 `/api/status`；真实浏览器登录态仍由持久 Profile 保存。看板通过同源 `/api/bootstrap` 获取本次服务启动的浏览器调用边界令牌，并在内存中通过 `X-Bullet-Screen-Token` 调用 `/api/status`、`/api/metrics`、`/api/events`、`/api/connect` 和 `/api/disconnect`；该令牌用于同源/跨站请求和 CSRF 类防护，不是防御同机恶意程序的认证系统。令牌不放入 URL、SQLite 或日志。请自行遵守抖音服务条款、账号权限和适用法律法规。

## API

```bash
curl http://127.0.0.1:4173/api/health
curl http://127.0.0.1:4173/api/bootstrap
# 将上一步响应中的浏览器调用边界 token 仅保存在当前终端变量，不要放入 URL 或日志
TOKEN='<ephemeral token from same-origin /api/bootstrap>'
curl -X POST http://127.0.0.1:4173/api/connect \
  -H "X-Bullet-Screen-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://live.douyin.com/123456","mode":"demo"}'
curl -H "X-Bullet-Screen-Token: $TOKEN" http://127.0.0.1:4173/api/status
curl -H "X-Bullet-Screen-Token: $TOKEN" http://127.0.0.1:4173/api/metrics
curl -H "X-Bullet-Screen-Token: $TOKEN" 'http://127.0.0.1:4173/api/events?limit=100'
curl -X POST http://127.0.0.1:4173/api/disconnect \
  -H "X-Bullet-Screen-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

`/api/events` 的事件会包含 `event_id、room_id、timestamp、type、user、content、metadata、topic、intent、sentiment、purchase_intent`。当前分析为规则优先，`rules-v2` 不使用 LLM：明确否定优先，引用/转述不升级为强信号；信号还必须满足事件数和独立用户数门槛，并携带规则版本、窗口、coverage、事实原因和证据。

规则信号通过 `/api/snapshot` 的 `metrics.signals` 返回。当前最小门槛是最近 60 秒内至少 2 条命中消息、至少 2 个独立用户；同一用户刷屏不会满足独立用户门槛。`gap` 或 `unknown` coverage 不会产生强信号，重复计算同一窗口使用稳定 `signal_id` 幂等保存。用户可以调用 `POST /api/signals/feedback`，提交 `useful`、`false_positive` 或带非空 `note` 的备注；反馈不会自动改变规则。

## 协议健康与事件静默

`/api/status` 和 `/api/snapshot` 同时返回 `protocol_available`、`last_protocol_at` 与 `last_valid_at`。`protocol_available` 有三个值：`true` 表示本次会话至少成功解码过一个正式协议 envelope（即使没有消息），`false` 表示看到了协议响应但全部不是当前支持的合法 envelope，`unknown` 表示还没有足够协议证据。页面 ready 只说明浏览器页面加载完成，不会把 `protocol_available` 变成 `true`。

`last_protocol_at` 表示最近一次通过来源校验并成功解码的正式协议响应，即使该响应没有消息也会更新；`last_valid_at` 只表示最近一次成功标准化并写入的有效事件。只有 `protocol_available=true` 且协议健康时，`EVENT_QUIET_AFTER_SECONDS=45` 才会把页面状态标成“采集正常 · 最近暂无新互动”；页面 ready 或非协议响应不会伪装成 quiet。`PROTOCOL_STALE_AFTER_SECONDS=120` 用于判断协议链是否失去可信响应，超过后才进入 `stale` 并记录缺口。这两个值是可解释的产品保护阈值，不是统计学结论。合法空 envelope 不等于采集失败，malformed/unknown 协议数据和未知 WebSocket frame 也不会刷新任一有效 freshness 时钟。

同一个正式 Collector 的 `status` 和 `snapshot.diagnostics` 还提供只读协议计数：`protocol_responses_total/valid/empty/unknown_only/malformed/source_rejected/with_events`、`protocol_messages_total`、`protocol_events_emitted`，HTTP status/Content-Type/Content-Encoding 分布、malformed stage 分布、decode 成功/失败计数，以及三个最近响应时间字段。它们只用于说明正式 `/webcast/im/fetch/` handler 实际看到了什么，不参与 freshness、coverage 或状态机决策，也不返回 body、请求头、query、原始 payload 或凭证。

## 采集边界

- 官方直播互动数据能力主要服务于已挂载直播玩法的房间，通常需要应用申请能力并启动推送任务，不能直接当作任意公开房间的通用观察 API。
- 当前真实 Adapter 的主路径是 Playwright 监听页面 `/webcast/im/fetch/`，在响应属于受支持的 `application/protobuffer` 形态时解码 Response/Message 信封，再按 `WebcastChatMessage`、`MemberMessage`、`GiftMessage`、`LikeMessage`、`SocialMessage`、`RoomStatsMessage` 等稳定字段输出事件。平台 `msg_id` 直接作为幂等事件 ID，不再从二进制中猜字符串。真实观察也发现，部分同样 ready 的公开页面只返回空 `application/json` 或短 `text/plain`，原因目前 UNKNOWN；这些页面不会被宣称为协议采集正常。
- 已经真实验证过一条完整的临时 v4 链路：页面、HTTP protobuf、标准事件、EventStore、metrics、snapshot 和 Dashboard 可以连通；这不代表所有房间或所有匿名页面都具备同样的协议能力。
- 首个协议响应可能混有历史消息：适配器用 Common/Response 的平台时间与连接开始时间比较，旧消息只播种去重；缺失平台时间时明确标记 `timestamp_source=collector_clock`，不伪装成平台事件时间。
- 页面 DOM fallback 识别抖音当前的 `webcast-chatroom___item`，只提取连接后新增且当前可见的弹幕与系统提示；首屏已有消息只用于建立去重基线，不计入实时指标。
- 协议可用时 DOM 只做对照验证，不会混入评论/礼物/点赞流；协议不可用超过 20 秒才降级为 DOM，并在 metadata 中标记 `source=dom`、`complete=false`。点赞按服务端批次统计，礼物连击数可能是累计值，Dashboard 不把它们冒充平台累计总量。
- 采集期间持续校验目标房间 URL，自动跳转到其他房间会立即停止；断开后 API 不返回上一会话数据。
- WebSocket binary transport 当前仍未作为标准事件通道支持；因此本项目不能保证所有 Douyin 房间都能可靠采集。上述能力边界属于当前真实验证结论，不应写成“Douyin 已完全支持”。这是对公开页面私有协议的观察实现，平台改版可能导致 schema 失效；后续可新增官方 Adapter 或 `DouyinSignedWebSocketAdapter`，只替换采集层，不改事件流水线、分析器和 Dashboard。

## 项目归类

B 站看板已独立归档到同级的 `../bilibili/` 子项目；本目录只负责抖音看板及其采集、分析链路。

## 文件入口

- `server.py`：抖音本地 HTTP 服务与 provider 入口。
- `douyin_adapter.py`：抖音 URL 解析、Playwright/DOM/demo Adapter、Collector。
- `live_intelligence.py`：统一事件、SQLite Store、窗口指标、规则分析和 Signal Engine。
- `index.html`、`app.js`、`styles.css`：信息密度优先的 Dashboard。
- `docs/douyin-exploration.md`：技术探索、系统设计、阶段计划和当前限制。
