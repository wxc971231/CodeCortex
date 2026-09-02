# CodeCortex 技术架构详细设计

**状态：** M0/M1a/M1b Implementation Baseline

**适用平台：** Linux、macOS、WSL

**开发运行时：** Python 3.14

**总体规格：** `CodeTree_Understanding_MVP.md`

## 1. 文档目的与约束

本文定义 M0、M1a 和 M1b 共同遵守的技术架构。总体规格决定产品目标、认知模型和审批边界；本文决定组件边界、数据所有权、持久化、并发、错误和部署方式。若详细设计与总体规格冲突，以总体规格为准并先修正文档，不允许实现自行选择一种解释。

MVP 只实现 Codex-first 版本：

- 用户通过显式 `$codecortex` Skill 使用产品；
- Main Codex 负责自然语言交互、语义判断和审批对话；
- CodeCortex Core 不调用任何 LLM；
- CodeCortex Analyzer 是 Codex subagent，不是 Core 内部模块；
- 不提供 Web UI、FastAPI 服务、常驻 daemon、向量数据库或模型供应商适配层；
- 不原生支持 Windows，但仓库中的正式数据格式保持跨平台可移植。

## 2. 核心架构决策

| 决策 | 结论 |
|---|---|
| 产品形态 | Codex Skill + 本地 STDIO MCP + Python Core |
| 进程模型 | 每个活跃 Codex 客户端可启动一个 MCP 进程，无常驻共享服务 |
| Main/Analyzer | 都访问同一个 Core；Main 使用完整工具集，Analyzer 使用只读工具集 |
| 正式真相 | Git 中 `.codecortex/` 的认知 JSON、源码基线摘要、History 和 Markdown Views |
| 高速索引 | `.codecortex/.cache/facts.sqlite3`，可删除、可重建 |
| Agent 数据流 | Analyzer 返回有界且压缩的 `AnalysisReport` 给 Main，不写临时分析数据库 |
| 代码解析 | Python 标准库 `ast`；被分析语法范围 Python 3.9～3.14 |
| 开发环境 | Conda；`pyproject.toml` + Hatchling 打包 |
| 用户安装 | 首选 pipx；不要求用户使用 Conda |
| 并发 | 仓库级 OS 文件锁 + revision/source preconditions + SQLite 事务 |
| Freshness | 每次 CodeCortex 工作前固定 Fact Preflight；不监听普通 Codex 编码 |

## 3. 系统上下文

```text
用户
  │ 自然语言、批准/拒绝
  ▼
Main Codex + $codecortex Skill
  ├── Codex 原生搜索、读源码、subagent 调度
  ├── Main MCP（读 + 受控维护/写）
  │      ▼
  │   CodeCortex Core
  │      ├── 正式认知状态（Git）
  │      └── SQLite 查询副本（本机 cache）
  │
  └── CodeCortex Analyzer Agent（临时 subagent）
         ├── Codex 原生搜索、读源码
         └── Analyzer MCP（只读）→ 同一个 Core
```

MCP 封装的是 Core 的规则化能力，不封装 Agent 推理。Main Codex 通过 Codex 自身机制派生 Analyzer；Analyzer 的最终 `AnalysisReport` 通过 subagent 返回通道交给 Main。

## 4. 质量属性

架构按以下优先级取舍：

1. **正式认知不丢失、不被未审批修改。**
2. **源码事实可靠且可以重新计算。**
3. **普通 Codex 不依赖 CodeCortex 可用性。**
4. **中型 Python 仓库查询即时、上下文有界。**
5. **另一台机器 clone 后可以恢复正式认知。**
6. **实现简单、行为可测试，优先于极限性能。**

## 5. 代码分层

推荐源码结构：

```text
src/codecortex/
├── domain/
│   ├── cognition/        # 认知节点、边、Flow、Mapping、图约束
│   ├── facts/            # CodeEntity、语法事实、推断关系
│   ├── proposals/        # Proposal、Patch、审批和 History Event
│   ├── freshness/        # SourceDigest、ChangeSet、局部 Freshness
│   └── errors.py         # 稳定领域错误码
├── application/
│   ├── initialize.py
│   ├── fact_sync.py
│   ├── query.py
│   ├── proposals.py
│   ├── rendering.py
│   ├── recovery.py
│   └── services.py       # 端口/接口定义与用例组合
├── infrastructure/
│   ├── python/           # 文件发现、摘要、AST、关系解析
│   ├── persistence/      # JSON、History、SQLite、锁、事务恢复
│   ├── rendering/        # Markdown 确定性投影
│   └── clock.py
├── interfaces/
│   ├── cli/              # argparse 命令适配器
│   └── mcp/              # STDIO MCP、profile 和 DTO
└── integrations/
    └── codex/            # install-codex、Skill 和 Analyzer 模板
```

