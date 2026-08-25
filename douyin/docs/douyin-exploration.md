# 抖音公开直播 Live Intelligence MVP：探索与设计

## 1. 技术探索报告

### 目标与成功标准

本阶段成功标准不是“抓到所有字段”，而是形成可运行的数据观察闭环：输入公开直播间 URL/room_id，采集器能启动、持续输出统一事件；Dashboard 能展示状态、事件流、趋势、主题、情绪、购买意图和可行动信号；采集层可被另一种 Adapter 替换。

### 候选路径比较

| 路径 | 可行性 | 稳定性 | 开发成本 | 风险 | 维护成本 | 决策 |
| --- | --- | --- | --- | --- | --- | --- |
| 官方开放能力 | 面向直播玩法和已挂载房间，不覆盖任意公开房间 | 高 | 中高 | 需应用、权限和任务生命周期 | 低 | 保留为合规扩展 |
| 页面 HTTP 长轮询/Protobuf | 已确认 `/webcast/im/fetch/` 返回实时事件信封 | 高 | 中 | 私有 schema、风控、页面改版 | 中高 | MVP 主路径 |
| 直接私有 WebSocket/Protobuf | 当前目标页未观察到 WebSocket，独立连接仍需签名 | 中 | 高 | 签名、Cookie、私有协议变更 | 高 | 后续专用 Adapter |
| Playwright 网络监听 | 可复用真实浏览器会话并观察页面实际请求 | 高 | 中 | 页面/协议变化 | 中 | MVP 承载方式 |
| Chrome DevTools Protocol | 同样可监听网络，适合接管已有 Chrome | 中高 | 中高 | 远程调试端口、浏览器运维 | 中高 | 后续接入 |
| DOM 抓取 | 低成本，可观察可见评论 | 低 | 低 | 延迟、丢失、选择器变化 | 高 | 当前 Adapter fallback |

### 选择结论

当前采用 `DouyinPublicAdapter = Playwright 登录会话 + /webcast/im/fetch/ Response/Message protobuf 精确解码 + DOM 对照/降级 + 显式 demo adapter`。它把平台会话和页面变化隔离在采集层，不把不稳定的抖音私有字段泄漏到分析层。协议可用时 DOM 不输出互动事件。

抖音官方文档对直播评论互动能力的描述是获取“挂载该玩法的直播间”中的特定互动消息，并要求应用申请能力、启动/停止推送任务。因此它不是本 MVP“任意公开房间观察”的直接替代方案。Playwright 官方文档支持 `page.on("websocket")` 和 `framereceived`/`framesent`；CDP Network 域也提供 WebSocket 创建、收发帧和关闭事件。

来源：

