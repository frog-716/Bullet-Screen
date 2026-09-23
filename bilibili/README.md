# BiliDanmaku · 本地直播运营看板

这是一个单机本地链路：

```text
看板浏览器 → localhost API → B站 REST（直连，WBI 回退）→ WebSocket → 二进制包/zlib 或 Brotli → 事件解析 → SQLite
```

## 运行

Python 3.9+、零第三方依赖；本批已在 Python 3.9.6 上实际验证。新 Mac 优先在项目根目录运行 `./scripts/setup.sh`，再运行 `./.venv/bin/python scripts/doctor.py` 和 `./.venv/bin/python scripts/smoke_test.py`；Node 不是运行时依赖。

```bash
../.venv/bin/python server.py --self-test
../.venv/bin/python server.py
```

然后打开 <http://127.0.0.1:4173/>。

连接设置里的房间号和 Cookie 会发送到本机服务。Cookie 支持完整浏览器 Cookie 或单独的 SESSDATA，只在本地服务进程内存中使用，不写入 SQLite、日志或 Git；普通房间通常需要登录态。看板启动后从同源 `/api/bootstrap` 获取本次服务启动的浏览器调用边界令牌，敏感 API 通过内存中的 `X-Bullet-Screen-Token` 调用；该令牌用于同源/跨站请求和 CSRF 类防护，不是防御同机恶意程序的认证系统。不要把令牌放入 URL、日志或持久化文件。

## 真实协议链路

1. `x/frontend/finger/spi` 获取匿名 `buvid3/buvid4`。
2. 优先按公开参考实现直接调用 `getDanmuInfo?id=...&type=0`；失败为 `-352` 时再获取 `x/web-interface/nav` 的每日 WBI 密钥并重试签名请求。
3. `getDanmuInfo` 成功后取得 token 和 WebSocket host 列表。
4. 建立 WebSocket，发送 op=7 认证包，并每 30 秒发送 op=2 心跳。
5. 解析 16 字节大端包头；默认使用 protover=2 的 zlib 兼容链路；安装可选的 `brotli` Python 包后自动使用并解析 protover=3。
6. 解析 `DANMU_MSG`、`SEND_GIFT`、`SUPER_CHAT_MESSAGE`、`INTERACT_WORD`、`INTERACT_WORD_V2`（最小 protobuf 字段解析）、B 站热度值和点赞事件。
7. 写入 `data/danmaku.sqlite3`，看板通过本地 API 每 2 秒同步。

`-352` 是 B 站风控响应，不会被降级伪装成“已连接”。
`/api/status` 会返回不含凭证的诊断信息：是否拿到 `buvid3/buvid4`、是否提交了 SESSDATA、直连/WBI 各自的返回码；不会返回 Cookie、token 或原始请求头。

看板只接受同时满足以下条件的数据：后端状态为已连接、事件属于当前 `session_id`、来源为 `bilibili_websocket`。未连接、连接失败或断开后，实时看板保持空白，不会回退显示 SQLite 历史记录。事件时间按浏览器本地时区显示。一个 SQLite 文件同一时间只允许一个 Bilibili 服务写入。

连接设置同时支持短房间号和真实房间号。前端分别维护“用户设置的目标房间”和“B 站解析后的活动房间”，后台轮询不会再用上一次失败连接的房间号覆盖新设置；瞬时 TLS/网络错误会对幂等 GET 请求进行最多 3 次尝试，未开播房间会明确返回“当前未开播”。

## 真实验证

2026-08-24，用户在登录态下完成真实连接测试并确认链路可用。短房间号 `7777` 已真实解析为活动房间 `545068`，并通过 WebSocket 事件回归；该结果只证明当次真实连接和房间解析链路，不代表平台接口具有永久稳定性。

## SQLite

数据库位置、备份/恢复、清理和旧库说明统一见项目级 [`docs/DATA.md`](../docs/DATA.md)。

数据库自动创建并启用 WAL：

- `sessions`：直播场次
- `events`：原始业务事件
- `metric_snapshots`：B 站热度值、点赞事件和弹幕速率快照
- `capture_gaps`：采集缺口
- `signals`、`signal_evidence`、`signal_feedback`：v4 规则信号、证据回链和人工反馈；不会根据旧事件自动脑补历史信号

SQLite 文件被 `.gitignore` 排除，凭证不会进入仓库。

当前规则信号通过 `/api/snapshot` 的 `signals` 和 `metrics.signals` 返回；`rules-v2` 只使用保守、可回放的本地规则。反馈通过受保护的 `POST /api/signals/feedback` 提交 `useful`、`false_positive` 或带非空 `note` 的评价。

当前 `events` 只保留看板和指标实际使用的字段：事件类型、时间、用户、文本、礼物、金额和 B 站热度值；原始 JSON 包和重复接收时间不再写入。