依赖只能向内：

```text
interfaces / integrations
          ↓
      application
          ↓
        domain

infrastructure 实现 application 定义的端口
```

Domain 不读取文件、不执行 SQL、不依赖 MCP、Codex 或 Pydantic。Application 不知道 Codex 对话细节。CLI 与 MCP 只做参数校验、错误映射和输出序列化，不复制业务规则。

## 6. 主要组件职责

### 6.1 Repository Locator

- 从启动工作目录向上寻找最近的 `.git`；
- 一个 MCP 进程固定服务一个 repository root；
- MVP 不支持 multi-root workspace；
- 所有输入路径先转换为仓库相对 POSIX 路径，再执行 `realpath/commonpath` 边界校验；
- 任何逃逸仓库的路径都返回 `PATH_OUTSIDE_REPOSITORY`。

### 6.2 Fact Engine

- 确定 Managed Source Set；
- 规范化文本并计算 SHA-256；
- 使用 `ast` 提取实体和语法事实；
- 解析本地 import、继承等 best-effort 关系；
- 只把可证明的信息标记为确定事实；
- 单文件失败形成 diagnostic，不中止其他文件。

### 6.3 Cognition Graph Engine

- 加载和校验正式图；
- 执行图查询、邻接遍历和 bounded context；
- 维护 Responsibility、Behavior、Capability、Logical Flow 和 Mapping 的约束；
- 不负责产生语义结论。

### 6.4 Proposal Engine

- 验证领域 Patch；
- 绑定 graph revision、source digest 和细粒度源码前置条件；
- 计算 canonical `patch_digest`；
- 在内存应用 Patch 后完整校验；
- 只在收到结构化 `approval_record` 时提交正式认知变化。

### 6.5 Freshness Engine

- 对比 cognition baseline 与当前源码；
- 生成单一有效 ChangeSet；
- 从 Mapping、证据和图结构计算 potentially affected scope；
- 计算每次查询的 Effective Query Freshness；
- 不调用 Agent 判断语义是否真正变化。

### 6.6 Renderer

- 从规范图确定性生成 Markdown；
- 同一正式状态在不同机器生成相同逻辑内容；
- 不读取 LLM 输出，不接受 Markdown 反向修改图。

## 7. 正式数据与缓存

```text
.codecortex/
├── manifest.json
├── config.toml
├── graph.json
├── entity_refs.json
├── source_baseline.json
├── PROJECT.md
├── history/events/*.json
├── views/
└── .cache/
    ├── facts.sqlite3
    ├── repository.lock
    ├── freshness.json
    ├── change_sets/
    ├── pending_proposals/
    └── transactions/
```

### 7.1 正式真相

- `manifest.json`：schema、graph revision、`cognition_initialized`、Digest Profile、Managed Source Set 规则和 cognition baseline；
- `graph.json`：唯一规范认知图；
- `entity_refs.json`：正式图实际引用的稳定 CodeEntity 身份和最后已知地址；
- `source_baseline.json`：与 cognition baseline 对应的受管理文件路径和逐文件内容摘要，用于 cache 删除或跨机器后的精确文件级差异重建；
- `history/events/`：不可变正式事件；
- `PROJECT.md`：用户手工维护的项目背景；
- `views/`：可重建但提交 Git 的人类可读投影。

`cognition_initialized=false` 时 baseline digest 必须为 null，source baseline 文件必须为空；M0 可以为持久化链路测试推进 graph revision，但不能把它标记为真实项目认知。首次 M1a 初始化 apply 在同一正式事务把该字段设为 true 并建立非空 source baseline。字段为 true 后 baseline 不得为空。

### 7.2 查询副本

SQLite 同时索引代码事实和正式认知的查询副本。它不是第二套真相：

- 每次连接先检查 cache schema、parser version、source digest 和 graph revision；
- 与 manifest 不一致时禁止返回混合数据；
- cache 缺失、损坏或版本不兼容时重建；
- 正式写入成功而 cache 刷新失败时，正式状态仍成功，下一次读取前重建。

## 8. 序列化与 ID 规则

所有正式 JSON：

- UTF-8、LF 换行、文件末尾一个换行；
- 对象键按固定 schema 顺序输出，集合按稳定 ID 排序；
- 缩进两个空格，不输出非有限浮点数；
- 时间使用 UTC RFC 3339，例如 `2026-09-02T01:23:45Z`；
- canonical digest 使用无无关空白、键排序后的 UTF-8 JSON 字节计算 SHA-256。

ID 命名空间：

