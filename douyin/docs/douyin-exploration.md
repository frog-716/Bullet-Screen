# 抖音公开直播 Live Intelligence MVP：探索与设计

## 1. 技术探索报告

### 目标与成功标准

本阶段成功标准不是“抓到所有字段”，而是形成可运行的数据观察闭环：输入公开直播间 URL/room_id，采集器能启动、持续输出统一事件；Dashboard 能展示状态、事件流、趋势、主题、情绪、购买意图和可行动信号；采集层可被另一种 Adapter 替换。

### 候选路径比较

| 路径 | 可行性 | 稳定性 | 开发成本 | 风险 | 维护成本 | 决策 |
| --- | --- | --- | --- | --- | --- | --- |
| 官方开放能力 | 面向直播玩法和已挂载房间，不覆盖任意公开房间 | 高 | 中高 | 需应用、权限和任务生命周期 | 低 | 保留为合规扩展 |
| 页面 HTTP/XHR 分析 | 可拿元数据，实时事件不一定在 XHR | 中低 | 中 | 动态参数、风控、页面改版 | 中高 | 作为响应观察辅助 |
| 直接私有 WebSocket/Protobuf | 覆盖潜力最高 | 中 | 高 | 签名、Cookie、私有协议变更 | 高 | 后续专用 Adapter |
| Playwright 网络监听 | 可复用真实浏览器会话并观察 WebSocket 帧 | 中高 | 中 | 页面/帧协议变化 | 中 | MVP 主路径 |
| Chrome DevTools Protocol | 同样可监听网络，适合接管已有 Chrome | 中高 | 中高 | 远程调试端口、浏览器运维 | 中高 | 后续接入 |
| DOM 抓取 | 低成本，可观察可见评论 | 低 | 低 | 延迟、丢失、选择器变化 | 高 | 当前 Adapter fallback |

### 选择结论

当前采用 `DouyinPublicAdapter = Playwright WebSocket/response 观察 + JSON/gzip/protobuf-string 解析 + DOM 可见评论 fallback + 显式 demo adapter`。它把平台会话和页面变化隔离在采集层，不把不稳定的抖音私有字段泄漏到分析层。

抖音官方文档对直播评论互动能力的描述是获取“挂载该玩法的直播间”中的特定互动消息，并要求应用申请能力、启动/停止推送任务。因此它不是本 MVP“任意公开房间观察”的直接替代方案。Playwright 官方文档支持 `page.on("websocket")` 和 `framereceived`/`framesent`；CDP Network 域也提供 WebSocket 创建、收发帧和关闭事件。

来源：

- [抖音直播间评论互动能力](https://partner.open-douyin.com/docs/resource/zh-CN/interaction/jierushuoming/hudongshuju/pinglunshuju)
- [抖音直播 SDK 功能介绍](https://open.douyin.com/platform/resource/docs/ability/douyin-live-sdk/introduction/)
- [Playwright Python Network](https://playwright.dev/python/docs/network)
- [Chrome DevTools Protocol Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/)

### 当前限制与降级

1. 本机默认没有 Playwright，`demo` 模式用于离线验证全链路；真实模式缺少依赖时返回明确错误，不伪装为已连接。
2. 抖音页面推送可能使用动态签名和二进制协议；当前对压缩二进制帧做通用字符串/方法启发式解析，因此事件覆盖取决于当前页面协议。
3. 礼物、关注、分享和在线人数可能因匿名/登录状态、房间权限和平台策略缺失；事件 Schema 允许字段缺失。
4. 当前 API 用轮询，单机单房间优先；Redis/PostgreSQL、SSE/WebSocket、多房间在后续阶段接入。

### 指定房间实测记录（2026-08-21）

对 `live.douyin.com/318653495382` 做了页面和采集器双层验证：浏览器页面能加载，标题为“一梦🎙️的抖音直播间 - 抖音直播”，视频区域可见；本地真实采集器能保持 `connected/online`。在约 15 秒匿名观察窗口内，页面的 `/webcast/im/fetch/` 返回空消息体，未捕获 comment/like/gift 等互动事件，只产生“页面已打开，等待实时事件”的状态事件。该结果说明当前房间可访问，但不能证明匿名页面已暴露可解析互动流；需要登录态、互动发生或后续适配抖音当前轮询/私有协议才能提升覆盖率。

对 `live.douyin.com/50828500437` 进行了第二次实测：页面标题为“央视频的抖音直播间 - 抖音直播”，页面显示观看量和“需先登录，才能开始聊天”。本地真实采集器连接 30 秒后仍只有 1 条页面状态事件，评论/点赞/礼物/关注/分享/在线人数均为 0；因此 `connected/online` 目前只能解释为页面会话已打开，不能作为真实在线人数或互动流的证明。测试中发现非 Brotli WebSocket 帧会触发可选解码器异常，已改为未知压缩格式跳过，不再中断帧监听。

Playwright 运行时支持通过 `PLAYWRIGHT_EXECUTABLE_PATH` 指定完整浏览器；页面返回的 Brotli 压缩 JSON 由可选 `brotli` 依赖解码，核心链路仍保持零第三方依赖。

## 2. 系统设计

```text
┌──────────────────────────────┐
│ URL / room_id                 │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ DouyinPublicAdapter           │
│ Playwright WS / response / DOM│
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
  "timestamp": "2026-08-21T08:17:00.118+00:00",
  "type": "comment",
  "user": {"id": "u1", "name": "观众A"},
  "content": "这个多少钱？",
  "metadata": {"source": "websocket", "method": "ChatMessage"},
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
- Playwright WebSocket `framereceived`、JSON response、DOM fallback。
- gzip/zlib 解压和通用 protobuf 字符串探测。
- demo adapter 和明确的 Playwright 缺依赖错误。

### Phase 2：事件处理 — 已实现

- comment/like/gift/follow/share/viewer_change/live_status/entry 统一类型。
- event_id 去重、会话、SQLite 事件与指标快照。
- 状态、错误、最后事件时间、在线人数诊断。

### Phase 3：分析 — 已实现

- 10 秒、60 秒、5 分钟窗口。
- 评论/点赞/礼物/关注/分享速度、互动人数和热度分。
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
