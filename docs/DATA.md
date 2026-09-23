# 本机数据、备份与清理

Bullet-Screen 默认把数据留在本机。它会联网访问直播平台，但项目没有把本地数据库或 Douyin 登录 Profile 上传到 Bullet-Screen 云端的流程。请把含登录状态的文件当作私密数据。

## 数据在哪里

| 内容 | 默认位置 | 属于什么 | 删除 App 后 |
| --- | --- | --- | --- |
| Bilibili 直播历史 | `bilibili/data/danmaku.sqlite3` | 用户数据 | 仍保留在项目文件夹 |
| Douyin 直播历史 | `douyin/data/danmaku.sqlite3` | 用户数据 | 仍保留在项目文件夹 |
| SQLite `-wal` / `-shm` | 与对应数据库同目录 | 数据库运行时组成部分；备份时与主库一起保存 | 仍保留在项目文件夹 |
| SQLite `.lock` | 与对应数据库同目录，文件名以 `.sqlite3.lock` 结尾 | 运行中的互斥锁；不是历史数据 | 仍保留；不要单独删除 |
| Douyin 浏览器登录状态 | `douyin/data/browser-profile/` | 私密用户数据，可能含 Cookie 和其他登录状态 | 仍保留在项目文件夹 |
| Launcher 记住的项目位置 | macOS `UserDefaults`，域名 `com.local.bullet-screen` | 本机偏好设置，不含直播历史 | 可能仍保留在 macOS 设置中 |
| Python/测试缓存 | 各代码目录中的 `__pycache__`、`.pytest_cache` | 可重新生成的缓存 | 与 App 分开；清理缓存不会清历史 |
| App 构建产物 | `dist/Bullet-Screen.app` | 可重新构建的 App | 删除 App 不会删项目数据 |
| Demo 数据 | 内存数据库 | 临时演示数据 | 服务退出后消失 |

`BULLET_SCREEN_DB` 或服务的 `--db` 参数可以把数据库改到别处；`DOUYIN_PROFILE_DIR` 可以把 Douyin Profile 改到别处。若使用了这些设置，实际数据位置以你设置的路径为准。App 还会在 macOS 设置中记住项目文件夹位置。当前数据工具只管理项目默认数据库；只要设置了 `BULLET_SCREEN_DB`，备份、恢复和清理直播历史都会停止，避免漏掉外部数据库或操作错目标。若手动用 `--db` 指定了外部位置，这些工具也不会替你管理那个外部文件；请先确认实际路径，并为它单独制定、验证安全备份方案。

页面手工输入的 Bilibili Cookie 和 Douyin Cookie 由本机服务处理，不作为直播历史保存。Douyin 的浏览器登录则由持久 Profile 保存；关闭采集不会自动退出登录。清除 Profile 才会清除此项目保存的 Douyin 登录状态。

## 备份

先关闭 Bullet-Screen 服务和 Douyin 登录浏览器。备份会尝试取得与服务共用的运行锁；如果服务或浏览器仍在使用数据，操作会停止，不会假装备份成功。运行锁只能协调 Bullet-Screen 自己的进程；其他可能读写这些数据库的本机工具也要先关闭。脚本只复制文件，不打开 SQLite，也不会触发 checkpoint。它会把主库、当时存在的 WAL/SHM 和 SHA-256 清单放入一个新目录；目录不能已存在。

```bash
./.venv/bin/python scripts/data_lifecycle.py backup --dry-run --output "$HOME/Documents/Bullet-Screen-backup"
./.venv/bin/python scripts/data_lifecycle.py backup --output "$HOME/Documents/Bullet-Screen-backup"
```

备份默认不包含 Douyin Profile。若你确实要备份本机登录状态，需明确添加 `--include-profile`：

```bash
./.venv/bin/python scripts/data_lifecycle.py backup --output "$HOME/Documents/Bullet-Screen-backup-with-login" --include-profile
```

带 Profile 的备份包含登录状态，清单会标为敏感。备份是普通文件夹，不会加密；请保存在受保护的本机磁盘或你自行加密的位置，不要发给别人或放进公开/共享目录。

清单记录创建时间、归档文件名、大小和 SHA-256。为避免打开数据库，清单里的 schema 版本会标为“未读取”，这不代表数据库已经通过完整性检查。运行锁文件不备份；它不是数据库内容，恢复时会由本机服务重新协调。

## 恢复

恢复默认只预览。预览会显示备份时间、目标项目、将新增/覆盖/移除的数据库文件；备份里的 Profile 默认不会恢复。确认看清目标后，必须同时传入 `--apply --confirm RESTORE` 才会执行。执行前工具会在备份目录旁自动建立一份“恢复前保护备份”。

