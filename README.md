# Bullet-Screen

本项目包含两套互不覆盖的直播看板子项目：

```text
Bullet-Screen/
├── bilibili/   # B 站直播直连看板、协议采集与历史 SQLite
└── douyin/     # 抖音 Live Intelligence 看板、采集 Adapter 与分析链路
```

## macOS 启动程序

在 macOS 上可构建并双击统一启动程序：

```bash
./macos/build-app.sh
open dist/Bullet-Screen.app
```

启动窗口中选择 `Bilibili` 或 `抖音`，程序会自动寻找可用端口、启动对应子项目服务并打开看板。抖音还可选择本地演示模式；真实采集仍需要本机已安装 Playwright/Chromium。若 macOS 没有识别签名，可在终端运行 `open dist/Bullet-Screen.app`，或在“系统设置 → 隐私与安全性”中允许打开。

如果 App 被移动到项目目录之外，启动时会弹出目录选择器；选择同时包含 `bilibili/server.py` 和 `douyin/server.py` 的项目根目录即可。

## B 站看板

```bash
cd bilibili
python3 server.py --self-test
python3 server.py
```

打开 <http://127.0.0.1:4173/>，连接设置与数据库均属于 `bilibili/` 子项目。

## 抖音看板

```bash
cd douyin
python3 server.py --self-test
python3 server.py --mode demo
```

抖音真实浏览器采集需要额外安装 Playwright；详见 [`douyin/README.md`](douyin/README.md)。两套服务默认使用同一个端口，请勿同时启动，或为其中一个指定其他 `--port`。

## 数据边界

本地 SQLite 位于各子项目自己的 `data/` 目录，并被 Git 忽略。Bilibili 页面输入的 Cookie 和 Douyin 页面手工输入的 Cookie 只在对应本地服务进程内存中使用，不写入数据库、日志或仓库。

Douyin 真实浏览器模式默认使用 `douyin/data/browser-profile/` 持久 Profile；该目录可能保存浏览器登录态和 Cookie。它不是“仅内存凭证”，关闭采集不会自动删除登录态。`--mode demo` 使用内存 SQLite，退出后清空；真实数据库和 Profile 都是本机运行数据，不应当当作静态网页资源或临时缓存处理。

本地页面通过同源 `/api/bootstrap` 获取本次服务启动生成的浏览器调用边界令牌，并只在页面内存中通过 `X-Bullet-Screen-Token` 调用敏感 API；令牌不放入 URL、localStorage、SQLite 或日志。它用于同源/跨站请求边界和 CSRF 类防护，不是防御同机恶意程序的认证系统。`/api/health` 可用于无令牌健康检查。

## 数据库治理

当前服务支持的数据库 schema version 是 `4`。本机现有数据库可能仍处于 v2；v3 也不能被 v4 服务直接写入。打开已有数据库时会同时检查版本和 schema signature（表、列属性、索引、唯一约束、foreign key 与 check constraint）；版本过旧、未知、更新或结构损坏都会拒绝继续写入。空数据库和 `--mode demo` 仍可创建内存/新数据库。旧数据库不会被服务偷偷升级；迁移必须显式执行。

迁移脚本位于 [`scripts/govern_databases.py`](scripts/govern_databases.py)，正式流程是锁定、复制 SQLite/WAL/SHM、只在隔离副本上验证和迁移、验证 candidate、持久化状态后切换，并保留原数据库用于恢复。v3 → v4 只创建空的 `signals`、`signal_evidence`、`signal_feedback` 表，不会根据旧事件脑补历史 signal；重复执行已是 v4 的目标会保持不变。本 checkpoint 没有对真实 v2/v3 数据库执行 migration；任何真实操作前都必须先使用隔离副本完成演练。

coverage API 区分四种状态：`reliable_with_data`（窗口完整且有事件）、`reliable_no_events`（有可靠采集证据但窗口内无事件）、`gap`（窗口与采集缺口重叠）和 `unknown`（没有足够证据判断完整性）。`unknown/null` 不等于数字 `0`；礼物数量、平台原始金额、币种和估算收入分别保留，不能互相替代。

v4 的 `signals` 只保存规则推断本身，`signal_evidence` 通过对应数据库的事件行主键建立外键，`signal_feedback` 保存多条人工评价；`signal_id` 由 provider、room、session、run、规则版本、信号类型和时间窗口稳定生成。v3 → v4 不会自动从历史事件生成 signal。

当前数据库只保留看板实际读取的字段：B 站事件移除了未使用的 `received_at`、`raw_json`；抖音事件移除了未使用的 `received_at`。早期误写入 B 站库的 `live_*` 抖音表已迁移到 `douyin/data/danmaku.sqlite3`，再从 B 站库删除。

B 站实时看板不会读取断开前的 SQLite 会话；只有当前已连接 WebSocket 的 `session_id` 会进入页面。现有 B 站旧数据已清空；抖音错误历史会话和修正验证会话也都已清空，下一次用户对照确认从空库开始。
