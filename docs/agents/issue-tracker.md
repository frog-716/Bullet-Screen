# 工单系统：本地 Markdown

本仓库的工单与规格以 Markdown 文件存放在 `docs/tickets/`。

## 约定

- 每个功能使用一个目录：`docs/tickets/<feature-slug>/`
- 规格文件为 `docs/tickets/<feature-slug>/spec.md`
- 每个实施工单独占一个文件，路径为 `docs/tickets/<feature-slug>/issues/<NN>-<slug>.md`；从 `01` 开始编号，不使用合并的工单文件
- 分诊状态记录在每个工单文件顶部附近的 `Status:` 行；角色字符串见 `triage-labels.md`
- 评论和对话历史追加到文件末尾的 `## Comments` 标题下

## 当技能要求“发布到工单系统”时

在 `docs/tickets/<feature-slug>/` 下创建新文件；目录不存在时一并创建。

## 当技能要求“获取相关工单”时

读取 `docs/tickets/` 下被引用的文件。用户通常会直接提供文件路径或工单编号。

## Wayfinding 操作

供 `/wayfinder` 使用。每份 map 文件对应多个工单子文件。

- **Map**：`docs/tickets/<effort>/map.md`，正文包含 `Notes`、`Decisions-so-far` 和 `Fog`
- **子工单**：`docs/tickets/<effort>/issues/NN-<slug>.md`，从 `01` 开始编号，正文写明问题。`Type:` 行记录工单类型（`research`/`prototype`/`grilling`/`task`），`Status:` 行记录 `claimed`/`resolved`。
- **阻塞关系**：在文件顶部附近使用 `Blocked by: NN, NN`。列出的所有工单均为 `resolved` 后，当前工单才解除阻塞。
- **Frontier**：扫描 `docs/tickets/<effort>/issues/`，查找未关闭、未阻塞且未认领的工单；编号最小者优先。
- **认领**：开始工作前设置 `Status: claimed` 并保存。
- **解决**：将答案追加到 `## Answer` 标题下，设置 `Status: resolved`，再把上下文指针（摘要和链接）追加到 `map.md` 的 `Decisions-so-far` 中。
