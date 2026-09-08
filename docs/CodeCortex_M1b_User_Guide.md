# CodeCortex M1b 用户指南

CodeCortex 用于在 Codex 中维护一份可审计的项目认知图谱。它适合回答“项目负责什么、某个行为依赖哪些能力、源码变化会影响什么”等跨文件问题；它不是普通编码、搜索或测试的前置条件。

普通 Codex 编码不会自动启动 CodeCortex。只有用户在 Codex 对话中明确使用
`$codecortex` 时，才会进入这套工作流。

## 1. 安装与健康检查

下面是**终端命令**，在安装 wheel 后执行一次：

```bash
pipx install dist/codecortex-0.1.0-py3-none-any.whl
codecortex install-codex
codecortex doctor
```

`install-codex` 会安装 `$codecortex` Skill、只读 Analyzer Agent 和 Main MCP
注册。它默认把 proposal apply 工具设为 Codex 的 `approval_mode = "prompt"`，并保留
`~/.codex/config.toml` 中不属于 CodeCortex 的内容。

若 `doctor` 提示某个 CodeCortex 管理资源已被改动，先查看差异；确定要恢复包内
资源时才运行：

```bash
codecortex install-codex --force
```

`doctor` 应显示 Python、可执行文件、Skill、Analyzer、MCP 配置和工具列表均为
`OK`。在尚未初始化的项目中，`REPOSITORY_STATE` 的 warning 是正常的。

在一个已初始化的项目根目录，可随时用下面的**终端命令**检查正式状态：

```bash
codecortex validate --json
```

## 2. 在 Codex 中何时使用

下面的内容是发送到 **Codex Chat** 的提示，不是在终端粘贴的 shell 命令：

```text
$codecortex 这个项目的训练流程从输入到报告是怎样的？请给出当前源码依据。
```

适合使用 `$codecortex` 的场景包括：

- 想了解跨模块的责任、行为、能力和依赖关系。
- 想判断一批源码改动是否影响已有认知。
- 希望将经审阅的理解保存为可追溯的项目图谱。
- 想在当前源码、已有图谱和不确定性之间得到明确区分。

普通的修一个 bug、写一个函数、搜索一个符号、运行测试或阅读单个文件时，直接让
Codex 工作即可。不要为了每个日常编码动作都调用 `$codecortex`。

## 3. 第一次初始化项目

在项目根目录打开 Codex Chat，发送：

```text
$codecortex init this repository. Prepare an initial cognitive Proposal and show its scope, evidence limits, uncertainties, proposal_id, and patch_digest.
```

M1b 会创建 `.codecortex/` 技术骨架、同步确定性事实、划定分析范围，并让只读
`codecortex-analyzer` 收集受限证据。Main 再根据分析生成一个 aggregate Proposal。

首次图谱并不是对源码的替代。审阅 Proposal 时尤其关注：

- Responsibility、Behavior、Capability 是否符合项目实际边界；
- 操作 diff 是否包含你预期的节点、边、流程和实现映射；
- 证据是否指向正确的仓库相对路径和实体；
- `uncertainties`、未映射区域和受限分析范围是否可接受；
- `patch_digest` 是否对应当前展示的 Proposal。

Proposal 本身不会由 CodeCortex 静默应用。Main 会用当前 Proposal 的精确
`proposal_id` 和 `patch_digest` 调用 apply 工具；默认由 Codex 宿主对这次原生
工具调用做审批。宿主拒绝、digest 过期、图 revision 改变或源码前置条件不满足时，
Core 不会写入图谱。

不要手工复制一段旧 digest 来“批准”新 Proposal。若 Proposal 被修订或源码已经
变化，必须重新展示并使用新的精确 digest。

## 4. 日常查询：ask 与 inspect

对一个开放问题使用：

```text
$codecortex ask 训练任务失败重试时，checkpoint 与报告生成分别由哪些模块负责？请区分已批准图谱结论和当前源码证据。
```

