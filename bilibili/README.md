# BiliDanmaku · 本地直播运营看板

这是一个单机本地链路：

```text
看板浏览器 → localhost API → B站 REST（直连，WBI 回退）→ WebSocket → 二进制包/zlib 或 Brotli → 事件解析 → SQLite
```

## 运行

Python 3.9+、零第三方依赖：

```bash
python3 server.py --self-test
python3 server.py
```

然后打开 <http://127.0.0.1:4173/>。

连接设置里的房间号和 Cookie 会发送到本机服务。Cookie 支持完整浏览器 Cookie 或单独的 SESSDATA，只在本地服务进程内存中使用，不写入 SQLite、日志或 Git；普通房间通常需要登录态。

## 真实协议链路

1. `x/frontend/finger/spi` 获取匿名 `buvid3/buvid4`。
2. 优先按公开参考实现直接调用 `getDanmuInfo?id=...&type=0`；失败为 `-352` 时再获取 `x/web-interface/nav` 的每日 WBI 密钥并重试签名请求。
3. `getDanmuInfo` 成功后取得 token 和 WebSocket host 列表。
4. 建立 WebSocket，发送 op=7 认证包，并每 30 秒发送 op=2 心跳。
5. 解析 16 字节大端包头；默认使用 protover=2 的 zlib 兼容链路；安装可选的 `brotli` Python 包后自动使用并解析 protover=3。
6. 解析 `DANMU_MSG`、`SEND_GIFT`、`SUPER_CHAT_MESSAGE`、`INTERACT_WORD`、`INTERACT_WORD_V2`（最小 protobuf 字段解析）、在线人数和点赞事件。
7. 写入 `data/danmaku.sqlite3`，看板通过本地 API 每 2 秒同步。

`-352` 是 B 站风控响应，不会被降级伪装成“已连接”。
`/api/status` 会返回不含凭证的诊断信息：是否拿到 `buvid3/buvid4`、是否提交了 SESSDATA、直连/WBI 各自的返回码；不会返回 Cookie、token 或原始请求头。

## SQLite

数据库自动创建并启用 WAL：

- `sessions`：直播场次
- `events`：原始业务事件
- `metric_snapshots`：在线人数、点赞、速率快照

SQLite 文件被 `.gitignore` 排除，凭证不会进入仓库。

当前 `events` 只保留看板和指标实际使用的字段：事件类型、时间、用户、文本、礼物、金额和在线人数；原始 JSON 包和重复接收时间不再写入。
