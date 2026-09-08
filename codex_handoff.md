# CodeCortex 交接 Checkpoint（精简版）

更新时间：2026-09-07

## 当前状态

- 项目：`/home/pc5090/Code/github/CodeCortex`
- 当前开发 worktree：`.worktrees/codecortex-m1b`
- 分支：`feature/codecortex-m1b`
- HEAD：`737e2c5 fix: preserve recovered formal anchors and benchmark auth`
- M0、M1a、M1b 实现任务及自动化门禁已完成。
- 尚未合并到 `main`，尚未 push；不要自行执行 merge/push/reset/clean/delete worktree。
- 主仓库 `main` 当前为 `10bf639`。根目录 `codex_handoff.md` 与 `.vscode/` 是未跟踪用户文件，不要提交。

## M1b 最终实现

已包含：

- 源码事实同步、baseline/change-set/freshness、受影响认知范围计算；
- bounded graph query、discussion routing、source-first/native fallback；
- Analyzer 只读隔离与 Main/Analyzer MCP profile；
- cache recovery、并发/锁/WAL/symlink 安全；
- proposal/baseline 审批与历史；
- benchmark harness、同一 Child Codex session follow-up；
- 私有结构化 Core runtime logging；
- F5 修复：恢复 cache 时保留被删除的正式实体锚点；partial scope 不再让 path evidence 掩盖 direct mapping；`--copy-auth` 同时复制到 Native 与 CodeCortex 两个隔离 home。

## 最终自动化证据

- 确定性测试：`710 passed, 3 deselected`
- MCP STDIO（sandbox 外）：`1 passed`
- Ruff：通过
- Mypy：`58 source files` 通过
- sdist/wheel build：通过
- `git diff --check`：通过
- F5 独立只读审查：PASS
- 成功 runtime log：`/tmp/codecortex-m1b-release-log-737e2c5/`，12 条 JSONL，包含 `fact_sync` 根 span 及 snapshot/parse/commit/replace 子 span；未发现源码、prompt、凭据字段。
- 默认 benchmark（不带 `--execute`）：按设计返回 exit 2 / skipped，不读取认证、不启动模型。

## 仍未完成的验收

这些不是代码门禁，且需要用户明确授权或人工操作：

1. 真实付费 Native-vs-CodeCortex Child Codex benchmark：`--execute --copy-auth`
2. 对该 benchmark 的 blind human review
3. VS Code host smoke test，包括审批与 resume

在上述项目完成前，不要宣称 M1b 已完成全部产品验收。

## 正确测试方式

M1b worktree 的 Conda editable 包可能仍指向 M1a；必须显式使用：

```bash
cd /home/pc5090/Code/github/CodeCortex/.worktrees/codecortex-m1b
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  conda run -n codecortex-dev python -m pytest -q -p no:cacheprovider
```

M1b 文档权威顺序：

1. `docs/CodeTree_Understanding_MVP.md`
2. `docs/CodeCortex_Technical_Architecture.md`
3. `docs/CodeCortex_M1b_Detailed_Design.md`
4. `docs/testing/M1B_ACCEPTANCE.md`

## 后续工作方式（重点）

- Main 只做 Controller：选 task、派发、审计 diff/测试、PASS/FIX/NEXT。
- 实现和测试交给一个 worker；worker 不得再派 agent。
- 不要让 Subagent 继承完整父线程历史；只传 task、文件范围、验收标准和必要背景。
- 新线程优先读取本精简 checkpoint，不要读取旧历史记录。
- `codex_handoff.md` 仍是用户未跟踪文件；如需更新，保留本文件的精简结构。

## Token 消耗诊断

旧主线程从 2026-08-31 持续至今，最近单轮输入约 11–12 万 tokens；部分 worker 因继承完整父线程达到约 19 万 tokens。高消耗根因是长期线程 + full-history subagent inheritance，不是 CodeCortex MCP 本身。切换 provider 不会清空线程历史。应新开线程，必要时使用 compaction，并采用 bounded worker prompt.