对已有图谱锚点使用：

```text
$codecortex inspect behavior.render-report. Explain its responsibility, dependencies, mappings, evidence, and current freshness.
```

每次明确的 `ask`、`inspect`、`sync`、`expand` 或 `reinitialize` 前，Fact Preflight
会同步可重建的事实缓存，再计算当前源码相对已批准 baseline 的一个 ChangeSet。
它不会调用 Agent，也不会修改认知图谱。对于 `ask`、`inspect` 和 `sync`，Main 还应
读取并如实报告 `pending_changes`；`expand` 和 `reinitialize` 同样先完成 Preflight，
再按各自的分析范围工作。

阅读回答时要区分：

- `changed_files`：baseline 已确定的 added、modified、deleted 或 renamed 源文件。
- `unmapped_changes`：还没有正式 owner 的实体、文件或一跳依赖。它们会带 path 和
  reason，例如 `one_hop_dependency_has_no_formal_owner`。

后者不是“已修改的文件”，也不是 Fact Preflight “刷新过的源文件”。若回答把一个
`unmapped_changes` 项说成具体修改位置，应以 `pending_changes` 的原始结果为准。

查询 freshness 的含义：

| 状态 | 应如何使用回答 |
|---|---|
| `current` | 图谱与当前 baseline 一致，可将图谱作为已批准导航。 |
| `unaffected_current` | 有改动，但已证明当前问题不受影响。 |
| `affected_source_first` | 先读 `changed_files` 中的当前源码，再把图谱作为导航。 |
| `unknown_source_first` | 范围可能未解析；先读当前源码，并把图谱结论标为不确定。 |

当 `scope_confidence` 是 `partial` 或 `unknown`，它是结论边界，不是可以用语言补全
的空白。回答应明确列出未映射项和原因，而不是断言影响范围完整。

## 5. 同步、展开和重初始化

源码改动后，想主动判断是否需要更新认知，可发送：

```text
$codecortex sync this repository. Inspect pending_changes, determine whether the changes are semantic, and prepare a Proposal only if the graph needs to change.
```

如果改动不改变语义，M1b 可以走带审计记录的 baseline advance；如果需要改变图谱，
它会生成 Proposal 并进入同样的审阅和宿主审批流程。

当某个已有 Behavior 缺少足够细节，使用：

```text
$codecortex expand behavior.render-report. Analyze the current bounded scope and prepare one Proposal if formal materialization is useful.
```

对于跨 Responsibility 的大改、模型变化或需要整体重建理解的项目，使用：

```text
$codecortex reinitialize this repository. Preserve confirmed intent, analyze the current source, and show a global diff Proposal without applying it silently.
```

`reinitialize` 不会清空图谱，也不应把遗漏的节点自动解释为可删除。删除、合并、拆分和
冲突都必须作为明确 Proposal 操作展示。

## 6. 审批方式与安全边界

CodeCortex 使用 Codex 的原生 MCP 宿主审批，不提供自己的“批准按钮”，也没有
`codecortex install-codex --approval-mode approve` 这种绕过模式。

如果你在 Codex 中选择“帮我批准”/“approve for me”或启用 Guardian review，这是
**Codex 宿主**决定如何处理同一次原生工具审批：Guardian 可能自动完成 review，
因此不一定出现人工 MCP 确认请求。这不降低 Core 的校验：调用仍携带精确的
Proposal ID 和 digest，Core 仍校验 pending 状态、当前 digest、graph revision 和
源码前置条件，并写入不可变 History Event。

应用后可以在项目根目录运行：

```bash
codecortex validate --json
python -c 'import json; m=json.load(open(".codecortex/manifest.json")); print(m["graph_revision"], m["cognition_initialized"])'
find .codecortex/history/events -maxdepth 1 -type f -printf '%f\n'
```

