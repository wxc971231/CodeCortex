# CodeCortex M0 详细设计：Codex 集成与持久化骨架

**状态：** Implementation Baseline

**前置文档：** `CodeCortex_Technical_Architecture.md`

**里程碑目标：** 在实现 Python AST 之前，证明真实 Codex Skill、MCP、Analyzer 权限隔离、审批和正式持久化链路成立。

## 1. 范围

M0 实现：

- 可安装的 Python 包和 `codecortex` 命令；
- Conda 开发环境；
- 用户级 `$codecortex` Skill；
- Main 与 Analyzer 两个 STDIO MCP profile；
- CodeCortex Analyzer 自定义 subagent 配置；
- 仓库定位和最小 `.codecortex/` 状态；
- 最小认知图、Proposal、History 和审批 apply；
- 幂等 Codex 安装与 `doctor`；
- 真实 Child Codex 集成验收。

M0 不实现：AST、完整事实索引、真实 Analyzer 建图、Freshness、任意问答和语义 Benchmark。

## 2. 工程骨架

```text
CodeCortex/
├── pyproject.toml
├── environment.yml
├── src/codecortex/
│   ├── __init__.py
│   ├── __main__.py
│   ├── domain/
│   ├── application/
│   ├── infrastructure/
│   ├── interfaces/cli/
│   ├── interfaces/mcp/
│   └── integrations/codex/
│       └── resources/
│           ├── SKILL.md
│           └── codecortex-analyzer.toml
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── doc/
```

### 2.1 Python 与依赖

- `requires-python = ">=3.14,<3.15"`；
- Hatchling 构建 wheel/sdist；
- CLI 使用标准库 `argparse`，不引入 CLI 框架；
- MCP 使用官方 Python MCP SDK；
- DTO/文件 schema 使用 Pydantic v2；
- 修改 Codex TOML 使用 `tomlkit`，以保留注释和未知配置；
- 文件锁、SQLite、JSON、hash、AST 均优先使用标准库。

`environment.yml` 只描述开发环境：Python 3.14、pip、测试和 lint 工具；运行时依赖仍由 `pyproject.toml` 声明。不得要求 pipx 用户安装 Conda。

## 3. CLI 契约

```text
codecortex --version
codecortex install-codex [--dry-run] [--force]
codecortex doctor [--json]
codecortex validate [--json]
codecortex mcp --profile main
codecortex mcp --profile analyzer
```

退出码：

| code | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数或配置错误 |
| 3 | 仓库未初始化 |
| 4 | 状态校验失败 |
| 5 | 锁或并发冲突 |
| 10 | 未预期内部错误 |

CLI stdout 只输出用户结果或 `--json` 数据；诊断和日志写 stderr。MCP 子命令的 stdout 专用于协议帧。

## 4. `install-codex` 设计

### 4.1 安装目标

```text
$HOME/.agents/skills/codecortex/SKILL.md
$HOME/.codex/agents/codecortex-analyzer.toml
$HOME/.codex/config.toml
```

安装是用户级操作，不修改业务仓库。资源从已安装 wheel 中读取，不依赖源码 checkout 路径。

### 4.2 幂等算法

1. 解析目标路径，拒绝符号链接逃逸和非普通目标文件；
2. 获取 `$HOME/.codex/.codecortex-install.lock`；
3. 读取现有文件并计算内容摘要；
4. 用 `tomlkit` 解析 Codex 配置；
5. 生成期望变更和 dry-run diff；
6. 内容完全一致时不写文件、不重复注册；
7. 实际变化前为原文件生成带 UTC 时间和随机后缀的备份；
8. 写临时文件、fsync、原子 rename；
9. 重新解析三个目标并运行安装后校验；
10. 失败时恢复本次变更前备份。

“幂等”指同一版本重复执行得到同一最终配置，不产生重复 MCP 条目或不断变化的文件。`--force` 只允许覆盖 CodeCortex 自己管理的资源，不删除其他 Skill、Agent 或 MCP 配置。

### 4.3 MCP 注册

安装器使用 `shutil.which("codecortex")` 解析当前可执行文件并写绝对路径到用户 Codex 配置。概念配置：

```toml
[mcp_servers.codecortex]
command = "/resolved/path/to/codecortex"
args = ["mcp", "--profile", "main"]
required = false
startup_timeout_sec = 10
tool_timeout_sec = 120

[mcp_servers.codecortex.tool_approvals]
apply_cognitive_proposal = "prompt"
```

