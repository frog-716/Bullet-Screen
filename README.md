# Bullet-Screen

本项目包含两套互不覆盖的直播看板子项目：

```text
Bullet-Screen/
├── bilibili/   # B 站直播直连看板、协议采集与历史 SQLite
└── douyin/     # 抖音 Live Intelligence 看板、采集 Adapter 与分析链路
```

## 第一次使用：先做安全 Demo 验收

本项目面向 macOS。本地运行不需要 Node；Node 只用于开发时检查浏览器 JavaScript。Bilibili 和核心服务只使用 Python 标准库，Douyin 真实浏览器模式还需要 Playwright、Chromium，以及可能的本地登录 Profile。

在一台没有本项目开发环境的新 Mac 上，先准备 Python 3.9 或更高版本；本批已在 Python 3.9.6 上实际验证，其他版本未在本批实机验证。如果系统没有 Python，请先从 Python 官方安装包安装，再重新打开终端。如果系统没有 Xcode Command Line Tools，先运行：

```bash
xcode-select --install
```

然后在项目根目录执行：

```bash
python3 --version
./scripts/setup.sh
./.venv/bin/python scripts/doctor.py
./macos/build-app.sh
./.venv/bin/python scripts/smoke_test.py
open dist/Bullet-Screen.app
```

这条路线会创建项目自己的 `.venv`，安装受控版本的 Douyin 依赖和 Playwright Chromium，检查环境，构建本地 App，并用临时 Bilibili SQLite 与 Douyin 内存 demo 验证 `health → bootstrap → snapshot → stop`。安装脚本不会 `sudo`、不会安装 Homebrew、不会修改 shell profile，也不会读取 Cookie、token、真实 Profile 或业务数据库。重复执行是安全的；遇到失败先运行 `./.venv/bin/python scripts/doctor.py`。

开发/测试依赖保持最小：测试使用 Python 标准库 `unittest`，JavaScript 只做 Node 语法检查，Launcher 由 Swift compiler 解析/构建；没有额外的开发依赖清单。

如果只想先看界面，也可以直接运行：

```bash
./.venv/bin/python douyin/server.py --port 4173 --mode demo
```

然后打开 <http://127.0.0.1:4173/>。Demo 使用内存数据库，不会触碰真实 SQLite。

## macOS 启动程序

在 macOS 上完成上面的安装和 smoke 后，可构建并双击统一启动程序：

```bash
./macos/build-app.sh
open dist/Bullet-Screen.app
```

启动窗口中选择 `Bilibili` 或 `抖音`，程序会自动寻找可用端口、启动对应子项目服务并打开看板。抖音还可选择本地演示模式；真实采集仍需要本机已安装 Playwright/Chromium。当前 App 只有 ad-hoc signing，不是 Developer ID 签名，也未公证；若 macOS 阻止打开，可在终端运行 `open dist/Bullet-Screen.app`，或在“系统设置 → 隐私与安全性”中允许打开。

第一次打开 App 时，先选择平台，再填写直播间链接或房间号；也可以点击“先试 Demo”，不用登录就能确认安装是否正常。Bilibili 通常可直接尝试公开直播间。Douyin 会先检查浏览器环境，必要时点击“登录 Douyin”；登录态只保存在本机的持久 Profile 中。页面打开后不一定代表已经读到互动数据，普通用户只需根据页面提示重试、检查直播状态或换一个公开直播间。

如果 App 被移动到项目目录之外，启动时会弹出目录选择器；选择同时包含 `bilibili/server.py` 和 `douyin/server.py` 的项目根目录即可。

Launcher 会先检查 Python、入口文件、端口和对应依赖，再等待 `/api/health` 与 `/api/bootstrap` 都成功后打开页面。停止时状态会保持为“正在停止”，直到服务进程真正退出；停止请求超时不会伪装成已停止。正常启动使用 `--port 0` 自动选择空闲端口，也可以通过 `BULLET_SCREEN_PORT` 指定端口。为做不触碰真实数据的本机演练，可设置 `BULLET_SCREEN_DB` 指向临时数据库；抖音勾选“本地演示模式”时使用内存数据库，不启动 Playwright，也不使用持久 Profile。

Launcher 也支持 `BULLET_SCREEN_ROOT` 和 `BULLET_SCREEN_PYTHON` 指定项目根目录与 Python。真实抖音模式的 preflight 只检查 Playwright/Chromium 是否可用，实际浏览器仍由服务按现有 `DOUYIN_PROFILE_DIR` 规则管理。