| 类型 | 格式 |
|---|---|
| 认知节点 | `responsibility.<slug>` / `behavior.<slug>` / `capability.<slug>` |
| Flow Step | `<behavior-id>#step.<slug>` |
| CodeEntity | `ent_<ULID>` |
| Edge | `edge_<ULID>` |
| Mapping | `map_<ULID>` |
| 正式证据 | `evid_<ULID>` |
| Proposal | `prop_<ULID>` |
| ChangeSet | `chg_<ULID>` |
| History Event | `evt_<ULID>` |

语义节点 ID 一经进入正式图不可修改；标题和别名可以经 Proposal 修改。Core 验证前缀与对象类型匹配，拒绝命名空间混用。

## 9. 配置

仓库配置使用 `.codecortex/config.toml`。MVP 支持：

```toml
schema_version = 1

[source]
include = ["**/*.py"]
exclude = []
respect_gitignore = true
python_min = "3.9"
python_max = "3.14"

[query]
default_depth = 2
max_nodes = 40
max_entities = 80
max_evidence = 80

[cache]
lock_timeout_seconds = 10
```

规则：

- 不允许绝对路径；
- 不保存 token、认证信息或机器路径；
- 未知字段默认报错，避免拼写错误被静默忽略；
- 所有默认值由版本化 schema 定义；
- 配置变化如果影响 Managed Source Set 或 Digest Profile，必须执行完整 Fact Preflight。

## 10. SQLite 连接与并发原则

- 使用 Python 标准库 `sqlite3`；
- `foreign_keys=ON`、`journal_mode=WAL`、设置 `busy_timeout`；
- 查询在共享仓库锁内使用短生命周期连接和参数绑定；
- 动态列名、排序字段和关系类型只能来自 allowlist；
- Analyzer profile 使用 URI `mode=ro`；
- Main 写入使用 repository lock，再开启 `BEGIN IMMEDIATE`；
- 解析源码在事务外完成，批量落库在短事务内完成；
- 全量 cache 重建先在新数据库完成；取得独占仓库锁、确认所有共享读锁已释放、checkpoint/关闭旧连接后，`quick_check`、外键和计数检查通过的新库才原子替换旧库及其 WAL sidecar；
- 不对可重建 cache 实施复杂迁移，schema 变化直接重建；
- 不允许整库载入；所有列表查询都必须分页或带 limit；
- Application 层提供批量查询，避免 N+1。

## 11. 仓库锁与正式状态事务

### 11.1 锁

- 使用 `fcntl.flock` 实现 POSIX advisory lock；
- 锁文件位于 `.codecortex/.cache/repository.lock`；
- 进程崩溃时 OS 自动释放锁，锁文件残留不代表仍被占用；
- 所有只读查询获取共享锁，所有会改变 cache generation 或正式状态的操作使用独占锁；
- 共享锁保证全量数据库替换时不存在仍持有旧 WAL generation 的读连接；SQLite snapshot 和 manifest revision 仍负责查询内容一致性；
- 超时返回 `LOCK_TIMEOUT`，不得删除锁文件抢锁。

### 11.2 正式事务

正式状态跨多个文件，不能只依赖逐文件 rename。事务步骤：

1. 获取仓库锁；
2. 重新读取 manifest、graph 和源码条件；
3. 在内存生成完整新状态；
4. 写入 `.cache/transactions/<txn_id>/staged/`；
5. 校验所有 staged 文件并计算摘要；
6. 保存事务 journal 和旧文件备份；
7. 依次替换 event、graph、entity refs、source baseline 和 views；
8. 最后替换 manifest，manifest 是 commit marker；
9. 刷新 SQLite 查询副本；
10. 标记完成并清理暂存。

启动恢复：manifest 仍指向旧 revision 时恢复旧文件；manifest 已指向新 revision 时完成清理和 cache 重建；无法证明一致性时返回 `FORMAL_STATE_CORRUPT`，不猜测修复。

## 12. MCP 进程与 Profile

使用一个 MCP server 实现，通过启动参数暴露不同工具：

```text
codecortex mcp --profile main
codecortex mcp --profile analyzer
```

- STDIO 上不得输出日志；日志写 stderr；
- server 启动时从 CWD 固定 repository root；
- Main profile 包含读工具和受控维护/Proposal 工具；
- Analyzer profile 只包含读工具；
- Analyzer 不拥有通用 SQL、文件写入、Proposal 或 baseline 工具；
- `apply_cognitive_proposal` 在 Codex MCP 配置中使用 host approval prompt；
- MCP `required=false`，失败不阻塞普通 Codex。

MCP DTO 使用显式版本字段。内部领域对象不得直接暴露，避免后续内部重构破坏工具协议。

## 13. 错误模型

所有 Core 错误包含：