```bash
./.venv/bin/python scripts/data_lifecycle.py restore --backup "$HOME/Documents/Bullet-Screen-backup"
./.venv/bin/python scripts/data_lifecycle.py restore --backup "$HOME/Documents/Bullet-Screen-backup" --apply --confirm RESTORE
```

只有当备份中含 Profile 且你确实要恢复那份 Douyin 登录状态时，才在预览和执行命令中都加 `--include-profile`。这会替换当前 Profile；执行前保护备份也会包含当前登录状态。

切换过程会先写恢复日志和阻止 Bullet-Screen 服务打开数据的标记，再逐项切换。主库、WAL、SHM 是多个文件，文件系统不能把它们作为一个 SQLite 事务一次替换；运行中的 Bullet-Screen 服务会被锁和标记挡住，外部数据库工具仍须保持关闭。若程序或电脑在中途退出，先运行：

```bash
./.venv/bin/python scripts/data_lifecycle.py recover
```

服务会在恢复标记存在时拒绝启动，直到恢复操作回滚完成或确认已完整切换。若 Profile 使用了自定义 `DOUYIN_PROFILE_DIR`，恢复时要保持相同设置。不要手工删除恢复标记或日志。

## 清理

清理命令默认只预览。建议先做备份；清理成功后不会留下供普通用户恢复的历史副本。确认后才添加对应的 `--apply --confirm` 参数。

清除直播历史（保留 Douyin 登录状态）：

```bash
./.venv/bin/python scripts/data_lifecycle.py clear --target history --provider all
./.venv/bin/python scripts/data_lifecycle.py clear --target history --provider all --apply --confirm CLEAR-HISTORY
```

退出本机 Douyin 登录并清除 Profile（不清直播历史）：

```bash
./.venv/bin/python scripts/data_lifecycle.py clear --target profile
./.venv/bin/python scripts/data_lifecycle.py clear --target profile --apply --confirm CLEAR-PROFILE
```

清理可重新生成的 Python、测试和指定 Chromium 缓存（不清 Cookie/登录状态）：

```bash
./.venv/bin/python scripts/data_lifecycle.py clear --target cache
./.venv/bin/python scripts/data_lifecycle.py clear --target cache --apply --confirm CLEAR-CACHE
```

清理数据库会删除主库及存在的 WAL/SHM，但保留 `.lock` 协调文件。清理 Profile 前要关闭采集器和 Douyin 浏览器；工具会检查项目运行锁和 Chromium 锁。普通删除不等于对 SSD 做取证级擦除；之前自行保存的备份也不会被清理。

若只要清掉 Launcher 记住的项目位置，先退出 App，再在终端运行 `defaults delete com.local.bullet-screen`。这只清本机偏好设置，不清数据库或 Profile。不要在 App 运行时执行。

## 卸载与删除项目

卸载 App 不等于删除数据。删除 `dist/Bullet-Screen.app` 只会移除 App 本身，不会删除项目文件夹、数据库、Profile 或 macOS 偏好设置。重新构建/安装后，只要原项目文件夹还在，仍可继续使用里面的数据和 Profile。

删除项目文件夹会连同其中的 Bilibili/Douyin 数据和默认 Douyin Profile 一起删除；如果通过 `BULLET_SCREEN_DB` 或 `DOUYIN_PROFILE_DIR` 把数据放到了别处，那些外部位置不会随项目删除。删除前请自行备份，并先退出 App/服务。

## 遇到旧数据库

当前服务使用 schema v4。旧 v2 或 v3 数据不会被服务自动升级（不会自动迁移）；遇到旧库时服务会拒绝打开写入，并提示“发现旧版数据。数据没有被自动修改。”原文件仍在原位置。你可以先用 Demo，或明确配置一个新的 v4 数据库路径。不要为了启动而删除、改名或覆盖旧数据库。

若现有文件已经是 v3 或 v4，v3 也不能由当前 v4 服务直接写入；v4 文件仍需通过当前结构签名检查。Launcher 会把旧 v2/v3 版本错误翻译成上述提示；如果是“结构损坏”或其他错误，不应误认为普通旧版本问题。

项目里有分开的 v2 → v3 和 v3 → v4 迁移代码，但没有用真实 v2 数据验证完整升级流程；因此目前不能向普通用户承诺真实 v2 → v4 可安全迁移。`scripts/govern_databases.py` 的默认预览会打开它指定的数据库，不是无接触检查；不要把它当作本地数据备份命令，也不要直接对真实旧库运行迁移。需要迁移旧历史时，应先做独立备份，并等待单独批准和隔离副本演练。