构建前会检查 `swiftc`、`codesign`、项目 Python 和 Launcher 源文件；缺少 Xcode Command Line Tools 时不会自动安装。

## B 站看板

```bash
cd bilibili
../.venv/bin/python server.py --self-test
../.venv/bin/python server.py
```

打开 <http://127.0.0.1:4173/>，点击右上角齿轮，在“B 站房间号”中填写数字 room ID；Cookie 可选，但普通房间通常需要登录态。连接失败时先查看页面错误，再运行 doctor 检查本机环境。

## 抖音看板

```bash
cd douyin
python3 server.py --self-test
python3 server.py --mode demo
```

抖音真实浏览器采集需要额外安装 Playwright、Chromium，并可能需要本地登录 Profile；详见 [`douyin/README.md`](douyin/README.md)。两套服务默认使用同一个端口，请勿同时启动，或为其中一个指定其他 `--port`。

抖音的“页面已打开”不等于“协议采集可用”。`/api/status` 与 `/api/snapshot` 通过 `protocol_available=true/false/unknown` 区分：只有成功解码过正式 HTTP protobuf envelope 才能显示协议采集可用；部分 ready 直播页面可能只返回空 JSON 或短文本，原因目前 UNKNOWN，WebSocket binary transport 也尚未作为标准事件通道支持。因此当前不能保证所有 Douyin 房间都可靠采集。

## 数据边界

直播历史和 Douyin 登录状态默认保存在项目目录本机，不会随删除 App 自动清除。数据位置、备份、恢复、清理、退出登录、卸载和旧数据库说明见 [`docs/DATA.md`](docs/DATA.md)。

本地页面通过同源 `/api/bootstrap` 获取本次服务启动生成的浏览器调用边界令牌，并只在页面内存中通过 `X-Bullet-Screen-Token` 调用敏感 API；令牌不放入 URL、localStorage、SQLite 或日志。它用于同源/跨站请求边界和 CSRF 类防护，不是防御同机恶意程序的认证系统。`/api/health` 可用于无令牌健康检查。

## 数据库治理

当前服务使用 schema v4。旧数据库不会被偷偷升级；遇到 v2/v3 时服务会拒绝写入并保留原文件。真实 v2 → v4 尚未完成真实数据验证，普通用户请先使用 Demo；数据安全细节见 [`docs/DATA.md`](docs/DATA.md)。

coverage API 区分四种状态：`reliable_with_data`（窗口完整且有事件）、`reliable_no_events`（有可靠采集证据但窗口内无事件）、`gap`（窗口与采集缺口重叠）和 `unknown`（没有足够证据判断完整性）。`unknown/null` 不等于数字 `0`；礼物数量、平台原始金额、币种和估算收入分别保留，不能互相替代。

v4 的 `signals` 保存规则推断本身，`signal_evidence` 通过对应数据库的事件行主键建立外键，`signal_feedback` 保存多条人工评价；`signal_id` 由 provider、room、session、run、规则版本、信号类型和时间窗口稳定生成。v3 → v4 不会自动从历史事件生成 signal。当前 `rules-v2` 只做保守候选提取：明确否定优先，引用/转述标记为不确定，不使用 LLM 或概率模型；强信号还必须同时满足事件数和独立用户数门槛，并受 coverage 约束。

当前数据库只保留看板实际读取的字段：B 站事件移除了未使用的 `received_at`、`raw_json`；抖音事件移除了未使用的 `received_at`。B 站实时看板不会读取断开前的 SQLite 会话；只有当前已连接 WebSocket 的 `session_id` 会进入页面。

## 遇到问题怎么办

当前版本接受的边界和未验证事项见[已知限制与后续计划](docs/KNOWN-LIMITATIONS.md)。

- App 或服务打不开：运行 `./.venv/bin/python scripts/doctor.py`，根据第一个 `✗` 处理 Python、Playwright、Chromium、Swift、端口或入口文件问题。
- Douyin 显示“页面已打开 · 等待协议采集”或“协议采集不可用”：这表示页面本身打开了，但当前房间没有被证明拥有可用的 HTTP protobuf 采集链。先确认直播确实在播、检查登录状态，必要时重新运行 `.venv/bin/python douyin/login.py`，或换一个公开直播间；不要把页面打开当成已采集。
- 提示旧数据库：服务不会自动修改它，原数据仍在原位置。先使用 Demo；备份与旧库说明见 [`docs/DATA.md`](docs/DATA.md)。