Analyzer profile 写在自定义 Agent 配置中，不作为 Main 默认可见的第二套服务，避免 Main 混淆工具来源。

`install-codex` 不修改认证文件，不打印 Codex config 中的 secret 值。

## 5. Skill 契约

Skill 名称为 `codecortex`，只响应用户显式 `$codecortex`。Skill 必须告诉 Main：

1. 先确认当前仓库和 CodeCortex 状态；
2. 每次 CodeCortex 工作先执行确定性 preflight（M0 可返回 not implemented）；
3. 小范围语义任务由 Main 处理；
4. 初始化、reinitialize 和大范围分析必须派生 `codecortex-analyzer`；
5. Analyzer 只返回 `AnalysisReport`，不能修改状态；
6. 任何正式认知变化先创建 Proposal；
7. 向用户展示影响并允许讨论；
8. 只有用户明确批准当前 `patch_digest` 后才能 apply；
9. 普通问答和 Native Codex 工作不得因 CodeCortex 故障停止。

M0 Skill 支持的意图：

```text
$codecortex status
$codecortex init
$codecortex inspect
$codecortex validate
```

M1a/M1b 在不改变上述安全规则的前提下扩充 ask、sync、reinitialize 和 expand。

## 6. Analyzer Agent 配置

自定义 Agent 文件定义：

- 名称和描述明确为 repository-wide project understanding；
- sandbox 为 read-only；
- 指令要求以证据为基础，区分事实、推断和不确定项；
- 禁止修改源码和 `.codecortex/`；
- 只注册 `codecortex mcp --profile analyzer`；
- 输出符合版本化 `AnalysisReport` schema；
- 不返回大段源码，只返回实体引用、短证据摘要和覆盖信息。

Analyzer 是 Codex subagent。Core 中不得创建名为 Analyzer 的 LLM service，也不得从 Core 发起模型请求。

## 7. MCP Server 启动

启动步骤：

1. 解析 `--profile`；
2. 从 CWD 向上定位 Git root；
3. 构建只服务该 root 的 Application Service 容器；
4. 根据 profile 注册静态工具列表；
5. 将日志配置到 stderr；
6. 启动 STDIO transport。

一个 MCP 进程生命周期跟随一个 Codex 客户端。关闭客户端后 stdin 关闭，server 应完成当前不可中断的文件操作、关闭连接并退出。不得派生后台 daemon。

## 8. M0 MCP 工具

### 8.1 两个 profile 都可见

| 工具 | M0 行为 |
|---|---|
| `repository_overview()` | 返回 root、初始化状态、graph revision、正式文件存在性 |
| `cognitive_graph()` | 返回最小图或 `NOT_INITIALIZED` |
| `inspect_node(node_id)` | 返回节点、关系和 approval 引用 |
| `history_event(event_id)` | 读取并校验不可变 Event |
| `validate_graph()` | 校验 manifest、graph、entity refs 和 History 引用 |

Analyzer profile 到此结束。

### 8.2 仅 Main 可见

| 工具 | M0 行为 |
|---|---|
| `initialize_repository(config?)` | 幂等创建 revision 0 正式状态 |
| `create_cognitive_proposal(input)` | 校验并保存 pending Proposal |
| `revise_cognitive_proposal(proposal_id, revision)` | 产生新 patch digest，旧批准失效 |
| `cognitive_proposal(proposal_id)` | 分页/有界读取当前 Proposal |
| `apply_cognitive_proposal(proposal_id, approval_record)` | 原子提交并返回 Event ID、graph revision |

M1a/M1b 增加的工具在各自文档定义。profile 工具列表使用测试锁定，防止写工具意外出现在 Analyzer。

## 9. revision 0 初始化

`initialize_repository` 只创建技术骨架：

```text
manifest.graph_revision = 0
graph.nodes = []
graph.semantic_edges = []
graph.logical_flows = []
graph.implementation_mappings = []
entity_refs = []
```

同时创建默认 `config.toml`、`PROJECT.md` 模板和空 Views。它不声称已经理解项目，也不推进 cognition baseline。重复执行返回现有状态；发现半初始化或非法正式文件时拒绝覆盖。