期望看到 `validate` 为 `true`、应用后 `graph_revision` 增加、
`cognition_initialized` 为 `true`，并新增一个 history event。History 中的
Proposal ID、digest、revision 和 approval 记录应能互相对应。

## 7. 当前 smoke 仓库示例

可以用仓库内冻结的 `tests/fixtures/m1b_repo/base` 创建一次 disposable smoke 仓库：

```bash
cd /home/pc5090/Code/github/CodeCortex/.worktrees/codecortex-m1b
SMOKE_REPO=$(mktemp -d /tmp/codecortex-vscode-smoke.XXXXXX)
cp -a tests/fixtures/m1b_repo/base/. "$SMOKE_REPO"/
git -C "$SMOKE_REPO" init -q
git -C "$SMOKE_REPO" add .
git -C "$SMOKE_REPO" -c user.name=Smoke -c user.email=smoke@example.invalid commit -qm "smoke fixture"
cd "$SMOKE_REPO"
```

在该目录打开 VS Code 的 Codex Chat，然后执行第 3 节的 `$codecortex init` 提示。
完成后运行第 6 节的验证命令。这个仓库是测试用副本；不要把它当作项目正式仓库，
也不要在其中保存真实源码。VS Code、Codex Chat 和宿主审批属于外部环境，未安装时
不要把此示例当作可由 CodeCortex CLI 单独完成的测试。

## 8. 低频维护入口

日常不需要直接调用 MCP 工具。Codex Skill 会在适当时使用；需要排查路由或状态时，
以下是低频入口：

- `repository_overview()` / `initialize_repository()`：查看仓库状态，或创建 M0 技术骨架；
- `sync_repository_facts(mode="full")` / `analysis_scope(...)`：重建事实缓存并取得有界分析范围；
- `search_cognitive_graph(...)` / `get_discussion_context(...)`：为问题召回有限的图谱上下文；
- `cognitive_freshness()`：仓库级 freshness 摘要；
- `pending_changes()`：当前 ChangeSet 的分页详情；
- `effective_query_freshness()`：某个查询的 source-first 路由；
- `cognitive_proposal()`：读取 pending Proposal；
- `history_event()`：读取一次不可变应用记录；
- `validate_graph()`：MCP 内的正式状态校验。

终端维护通常只需：

```bash
codecortex doctor
codecortex validate --json
codecortex --version
```

`codecortex mcp --profile main` 和 `codecortex mcp --profile analyzer` 是给 Codex
配置启动的 STDIO 服务，不是常规交互命令；不要在普通终端会话中手动驱动其协议。

## 9. 常见误区与排错

| 现象 | 处理方式 |
|---|---|
| 普通编码时没有更新图谱 | 正常。只有明确 `$codecortex` 才会运行 Preflight 和图谱流程。 |
| `doctor` 报资源不匹配 | 查看本地改动；确认后用 `codecortex install-codex --force` 恢复管理资源。 |
| `doctor` 报未初始化 | 在 Codex Chat 使用 `$codecortex init this repository`。 |
| 提案没有应用 | 查看 Codex 宿主是否拒绝或由 Guardian review 处理；再读取 pending Proposal 和当前 digest。不要伪造 approval record。 |
| apply 因 stale/mismatch 失败 | 源码、图 revision 或 Proposal 已变；重新同步、重新分析并审阅新的 Proposal。 |
| 回答声称的改动文件与实际不符 | 读取 `pending_changes`；分开检查 `changed_files` 与 `unmapped_changes`，并按当前源码重新判断。 |
| `scope_confidence=partial/unknown` | 先读当前源码，披露未映射 path/reason；不要将图谱当成完整事实。 |
| Analyzer 无法完成分析 | 它是只读且有界的。缩小范围、提供明确问题，或让 Main 重做受限分析；不要为它开放写权限。 |

若正式状态疑似损坏，先保存现场并运行 `codecortex validate --json`。它提供稳定的错误
位置和代码；不要通过删除 `.codecortex/` 来掩盖问题。