```json
{
  "code": "PROPOSAL_STALE",
  "message": "Proposal source preconditions no longer match",
  "retryable": false,
  "details": {},
  "suggested_action": "Revise or recreate the proposal"
}
```

稳定错误至少包括：

- `NOT_INITIALIZED`
- `PATH_OUTSIDE_REPOSITORY`
- `LOCK_TIMEOUT`
- `CACHE_REBUILD_REQUIRED`
- `SOURCE_PARSE_PARTIAL`
- `ANALYSIS_REPORT_INVALID`
- `PROPOSAL_STALE`
- `GRAPH_REVISION_CONFLICT`
- `APPROVAL_REQUIRED`
- `APPROVAL_MISMATCH`
- `FORMAL_STATE_CORRUPT`
- `UNSUPPORTED_SCHEMA`

接口适配器只能把领域错误翻译为 CLI exit code 或 MCP error，不得吞掉错误或擅自继续。

## 14. 日志与隐私

- 默认结构化日志写 stderr；
- 每次操作带 `operation_id`，写操作额外带 `transaction_id`；
- 记录耗时、文件/实体数量、revision 和错误码；
- 不记录完整源码、用户自然语言对话、token 或认证信息；
- 路径只记录仓库相对路径；
- `.codecortex/` 不保存聊天历史；
- 自动测试的 Codex JSONL trace 存在测试产物目录，不进入正式认知状态。

## 15. 安全边界

- 所有文件访问限制在 resolved repository root；
- 符号链接逃逸必须被拒绝；
- JSON/TOML 大小、集合长度、查询 limit 和 MCP payload 都有上限；
- 不执行被分析仓库代码，不导入目标模块；
- AST 解析只读取文本；
- SQL 全部参数化；
- Analyzer MCP 从工具注册层移除写工具，不只依赖 prompt 禁止；
- Analyzer 自定义 Agent 默认请求 `sandbox_mode="read-only"`，但父会话的实时 sandbox/approval 覆盖可能被 Codex 重新应用；CodeCortex 能硬性保证的是 Analyzer MCP 没有写工具、Core 拒绝 Analyzer profile 的写请求，不能宣称独立撤销 Codex 原生文件写权限；
- Analyzer 开始前和 Main 消费报告前都校验 repository source digest；期间源码发生任何变化时报告作废，不允许据此创建或应用 Proposal；
- Core 可以验证结构化 approval record 与 Patch 一致，但不能证明自然语言批准确实来自用户；这属于 Main Codex 信任边界。

## 16. 跨机器恢复

Git 提交：manifest、config、graph、entity refs、source baseline、PROJECT、history 和 views。Git 忽略整个 `.codecortex/.cache/`。

clone 后：

1. 用户在新机器安装 CodeCortex 并运行 `install-codex`；
2. Core 校验正式 JSON、source baseline、History 引用和 View 可重建性；
3. 从源码和 entity refs 重建 SQLite；
4. 用版本化 Source Digest Profile 计算当前摘要；
5. 与 cognition baseline 相同则 fresh；不同则用 `source_baseline.json` 与当前逐文件摘要生成精确的 added/modified/deleted 文件集合和 ChangeSet；
6. 不因 cache 缺失假设认知仍然 fresh。

CRLF/LF、路径分隔符和文件遍历顺序通过 Source Digest Profile 规范化。原生 Windows 不在 MVP 支持范围，但正式文件不写入 POSIX 机器绝对路径。

## 17. 对 deepwiki-open 的借鉴边界

可以借鉴：

- 仓库路径封装与 token 脱敏思路；
- 文件遍历、过滤和 POSIX 路径规范化模式；
- Pydantic 结构化输出和源码引用模型；
- `realpath/commonpath` 防目录穿越；
- 源码片段定位到真实行号的 grounding 思路。

不引入：

- FastAPI、前端和后台任务系统；
- RAG、Embedding、向量数据库；
- AdalFlow 或模型 provider 层；
- 远程仓库 clone 服务；
- Wiki 页面缓存和大段 LLM 内容持久化。

任何实际复制代码前必须单独核对许可证和依赖，不因“参考实现”自动复制。

## 18. 架构验收条件

- Domain 可以在不启动 MCP、Codex 或网络的情况下测试；
- CLI 与两个 MCP profile 调用同一组 Application Services；
- 删除 SQLite 后所有正式认知和 baseline 文件级差异仍可恢复；
- 未经 apply 的操作不能改变 graph revision；
- 两个 MCP writer 并发时只有满足最新 preconditions 的操作成功；
- Analyzer 工具列表中不存在任何正式或 cache 写能力；
- Analyzer 报告跨越源码变化时必须被拒绝；
- CodeCortex 启动失败不阻塞 Native Codex；
- 所有跨文件正式写入都可恢复到完整旧 revision 或完整新 revision。
