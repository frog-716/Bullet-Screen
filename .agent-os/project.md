# Purpose

构建本机运行的 B 站直播运营看板：接收弹幕、礼物、醒目留言、互动、在线人数和点赞等事件，写入本地 SQLite，并在浏览器展示实时数据。

# Constraints

- 凭证只在本地服务进程内存中使用，不写入 SQLite、日志或 Git。
- 核心运行链路保持 Python 3.9+、零第三方依赖；`brotli` 仅为可选能力。
- 直播数据默认保留在本地 SQLite；运行时数据库和临时文件不进入 Git。
- `/api/status` 不得暴露凭证。

# State

- 当前阶段：本地初版验证与迭代。
- 已有本地 HTTP/API 服务、浏览器看板、B 站连接与协议解析、SQLite 事件和指标存储。
- `python3 server.py --self-test` 已通过 SQLite、packet codec、zlib recursion 和 `DANMU_MSG` parser 自检。
- 下一步：用测试房间完成一次端到端连接验证；目前没有完整端到端验收证据。

# Sources

- 产品说明与运行方式：`README.md`
- 后端、协议链路与 API：`server.py`
- 前端：`index.html`、`app.js`、`styles.css`
- 本地运行数据：`data/danmaku.sqlite3`
