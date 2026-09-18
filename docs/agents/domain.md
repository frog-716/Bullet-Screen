# 领域文档

工程技能探索代码库时，按本文件约定读取本仓库的领域文档。

## 探索代码前读取

- `docs/domain/CONTEXT.md`：共享的领域词汇与领域模型
- `docs/domain/adr/`：与待修改区域相关的 ADR

这些路径不存在时继续执行，无需提示。`/domain-modeling` 技能会在术语或决策得到确认时按需创建领域文档；该技能可由 `/grill-with-docs` 和 `/improve-codebase-architecture` 调用。

## 文件结构

本仓库采用单上下文布局：

```text
docs/domain/
├── CONTEXT.md
└── adr/
    ├── 0001-example-decision.md
    └── 0002-another-decision.md
```

## 使用词汇表中的术语

在工单标题、重构提案、假设或测试名称中提及领域概念时，使用 `docs/domain/CONTEXT.md` 定义的术语，不使用词汇表明确排除的同义词。

如果所需概念尚未定义，先重新判断该术语是否属于本项目；若确属缺口，记录下来交由 `/domain-modeling` 处理。

## 标记 ADR 冲突

如果输出与现有 ADR 冲突，必须明确指出冲突，不得静默覆盖既有决策。