- [抖音直播间评论互动能力](https://partner.open-douyin.com/docs/resource/zh-CN/interaction/jierushuoming/hudongshuju/pinglunshuju)
- [抖音直播 SDK 功能介绍](https://open.douyin.com/platform/resource/docs/ability/douyin-live-sdk/introduction/)
- [Playwright Python Network](https://playwright.dev/python/docs/network)
- [Chrome DevTools Protocol Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/)
- [公开逆向 schema 与脱敏样本（MIT）](https://github.com/opedium/douyin-live-proto)
- [另一份 Douyin Web protobuf 定义](https://github.com/freeloop4032/douyin-live/blob/main/dy.proto)

### 当前限制与降级

1. Playwright 已安装在项目 `.venv`；`demo` 只用于离线验证全链路并使用内存数据库，真实模式缺少依赖时返回明确错误。
2. 当前实现基于公开页面私有 protobuf schema，不是官方数据 API；平台改版、账号风控或房间策略都可能改变覆盖率，事件统一标记 `complete=false`。
3. 礼物连击数可能是累计值，点赞按服务端批次到达；Dashboard 展示事件/批次速度，不宣称平台累计总量。缺失平台时间的消息使用采集接收时间并写明 `timestamp_source=collector_clock`。
4. 当前本机测试中 headless 页面会停止持续请求，真实采集需要保持可见 Chromium 直播页；登录 Profile 可复用，不需要每次登录。
5. 当前 API 用轮询，单机单房间优先；Redis/PostgreSQL、SSE/WebSocket、多房间在后续阶段接入。

### 指定房间实测记录（2026-08-21）

对 `live.douyin.com/318653495382` 做了页面和采集器双层验证：浏览器页面能加载，标题为“一梦🎙️的抖音直播间 - 抖音直播”，视频区域可见；本地真实采集器能保持 `connected/online`。在约 15 秒匿名观察窗口内，页面的 `/webcast/im/fetch/` 返回空消息体，未捕获 comment/like/gift 等互动事件，只产生“页面已打开，等待实时事件”的状态事件。该结果说明当前房间可访问，但不能证明匿名页面已暴露可解析互动流；需要登录态、互动发生或后续适配抖音当前轮询/私有协议才能提升覆盖率。

对 `live.douyin.com/50828500437` 进行了第二次实测：页面标题为“央视频的抖音直播间 - 抖音直播”，页面显示观看量和“需先登录，才能开始聊天”。本地真实采集器连接 30 秒后仍只有 1 条页面状态事件，评论/点赞/礼物/关注/分享/在线人数均为 0；因此 `connected/online` 目前只能解释为页面会话已打开，不能作为真实在线人数或互动流的证明。测试中发现非 Brotli WebSocket 帧会触发可选解码器异常，已改为未知压缩格式跳过，不再中断帧监听。

Playwright 运行时支持通过 `PLAYWRIGHT_EXECUTABLE_PATH` 指定完整浏览器；页面返回的 Brotli 压缩 JSON 由可选 `brotli` 依赖解码，核心链路仍保持零第三方依赖。

### 匿名页面采集实验（2026-08-23，未作为真实验收）

从抖音直播首页选择当时在线房间 `119601611923`（页面标题“小u哈 ²º⁶⁷💪奋斗版的抖音直播间”）做匿名 DOM 实验。采集器先访问直播首页建立页面上下文，再进入带 `show_type=live_cover` 的房间链接；DOM fallback 识别 `webcast-chatroom___item` 后曾观察到以下页面文本事件：

- `viewer_change`：在线人数从约 2417 变化到 2943；
- `comment`：12 条，示例“来了”“晚上播吧”“老大好好休息吧，马上公会赛了”；
- `like`：2 条，内容为“为主播点赞了”；
- 指标：评论 12 条/分钟、点赞 2 条/分钟、互动用户 14、热度分 15.0。

这些记录未经过登录态和用户侧页面对照验收，不能作为真实采集完成的证据。2026-08-24 已按用户要求清空相关会话、事件和指标，后续只用登录态浏览器与用户可见直播页面逐条对照验收。

### 登录态对抗性审查与修正验收（2026-08-24）

用户在项目专用 Playwright Chromium Profile 中完成登录后，对页面数据与 API 做反向核验。审查确认原实现有四项系统性错误：把“来了”进场提示算作评论；连接时回填页面已有消息；把可见点赞/礼物提示描述为平台总量；断开后继续读取上一会话。旧会话中 53.2% 的所谓评论实际为“来了”，相关 3 个会话已清空。

修复后从空库重新连接当时在线房间 `174657918755`，约 73 秒得到：

- 首屏 18 秒只有 `live_status` 和页面在线约值，没有历史消息回填；
- 连接后新增 `comment=17`、`entry=10`、可见 `like` 提示 5、可见 `gift` 提示 2；
- “来了”全部归为 `entry`，不参与评论速率、情绪、热词或互动人数；
- 所有 DOM 事件写入 `semantics` 和 `complete=false`，不再表示平台完整总量；
- 断开后 API 返回 0 事件、0 评论、0 在线；页面 URL 跳转到其他房间时立即报错停止。

以上 DOM 结果只作为协议接入前的缺陷定位，不再作为最终验收证据。

随后对页面网络重新抓包，确认该页面没有可见 WebSocket，而是通过 `/webcast/im/fetch/` 返回 `application/protobuffer`。页面加载的 `transport-schema-im` 定义了 Response（重复 Message、server now、cursor 等）和 Message（method、payload、msg_id）信封；业务 payload 的常用字段与两份公开 schema 交叉核对后，再以真实响应验证。

最终从空库连接房间 `174657918755` 做协议级对抗测试，得到以下可复核结果：

- `/api/status`：`protocol_verified=true`，传输为 `http_long_poll_protobuf`，目标 URL 路径仍为 `/174657918755`；
- 32 条事件中有 28 条真实协议互动：`comment=3`、`entry=20`、`gift=2`、`like=1`、`viewer_change=2`，平台 `msg_id` 全部唯一；
- 评论/礼物/进场/点赞没有任何 `source=dom` 混入；DOM 只保留首次在线点值和对照用途；
- 两条协议评论的“用户名 + 正文”与同一页面可见行精确匹配；
- API 同时返回 `timestamp=+08:00` 和原始 `timestamp_utc=+00:00`，全部表示同一时刻，修复了把 UTC `19:xx` 直接显示成北京时间的问题；
- 静态资源响应使用 `Cache-Control: no-store` 并升级资源版本，防止旧页面继续运行错误时间代码。

这些证据证明 MVP 已接到该公开直播间页面实际收到的协议事件，但仍不证明覆盖抖音后台“全部”互动；平台策略、网络丢包和私有协议变化仍是明确边界。

本次同时修正了三个问题：冷启动直接进入房间导致抖音只返回骨架页；原 DOM 选择器漏掉 `webcast-chatroom___item`；页面尚未确认内容时过早标记 `connected`。结束房间现在会先识别“直播已结束”，骨架页会返回错误状态。

## 2. 系统设计

```text
┌──────────────────────────────┐
│ URL / room_id                 │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ DouyinPublicAdapter           │
│ Playwright fetch protobuf / DOM│
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Event Pipeline                │
│ normalize → dedupe → SQLite   │
│ live window 10s / 60s / 5m    │
└───────┬──────────────┬───────┘
        ▼              ▼
┌──────────────┐  ┌────────────────┐
│ Rule Analyzer│  │ Signal Engine  │
│ topic/intent │  │ trend/risk/next│
│ sentiment    │  │ action         │
└──────┬───────┘  └───────┬────────┘
       └──────────┬───────┘
                  ▼
        ┌───────────────────────┐
        │ Local HTTP API         │
        │ status/metrics/events  │
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │ Web Dashboard          │
        │ stream/trend/insights  │
        └───────────────────────┘
```

### Event Schema

```json
{
  "event_id": "sha1-or-platform-id",
  "provider": "douyin",
  "room_id": "123456",
  "timestamp": "2026-08-21T16:17:00.118+08:00",
  "timestamp_utc": "2026-08-21T08:17:00.118+00:00",
  "type": "comment",
  "user": {"id": "u1", "name": "观众A"},
  "content": "这个多少钱？",
  "metadata": {"source": "fetch_protobuf", "method": "WebcastChatMessage"},
  "topic": "price",
  "intent": "price_consultation",
  "sentiment": "neutral",
  "purchase_intent": "medium"
}
```

`live_events` 使用 `event_id UNIQUE` 做幂等去重；Cookie、WebSocket URL、token 和原始敏感请求头不进入事件 metadata。SQLite 表使用独立的 `live_*` 前缀，避免破坏原有 B 站表。

### 模块职责

- `DouyinPublicAdapter`：解析输入、管理 Playwright/浏览器会话、接收帧、做轻量协议识别、输出候选事件。
- `DouyinCollector`：管理房间会话、线程生命周期、统一事件、状态变化和快照。
- `LiveEventStore`：会话、事件、指标快照的本地持久化。
- `SignalEngine`：窗口统计、热词/问题聚合、情绪/购买意图比例、运营建议。
- `AppHandler`：HTTP API 和静态 Dashboard 服务。
- `app.js`：2 秒轮询并渲染信息密度优先的看板。

## 3. MVP 开发计划与完成状态

### Phase 1：数据获取 — 已实现

- URL/room_id 解析。
- Playwright `/webcast/im/fetch/` protobuf Response/Message 信封监听、JSON/WebSocket 兼容路径和 DOM fallback。
- 首页 warm-up、`show_type` 安全保留、直播状态 readiness 检查和 `webcast-chatroom___item` DOM fallback。
- 零生成代码的 protobuf wire 解码；精确支持 comment/entry/gift/like/follow/share/viewer/status 常用字段，未知消息不猜测。
- demo adapter 和明确的 Playwright 缺依赖错误。

### Phase 2：事件处理 — 已实现

- comment/like/gift/follow/share/viewer_change/live_status/entry 统一类型。
- event_id 去重、会话、SQLite 事件与指标快照。
- 状态、错误、最后事件时间、在线人数诊断。

### Phase 3：分析 — 已实现

- 10 秒、60 秒、5 分钟窗口。
- 评论事件速度、点赞批次/礼物/关注/分享事件速度、互动人数和观测热度；不宣称平台累计总量。
- 规则 topic、intent、sentiment、purchase_intent。
- 热词、高频问题、购买机会、负面风险和口播建议。

### Phase 4：Dashboard — 已实现

- URL/room_id、Cookie、采集模式设置。
- 实时事件流、过滤、搜索、状态卡。
- 核心指标、趋势 SVG、主题、问题、情绪条、热词、运营信号。

### Phase 5：优化 — 待后续

- 真实房间 fixture 与协议字段覆盖率统计。
- CDP attach adapter、直接签名 WebSocket adapter。
- SSE/WebSocket 推送、Redis/PostgreSQL、多房间和历史对比。
- 可选 LLM 分析、竞品横向分析和直播 Agent。