M1a 的 `$codecortex init` 在 revision 0 上完成真实 AST、Analyzer 和初始化 Proposal，批准后产生 revision 1。

## 10. M0 最小 Proposal

M0 实现完整安全语义，但只需支持测试所需的节点/边领域操作。Proposal 文件位于：

```text
.codecortex/.cache/pending_proposals/prop_<id>.json
```

最小字段：

```yaml
schema_version: 1
proposal_id: prop_...
status: proposed
base_graph_revision: 0
analyzed_source_digest: null
source_preconditions: []
operations: []
affected_nodes: []
reason: "..."
evidence: []
uncertainties: []
revision_log: []
patch_digest: "sha256:..."
created_at: "...Z"
```

`revise` 不原地改变已展示版本：在同一 Proposal 中追加 revision record、替换 current operations 并计算新 digest。apply 只接受 current digest。

## 11. Approval Record

```yaml
proposal_id: prop_...
patch_digest: "sha256:..."
approved_by: user
approved_at: "2026-09-02T01:23:45Z"
approval_summary: "批准新增 Repository Understanding responsibility"
```

Core 验证：

- proposal 状态允许 apply；
- proposal ID 和 digest 匹配；
- base graph revision 未变化；
- approval 时间格式有效；
- approval summary 非空且长度受限；
- Patch 内存应用和全图校验成功。

Core 不独立验证自然语言消息作者；Skill/Main 是否如实构造 record 是明确的 MVP 信任边界。

## 12. M0 正式提交

成功 apply：

1. 分配 `evt_` ID；
2. 把 Event ID 作为 provenance 写入受影响正式对象；
3. graph revision 加一；
4. 写 `cognitive_proposal_applied` Event；
5. 生成 Views；
6. 按技术架构事务协议提交；
7. 将 pending Proposal 标记为 applied 或删除；正式 Event 已包含完整快照。

失败不得暴露一半 revision。apply 返回：

```json
{
  "event_id": "evt_...",
  "graph_revision": 1,
  "applied_proposal_id": "prop_..."
}
```

## 13. `doctor`

检查项：

- Python 与 package 版本；
- `codecortex` 可执行路径；
- Skill 资源存在且摘要匹配；
- Analyzer Agent 配置可解析；
- Codex config 中 Main MCP 命令可执行；
- 当前目录是否为 Git 仓库；
- 若已初始化，正式状态能否校验；
- SQLite/cache 问题只显示可重建提示；
- Main/Analyzer profile 的工具权限矩阵。

`doctor` 默认只读，不自动修复。建议动作可以提示重新运行 `install-codex` 或 cache rebuild。

## 14. 测试

### 14.1 单元测试

- TOML 合并保留无关配置和注释；
- 重复安装无 diff；
- 备份、失败回滚和符号链接拒绝；
- repository root 解析；
- profile 工具 allowlist；
- canonical Patch digest；
- approval mismatch、revision conflict；
- History 引用完整性。

### 14.2 集成测试

- 临时 Git repo revision 0 初始化；
- fixture Proposal create/revise/apply；
- apply 各阶段故障注入；
- 两个进程同时 apply，至多一个成功；
- Analyzer MCP 尝试写工具得到 tool-not-found/permission error；
- MCP stderr 日志不污染 stdout protocol。

### 14.3 真实 Codex E2E

在临时 Git 仓库运行独立 `codex exec --ephemeral --json`：

1. `$codecortex status` 能调用 Main MCP；
2. Main 能派生 Analyzer；
3. Analyzer 能读 overview，不能写；
4. 未给用户批准时 graph revision 保持不变；
5. 用 `codex exec resume` 提供明确批准后 apply 成功；
6. 禁用/破坏 CodeCortex MCP 后普通 Codex 任务仍能完成。

非交互环境无法完全代替 VS Code host approval 弹窗；M0 发布前保留一次人工 VS Code 验收。

## 15. M0 完成条件

- 全新机器安装流程有文档且 `doctor` 通过；
- Main 与 Analyzer 使用真实 Codex 配置启动；
- Analyzer 工具权限在实现层不可写；
- Proposal 未批准不可改变正式状态；
- apply 生成可追溯 History Event；
- 并发和崩溃测试没有产生部分正式 revision；
- MCP 故障不阻塞普通 Codex；
- M0 不包含任何伪 AST 或伪项目理解逻辑。
