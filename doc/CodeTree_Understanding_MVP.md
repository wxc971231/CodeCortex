# CodeCortex 项目理解系统 MVP

## 设计规格 v0.6.1

**状态：** Implementation Baseline 已冻结

**定位：** 面向 Codex 的持久化、可校正项目认知层（Persistent Cognitive Layer）

**首版环境：** VS Code + 官方 Codex

**首版语言：** Python

**首版平台：** Linux、macOS、WSL；不原生支持 Windows

**开发环境：** Conda + Python 3.14；被分析仓库语法范围 Python 3.9～3.14

**首版形态：** CodeCortex Core + MCP + CodeCortex Skill + CodeCortex Analyzer

## 1. 目标

CodeCortex MVP 首先验证一个独立问题：

> 能否基于可靠代码事实建立一张准确、可校正、可渐进展开的项目认知图，帮助用户和 Codex 快速理解一个中型 Python 仓库。

CodeCortex 的核心定位不是替代 Codex 的代码搜索、源码读取和推理能力，而是为 Codex 提供一层可按需调用、可跨会话、跨机器复用的持久化项目认知状态。认知图负责组织和路由项目理解，真实源码始终作为最终事实来源。

项目认知图不是源码目录、AST 可视化或一次性生成的 Wiki。它以软件职责、行为和共享能力为认知坐标，并将这些语义节点映射到真实代码实体。

第一版主要服务以下场景：

- 快速获得陌生项目的职责全貌；
- 从 Responsibility 下钻到 Behavior、Logical Flow 和 Capability；
- 从认知节点定位到真实 Module、Class、Method 和 Function；
- 从项目、认知节点或源码实体发起有证据的问题讨论；
- 在认知图未覆盖问题时保留完整的原生 Codex 搜索和推理能力；
- 由用户修正 Analyzer 或 Main Codex 产生的错误认知；
- 将经用户批准的项目认知随 Git 跨机器携带。

## 2. MVP 边界

### 2.1 包含

- Python 仓库的确定性 AST 索引；
- Responsibility、Behavior 和 Capability 三类语义节点；
- Behavior 的 Logical Flow；
- 语义节点到 CodeEntity 的 Implementation Mapping；
- L0/L1 优先、L2/L3 按需生成的渐进构建；
- Codex 显式触发的初始化、查询、局部展开、认知同步和全局重新分析；
- 基于认知图的问题讨论、连续追问和原生 Codex fallback；
- 每次 CodeCortex 工作前的源码事实收敛和查询级影响范围判断；
- 专用 CodeCortex Analyzer，以及由只读 MCP profile 强制执行的正式状态写隔离；
- Proposal、讨论、审批和应用两阶段认知写入；
- 结构化规范数据与自动生成的 Markdown Views；
- 全局工具安装与仓库内认知状态；
- Git clone 后自动恢复本地索引和实体解析。

### 2.2 不包含

- 编码前需求—架构适配治理；
- 普通 Codex 任务期间的 CodeCortex 监听或任务末同步；
- CodeCortex Core 活跃期间的文件系统 watcher；
- 通用 Codex Task Start/End 治理；
- Workspace Snapshot；
- 自动设计张力检测；
- 自动重构；
- 独立模型 API Runtime；
- 自建聊天界面；
- VS Code 图形化认知树视图；
- 源码 Embedding、Vector Database 和传统 RAG；
- 完整数据流分析引擎；
- 精确跨文件调用图；
- 动态运行时调用追踪；
- 多语言代码分析；
- 大型 monorepo 的性能承诺。

数据流分析被视为重要的后续确定性能力，而不是不需要的功能。MVP 中的 Logical Flow 先由 Analyzer 基于结构事实和 Targeted Source Read 进行语义归纳。

## 3. 产品形态

CodeCortex 是 Codex-first 的独立 Headless Product，也是 Codex 可按需调用的持久化项目认知层。它在逻辑上连接 Codex 与源码，但不代理或拦截 Codex 的原生源码访问；CodeCortex Skill 是交互入口，MCP 与 Core 承载确定性系统能力。CodeCortex 不是纯 Skill，也不在 MVP 中运行第二套独立 Agent Runtime。

```text
User
  ↓ 显式触发 $codecortex
Main Codex
  ├─ 用户交流和最终语义判断
  ├─ Proposal 管理
  ├─ 用户批准后的认知写入
  └─ 大范围分析时派生 CodeCortex Analyzer
                       │
                       └─ 默认只读分析
  │
  └──────────── MCP ────────────┐
                                ▼
                       CodeCortex Core
                    ├─ Python 代码事实
                    ├─ Freshness / ChangeSet
                    ├─ 项目认知图
                    ├─ CodeEntity 映射
                    ├─ Proposal / Revision
                    ├─ 校验和持久化
                    └─ Markdown 投影
```

### 3.1 组件职责

**Main Codex**

- 理解用户意图；
- 决定当前问题需要读取哪些认知节点和代码范围；
- 判断是否委派 Analyzer；
- 基于认知图、CodeEntity 和源码证据回答用户问题；
- 综合 Analyzer 建议，而不是原样接受；
- 向用户解释认知修改的原因和影响；
- 与用户讨论并修订 Proposal；
- 仅在用户明确批准后提交正式认知写入。

**CodeCortex Analyzer**

- 执行全仓或大范围 read-heavy 分析；
- 通过只读 MCP 获取代码事实和已有认知；
- 使用自身代码搜索能力进行 Targeted Source Read；
- 将本次选择提交的候选数据压缩为结构化且大小受限的 `AnalysisReport` 返回 Main Codex；
- 不修改业务源码；
- 不创建或应用正式认知修改。

CodeCortex 能硬性保证 Analyzer MCP 不暴露写工具、Core 拒绝 Analyzer profile 写正式状态。自定义 Agent 默认请求 read-only sandbox，但父 Codex 会话的实时 sandbox/approval 覆盖可能被重新应用给 subagent，因此产品不能宣称独立撤销父会话已经授予的原生文件权限。Analyzer 仍被明确指示不得写仓库；分析开始与 Main 消费报告时必须核对源码摘要，期间源码有变化则报告作废。

**CodeCortex Core**

- 提取和维护确定性 Python 代码事实；
- 在 CodeCortex 入口执行源码对账、事实收敛和确定性的候选认知影响范围计算；
- 保存源码、事实和认知三层 Freshness 状态；
- 保存和查询项目认知图；
- 校验语义节点、边、CodeEntity 和 Source Reference；
- 管理认知 revision 和 Proposal 状态；
- 保存和校验正式认知变更的不可变 History Event；
- 原子应用已批准的 Proposal；
- 生成确定性的 Markdown Views；
- 重建可丢弃的本地缓存；
- 不进行开放式 LLM 语义推理。

**CodeCortex Skill**

- 定义显式触发语法；
- 定义初始化、查询、展开、认知同步和重新分析流程；
- 定义认知图讨论、证据标准和 fallback 流程；
- 定义 Analyzer 委派规则；
- 强制执行语义认知修改的用户审批边界；
- 控制 Main Codex 和 Analyzer 可使用的 MCP 工具集。

## 4. 显式触发

MVP 不自动介入普通 Codex 任务。只有用户显式提到 `$codecortex` 时才启动 CodeCortex 工作流，例如：

```text
使用 $codecortex 初始化这个项目
使用 $codecortex 介绍这个项目
使用 $codecortex 展开“Wiki Generation”
使用 $codecortex 分析模型调用流程
使用 $codecortex ask “这个方法的输出最终被谁消费？”
使用 $codecortex sync 同步未决认知变化
使用 $codecortex 全局重新分析
```

这样可以独立测量 CodeCortex 对项目理解的收益，也避免为普通 Codex 请求增加固定延迟和上下文开销。一次显式启动 `$codecortex ask` 后，当前 Codex 对话中的连续追问沿用本次讨论语境；用户明显切换话题或明确结束后退出。用户不需要为同一轮讨论的每个追问重复触发 Skill。

### 4.1 普通 Codex 与 CodeCortex 的边界

仓库即使已经初始化 CodeCortex，普通 Codex 编码也不依赖 CodeCortex：

- 不自动启动 Skill 或 Core；
- 不监听文件变化；
- 不更新事实索引或认知图；
- 不在普通编码任务结束时创建认知 Proposal；
- Core 或 MCP 不可用时不影响原生 Codex 工作。

因此，MVP 不依赖 Codex 提供可靠的任务结束生命周期 Hook。两次 CodeCortex 工作之间发生的所有修改，在下一次显式进入 CodeCortex 时统一发现和处理。

### 4.2 CodeCortex 入口 Preflight

除首次初始化外，每次显式进入 `$codecortex` 工作流都必须先执行两层 preflight。第一层 Fact Preflight 同步完成：

```text
读取上次 cognition baseline
  → 计算当前受管理源码摘要
  → 摘要一致：直接进入本次 CodeCortex 工作
  → 摘要不一致：重建或增量更新代码事实
  → 生成聚合 ChangeSet
  → 计算 changed entities 和 potentially affected cognitive scope
  → 立即进入当前问题的范围判断
```

源码摘要基于受管理文件集合、相对路径和内容计算，不以 Git commit 作为唯一依据，因此能够发现未提交修改，也不会被 `.codecortex/` 自身的提交扰动。

所有源码摘要必须使用 manifest 中版本化的统一 Source Digest Profile，例如：

```yaml
source_digest_profile:
  version: 1
  algorithm: sha256
  managed_source_rules_version: 1
  path_separator: "/"
  text_newline: lf
```

该 Profile 同时适用于 cognition baseline、ChangeSet 前后摘要、Proposal `analyzed_source_digest`、`source_preconditions` 和 History Event：

- Managed Source Set 的包含、排除和文本文件判定规则必须版本化且与操作系统无关；
- 相对路径统一使用 `/`，不得按当前文件系统进行大小写折叠；
- 文件集合按规范化相对路径稳定排序后参与集合摘要；
- 按版本化规则认定的文本源码在摘要前将 CRLF 和 CR 规范为 LF；
- 除换行外，不修剪空白、不修改 Unicode、不重新编码或格式化源码；
- 非文本受管理文件按原始字节计算摘要；
- Digest Profile 或 Managed Source Set 规则版本变化时必须执行完整 Fact Preflight，不得直接比较或继承旧 baseline。

Potentially Affected Cognitive Scope 只是 Core 根据 CodeEntity Mapping、认知图关系和确定性/保守依赖闭包计算出的候选集合，不代表认知语义已经发生变化。Core 可以返回 potentially affected nodes/entities 和 scope confidence，但不得返回 `semantic_change`、建议新增语义节点或其他开放式语义判断；实际语义变化只能由 Main Codex 或 Analyzer 判断。

第二层 Semantic Cognition Sync 不作为每次 CodeCortex 查询的阻塞前置条件。Main Codex 将当前问题范围与所有未决 ChangeSet 的影响范围比较：

```text
影响范围完整且与当前问题无交集
  → 正常使用未受影响认知

当前问题涉及受影响节点或实体
  → Source-first degraded mode
  → 按需读取相关源码
  → 范围较大时派生 targeted Analyzer

影响范围 partial / unknown
  → 不宣称相关认知 fresh
  → 对当前问题做保守源码核验
  → 不因不确定性自动启动全仓 Analyzer
```

完整或局部 Semantic Cognition Sync 只在用户显式执行 `$codecortex sync`、当前问题确实需要受影响认知、用户要求更新相关认知，或执行全局 `reinitialize` 时运行。同步确认无认知变化后可以自动推进 baseline，并写入 `reason: no_semantic_change` 的 `cognition_baseline_advanced` Event；确认存在语义变化时才生成聚合 Proposal。

MVP 不实现文件系统 watcher。正确性完全由每次显式进入 CodeCortex 时的摘要对账和 Fact Preflight 保证；只有 M1b 的真实性能数据证明入口扫描不足时，后续版本才考虑 watcher、防抖和并发索引。

### 4.3 三层 Freshness

CodeCortex 分别记录：

- **Source Freshness**：当前受管理源码是否已被发现；
- **Fact Freshness**：AST、符号、关系和 CodeEntity 是否与源码一致；
- **Cognition Freshness**：认知图是否已经对当前源码完成语义影响判断。

每批差异形成一个 `ChangeSet`，至少包含前后源码摘要、涉及文件和实体、来源、影响范围、创建时间和处理状态。影响范围必须包含 `file_diff_completeness`、`entity_diff_completeness: complete | partial`、`affected_nodes`、`affected_entities`、`unmapped_changes` 和 `scope_confidence: complete | partial | unknown`。无法可靠确定来源时使用 `unknown`，不得把修改猜测为 Codex 或用户产生。

- `complete`：全部变化/删除文件已识别并解析成功；每项变化都能通过当前/历史 Implementation Mapping、Evidence 或经 resolver 高置信解析的结构关系连接到已有认知节点，或者被规则化地证明与正式认知无关；没有 unresolved 的相关关系或 `unmapped_changes`，并已完成保守依赖闭包；
- `partial`：只解析了部分变化，或仍存在无法通过确定关系连接到已有认知节点的新增、删除或未映射内容；
- `unknown`：无法可靠界定变化可能影响的认知范围。

删除实体可以使用删除前的 CodeEntity tombstone 和历史 Mapping 计算候选范围。无法连接的新文件、新实体或删除项必须保留在 `unmapped_changes` 中，使 scope 至少为 `partial`；Core 不得为了获得 `complete` 状态而推断它们“与认知图无关”。错误的 `complete` 比保守降级更危险。

cache 连续存在时，SQLite 中独立的 baseline entity snapshot 只随 cognition baseline 推进，因此可完整比较新旧实体。cache 被删除或换机器后，正式 source baseline 仍能恢复完整文件级差异，Core 也能恢复当前实体和 `entity_refs.json` 中正式引用过的旧实体；但未被正式引用的全部旧实体无法重建，因此 `entity_diff_completeness` 必须标为 `partial`。这不自动决定 `scope_confidence`，后者仍须逐项满足上述严格证明条件。

认知状态至少区分：

- `fresh`：事实与认知均已对当前源码完成收敛；
- `pending`：已发现可能的语义变化，等待讨论或审批；
- `stale`：认知图明确落后于当前源码；
- `unresolved`：由于解析或映射歧义无法完成判断。

仓库级 `pending`、`stale` 或 `unresolved` 只用于汇总提示，不意味着整张图都不可使用。每次查询动态计算 Effective Query Freshness：

- 所有未决 ChangeSet 的 scope 均为 `complete`，且当前查询范围与 affected scope 无交集：相关认知仍可正常使用；
- 当前查询与任一 affected scope 相交：进入 Source-first degraded mode；
- 任一可能相关的 ChangeSet 为 `partial` 或 `unknown`：保守核对当前问题所需源码，不把旧认知冒充当前事实。

affected scope 的计算必须包含 CodeEntity 映射、认知图父子关系、共享 Capability 及必要的依赖传递范围。`pending`、`stale` 或 `unresolved` 不阻塞普通 Codex，也不必阻塞 CodeCortex 问答。

Cognition Freshness 是仓库级汇总状态，ChangeSet affected scope 决定查询级可信度。MVP 不为每个节点持久化另一套 Freshness 状态。

## 5. 认知图模型

一个 Git 仓库对应一张认知图。MVP 不设置 Project 节点，图本身保存仓库级元数据，顶层直接由 Responsibility 组成。

### 5.1 语义节点

MVP 仅包含三类语义节点：

- **Responsibility**：项目中的主要职责区域；
- **Behavior**：职责区域向用户或其他系统提供的主要行为；
- **Capability**：可以被一个或多个 Behavior 使用的软件能力。

Logical Flow 是 Behavior 内部的有序步骤，不作为共享顶层节点。每个 Flow Step 可以引用 Capability 或直接映射到 CodeEntity。

### 5.2 关系

MVP 保留以下明确关系：

```text
Responsibility ─contains──────→ Behavior
Behavior       ─uses──────────→ Capability
Behavior       ─implemented_by→ CodeEntity
Capability     ─implemented_by→ CodeEntity
Capability     ─depends_on────→ Capability
FlowStep       ─uses──────────→ Capability
FlowStep       ─implemented_by→ CodeEntity
```

不增加含义模糊的通用 `related_to` 关系。新增关系必须有明确语义、方向和校验规则。

Behavior 和 Flow Step 可以直接映射 CodeEntity，不强迫所有具体实现先抽象成 Capability。这用于避免 Analyzer 为一次性流程步骤制造虚假的共享能力。

### 5.3 节点字段

语义节点至少包含：

```yaml
id: capability.streaming-model-invocation
kind: capability
title: Streaming Model Invocation
aliases:
  - Model Calling
  - 模型调用
summary: 为 Wiki、问答和项目理解功能提供流式模型调用
epistemic_status: inferred
created_by: analyzer
last_modified_by: analyzer
intent: null
observed: 当前源码实际呈现出的实现方式
node_revision: 3
approval:
  approval_event_id: evt_01JXYZ
  approved_by: user
  approved_at: 2026-08-31T12:00:00+08:00
```

`epistemic_status` 只允许：

- `established`：正式项目设计文档明确声明该语义结论，或用户明确确认该语义结论；
- `inferred`：基于间接证据形成了合理的语义归纳；
- `uncertain`：证据不足、相互冲突或存在重要未解问题。

源码能够直接建立 CodeEntity、签名、位置和语法关系等代码事实，但仅从源码归纳出的 Responsibility、Behavior 或 Capability 通常仍为 `inferred`。源码证据充分不等于项目设计语义是直接事实，Analyzer 不得把首次语义归纳自动提升为 `established`。

`created_by` 和 `last_modified_by` 至少区分：

- `analyzer`；
- `main_codex`；
- `user`。

节点创建者、最后修改者、审批历史和 epistemic status 是四个独立概念，不能压缩成单一 `origin`。Proposal 获得审批只表示允许写入，不自动把 `inferred` 或 `uncertain` 提升为 `established`。未来治理阶段如需表达设计健康度，应使用独立的 `design_status`，不得复用 epistemic status。

`intent` 允许为 `null`，且与 `observed` 必须分开。Analyzer 或 Main Codex 可以在 Proposal 中给出 `inferred_intent`，但推断不得冒充项目的正式意图。只有用户明确确认、项目文档直接声明，或推断经过审批后，内容才能写入正式 `intent`。

Behavior 的 Logical Flow 必须显式区分以下 materialization 状态：

- `unmaterialized`：尚未深入分析，不表示不存在流程；
- `materialized`：已经建立正式 Flow Steps；
- `not_applicable`：已经确认该 Behavior 不适合用有序流程表达。

状态及 Flow Step 的新增、顺序和语义变化都属于正式认知修改，必须通过 Proposal 和审批。L3/L4 动态加载不自动改变 materialization 状态。

### 5.4 Progressive Disclosure

用户侧保持以下渐进路径：

```text
L0  Responsibility
      ↓
L1  Behavior
      ↓
L2  Logical Flow / Capability
      ↓
L3  Implementation Structure
      ↓
L4  Source Code
```

L3 不单独持久化为一套节点，而是由语义节点的 `implemented_by` 映射动态投影。L4 始终是真实源码，不复制到认知图。

因此“L3/L4 尚未加载”不等于认知图未覆盖。只要存在相关 Responsibility、Behavior 或 Capability 锚点，查询应先使用认知图缩小范围，再动态加载代码事实和源码。

### 5.5 内部图与用户认知树

CodeCortex 内部保存的是允许共享引用和交叉依赖的认知图；用户看到的是从特定 Responsibility 或 Behavior 出发生成的渐进式树状投影。Capability 可以被多个 Behavior 共享，因此内部数据不能为了界面上的树形展开而复制节点。

`TREE.md`、局部 Views 和未来 UI 都只是同一规范图的投影。树中出现同一 Capability 多次时必须保留相同节点 ID，使用户能够识别“多个行为依赖同一能力”。

## 6. Python 代码事实

### 6.1 解析范围

MVP 使用 Python 标准库 `ast` 提取：

- File / Module；
- Class；
- Function；
- Method；
- Import declaration；
- Declared base expression；
- Docstring；
- Decorator；
- Function signature；
- Source location。

首版代码事实分成两档。确定性语法事实直接来自 AST：

```text
contains
import_declaration
declared_base
```

解析或推断关系包括：

```text
imports
inherits
tested_by
calls
```

`import_declaration` 保存源码中的原始导入声明，`imports` 表示解析到具体本地 Module 后的依赖；`declared_base` 保存 class 定义中的基类表达式，`inherits` 表示解析到具体 CodeEntity 后的继承关系。`tested_by` 依赖测试目录、命名、fixture、import 或调用等证据，不能声明为确定事实。

所有解析或推断关系必须携带 source evidence、confidence 和 resolver/analyzer 版本。函数调用只保存 best-effort 结果；Core 不声称能够精确恢复 Python 动态调用链。

### 6.2 CodeEntity 身份

每个 CodeEntity 同时维护稳定身份和当前源码地址：

```yaml
uid: ent_01JXYZ
address: api.chat._stream:OpenAIChatStreamer.respond_stream
kind: method
relative_path: api/chat/_stream.py
start_line: 198
end_line: 216
signature: "(self, prompt: str) -> AsyncIterator[str]"
fingerprint: "..."
```

- `uid` 用于长期 Implementation Mapping；
- `address` 表示当前 module 和 qualname；
- `relative_path` 必须相对仓库根目录；
- `fingerprint` 辅助识别移动或改名。

重新索引时：

1. address 相同则保留 uid；
2. address 变化但 Git rename、签名和 fingerprint 足以高置信匹配时保留 uid；
3. 无法可靠判断时不自动迁移 Mapping；
4. 原实体标记为 missing，新实体获得新 uid；
5. 与认知图关联的歧义进入 unresolved 状态，等待后续认知 Proposal。

## 7. 数据流扩展位置

MVP 不实现数据流引擎，但关系模型预留来源、证据和置信度：

```yaml
type: data_flow
source_entity: ent_source
target_entity: ent_target
evidence:
  relative_path: api/services/research.py
  start_line: 60
  end_line: 294
confidence: inferred
analyzer: future-dataflow-v1
```

后续演进顺序：

```text
函数内 CFG / def-use
  → 参数、返回值和状态修改
  → 跨函数摘要
  → 跨模块数据流
```

新增数据流事实只增强 Analyzer 证据，不改变 Responsibility、Behavior、Capability 和 Implementation Mapping 的基本模型。

## 8. 初始化与渐进构建

### 8.1 首次初始化

初始化只能由用户在 Codex 中显式触发：

```text
$codecortex init
  ↓
Core 全量建立 Python AST 索引
  ↓
Main Codex 强制派生 CodeCortex Analyzer
  ↓
Analyzer 分区分析中型仓库
  ↓
生成 L0/L1 和少量关键 Capability 建议
  ↓
Main Codex 创建整体初始化 Proposal
  ↓
用户查看 Big Picture、讨论和局部修订
  ↓
用户一次性批准
  ↓
Core 写入正式认知图并生成 Views
```

初始化不要求完整生成所有 L2/L3。未 materialize 的局部可以在用户首次展开时补充。

### 8.2 中型仓库分析策略

目标真实仓库范围为约 100～500 个 Python 文件。Analyzer 不一次读取全部源码，而采用：

```text
Repository Overview
  → 按 package/module 分区
  → 读取分区代码事实和关键源码
  → 汇总 Responsibility / Behavior 候选
  → 分析交叉区域
  → 输出全局 L0/L1 建议
```

### 8.3 局部展开

```text
$codecortex inspect <node>
  ↓
同步完成 Fact Preflight
  ↓
按 inspect 目标判断 Effective Query Freshness
  ↓
读取已有节点和 analysis scope
  ↓
范围小：Main Codex 直接分析
范围大：派生 Analyzer
  ↓
返回解释
  ↓
需要补充正式认知时创建 Proposal
  ↓
用户批准后写入
```

## 9. Analyzer 委派规则

以下场景必须使用 Analyzer：

- 首次初始化；
- 用户要求全局重新分析；
- 用户显式要求使用 Analyzer；
- 用户显式要求 `$codecortex sync` 且累计变化涉及大量文件或多个 Responsibility；
- 当前问题与大范围或不确定 affected scope 相交，且可靠回答需要读取大量文件；
- 分析跨越多个 Responsibility；
- Main Codex 判断需要读取大量文件才能形成可靠结论。

以下场景默认由 Main Codex 处理：

- 展开已有认知节点；
- 查看已有 Implementation Mapping；
- 少量文件的局部补充；
- 与用户讨论 Proposal；
- 审批后提交认知修改。

Core 的 `analysis_scope` 返回文件、模块、Responsibility 和 CodeEntity 数量，供 Main Codex 判断是否委派。MVP 不写死统一文件数阈值；后续根据真实任务数据确定软阈值。

Analyzer 使用独立上下文完成探索，只向 Main Codex 返回结构化结论、主要证据、不确定项和建议认知 Patch，从而减少主讨论线程中的大范围源码探索内容。

## 10. 全局重新分析

全局重新分析不是破坏性清空：

```text
$codecortex reinitialize
  ↓
Analyzer 重新分析整个当前仓库
  ↓
同时读取已有认知和用户确认内容
  ↓
生成全局替换 Proposal
  ↓
展示新增、删除、移动、合并和冲突
  ↓
用户讨论和批准
  ↓
Core 才更新正式图
```

已有正式 `intent`、用户确认或用户修正的内容不会被静默覆盖。代码与用户确认意图冲突时，Proposal 必须明确展示冲突和对应审批历史。

真正清空认知图属于单独的破坏性操作，不进入普通 `reinitialize`，也不属于 MVP 的必要功能。

## 11. Proposal 与审批

### 11.1 状态机

```text
DRAFT → PROPOSED → APPROVED → APPLIED
                  ↘ REJECTED
                  ↘ REVISED → PROPOSED
                  ↘ STALE → REVISED → PROPOSED
```

### 11.2 Proposal 内容

每个 Proposal 至少包含：

- Proposal ID，使用 `prop_` 前缀；
- base graph revision；
- 关联的 ChangeSet ID；
- `analyzed_source_digest`，记录分析时的全仓受管理源码摘要；
- `source_preconditions`，记录本 Proposal 实际依赖的相对路径、内容摘要和必要的 CodeEntity fingerprint；
- 修改原因；
- 修改前后结构化 Diff；
- 受影响节点；
- Implementation Mapping 变化；
- 主要源码证据；
- Analyzer/Main Codex 的不确定项；
- 用户讨论后的修订记录；
- 最终审批来源和时间。

### 11.3 审批边界

可自动维护：

- AST 代码事实；
- 文件和行号位置；
- 明确不改变语义的高置信 CodeEntity 地址更新；
- Freshness 状态、ChangeSet 和无语义变化的 cognition baseline；
- 本地缓存重建。

必须审批：

- Responsibility、Behavior、Capability 的新增、删除和移动；
- Logical Flow 变化；
- Capability 合并或拆分；
- 语义 Implementation Mapping；
- 认知节点 epistemic status 变化；
- 全局重新分析结果。

MVP 的用户批准通过 Main Codex 对话完成，由 Skill 强制 Main Codex 只有在收到明确批准后才能调用 apply。`apply_cognitive_proposal` 必须携带结构化 `approval_record`，至少包含 Proposal ID、批准者、批准时间和批准时的 Patch 摘要。

Core 能验证 Proposal、approval record、base graph revision、source preconditions 和 Patch 内容的一致性，但不能独立证明一条自然语言确认确实来自用户。Main Codex 是否如实把用户确认转换为 `approval_record`，属于 Codex-first MVP 的明确信任边界；规格和测试不得宣称 Core 能独立鉴别真实用户批准。

审批按 ChangeSet 聚合，不按文件、节点或每次保存分别提出。实现细节调整、重命名、测试修改和不改变职责/行为/能力的局部修复应自动完成事实收敛，不创建 Proposal。

普通语义变化先聚合为 `pending Proposal`，不按文件变化立即打断用户。每次显式进入 `$codecortex` 工作流至多汇总提醒一次；一次 `$codecortex ask` 启动后的连续追问属于同一 invocation，不重复提醒。下一次显式 `$codecortex` invocation 可以再次汇总。该规则由 Skill/Main Codex 执行，不要求 Core 识别 Codex thread 或会话生命周期。

删除核心能力、职责大范围迁移等高影响变化可以在当前 invocation 的自然检查点及时提醒。用户选择暂缓后，本次 invocation 不重复请求审批。未审批认知变化不会修改正式图，也不阻塞普通 Codex 工作。

拒绝某个认知 Patch 不等于自动忽略源码变化。Main Codex 应根据用户意见修订或重新分析；只有用户明确确认“正式认知仍然有效，无需修改”时，Core 才能在不修改 graph 的情况下推进 cognition baseline，并写入 `reason: user_accepted` 的 `cognition_baseline_advanced` Event。

### 11.4 过期检查与原子性

- Proposal 绑定 base graph revision；
- base revision 变化后旧 Proposal 不可应用；
- apply 前必须先同步最新确定性事实；
- 当前 repository source digest 必须与 `analyzed_source_digest` 完全一致；任一 Managed Source Set 变化都使 Proposal 进入 `STALE`，必须重新分析或 revise；
- `source_preconditions` 的路径摘要和 CodeEntity fingerprint 仍需逐项复核，防止实现或摘要算法错误绕过约束；
- MVP 不实现“证明后续变化无关后继续 apply”的局部 rebase 优化，以保持审批对象、正式 source baseline 和真实源码严格对应；
- Core 先在内存中应用 Patch 并完整校验；
- APPLIED Proposal 的不可变 History Event 与 graph、entity refs、source baseline 和 Views 一起通过临时文件完成一致性写入；
- 任一步失败都保持上一正式 revision 不变。

## 12. 持久化

### 12.1 项目目录

```text
.codecortex/
├── manifest.json
├── config.toml
├── graph.json
├── entity_refs.json
├── source_baseline.json
├── PROJECT.md
├── history/
│   └── events/
├── views/
│   ├── TREE.md
│   ├── responsibilities/
│   ├── behaviors/
│   └── capabilities/
└── .cache/
    ├── facts.sqlite3
    ├── repository.lock
    ├── freshness.json
    ├── change_sets/
    ├── pending_proposals/
    └── transactions/
```

### 12.2 真相来源

- `graph.json` 是语义认知图的规范数据；
- `manifest.json` 保存 schema、受管理源码规则，以及与当前 graph revision 对应的 cognition baseline 摘要；
- `entity_refs.json` 保存认知图实际引用的稳定 CodeEntity 身份；
- `source_baseline.json` 保存与正式 cognition baseline 对应的受管理文件路径和逐文件内容摘要，不保存源码或 AST；
- `PROJECT.md` 保存用户可手工维护的项目背景和目标；
- `history/events/` 保存已经影响正式状态的不可变审计事件；
- `views/` 是从 graph 确定性生成的人类可读投影；
- `.cache/` 是可删除、可重建的机器状态。

`facts.sqlite3` 保存代码事实和正式认知的本机查询副本，用于按路径、实体、关系、认知节点和 Mapping 高效索引。它不是第二套真相：每次查询必须核对 cache schema、parser version、当前源码摘要和 graph revision；任一不匹配时禁止返回混合数据并重建 cache。正式状态以 Git 中的 graph、entity refs、source baseline、manifest 和 History 为准。

`freshness.json`、当前事实摘要和未决 ChangeSet 属于可重建状态。删除缓存后，Core 必须从已提交 cognition baseline、`source_baseline.json` 和当前源码重新计算，而不是默认认知图仍然 fresh。source baseline 与 manifest baseline 必须在同一正式事务推进；两者摘要不一致属于正式状态损坏。

`source_baseline.json` 的最小规范为：

```yaml
schema_version: 1
digest_profile_version: 1
managed_source_set_version: 1
repository_source_digest: "sha256:..."
files:
  - relative_path: src/codecortex/application/query.py
    content_digest: "sha256:..."
```

文件按规范化相对路径稳定排序且路径唯一。普通 Fact Sync 只更新 cache，不能修改 source baseline；只有初始化 apply、认知 Proposal apply 或 cognition baseline advance 的正式事务才能整体替换它。

M0 revision 0 是唯一空基线：manifest cognition baseline 与 `repository_source_digest` 同时为 null，`files=[]`；首次真实初始化 apply 后不再允许 null。

MVP 只持久化两类 History Event：

- `cognitive_proposal_applied`：使用独立的 `evt_` Event ID，并保存原 `prop_` Proposal ID、最终 Proposal 快照、结构化 Diff、修改原因、源码证据摘要、source preconditions、change set summary 和 approval record；
- `cognition_baseline_advanced`：保存 cognition baseline 的前后源码摘要、对应 graph revision、决定来源和 `reason: no_semantic_change | user_accepted`。

`no_semantic_change` 表示 Main Codex 综合自身或 Analyzer 证据后确认无需改图，可以自动推进 baseline，不需要 approval record。`user_accepted` 表示用户明确确认正式认知仍然有效，必须保存 approval record。两种原因都必须生成正式 Event，并与 manifest 中的 baseline 更新形成同一个可恢复的一致性边界。

Applied Event 可以保留临时 `change_set_id` 作为来源标识，但不能依赖 `.codecortex/.cache/change_sets/` 才能解释。Event 必须嵌入自包含的 `change_set_summary`，至少包括：

```yaml
before_source_digest: "..."
after_source_digest: "..."
changed_files: []
changed_entities: []
affected_nodes: []
scope_confidence: complete
unmapped_changes: []
```

`change_set_summary` 是正式审计快照，不要求保存完整临时 ChangeSet 或分析中间产物。

`DRAFT`、`PROPOSED`、`REVISED` 和 `STALE` 等非正式 Proposal 状态只保存在 `.codecortex/.cache/pending_proposals/`，可以在删除缓存或跨机器后丢失。`REJECTED` 不进入正式 History，可以立即删除或仅作为本机临时诊断保留。

Analyzer 不直接写 SQLite，也不创建独立 Analysis Result 生命周期。它通过 Codex subagent 返回压缩的 `AnalysisReport`；Main Codex 收到后立即创建或修订 pending Proposal。Proposal 是唯一持久化的临时语义工作状态。“完整报告”只指它包含本次选择提交的全部候选数据，不代表完整覆盖仓库。

History Event ID、Proposal ID 和 ChangeSet ID 使用独立命名空间：`evt_`、`prop_` 和 `chg_`。正式节点中的 `approval.approval_event_id` 指向 `cognitive_proposal_applied` Event，Event 内再保存 `proposal_id`，形成 `Node → History Event → Proposal Snapshot`。Core 校验时必须拒绝悬空 Event 引用或 ID 类型混用。

`cognitive_proposal_applied` 的 `event_id` 由 Core 在 apply 事务中分配，并作为确定性 provenance metadata 写入受影响节点的 `approval_event_id`。用户批准的 Patch 摘要覆盖语义修改；Core 自动附加的 Event ID、批准者和批准时间属于受校验的操作元数据，不改变获批语义。apply 返回新的 Event ID 和 graph revision。

History Event 一经写入不可修改。apply 成功时，最终 Proposal Event、graph revision、entity refs、source baseline、Views 和 manifest 必须形成同一个可恢复的一致性边界；发生中断时不得暴露只更新一部分的正式状态。

MVP 不支持用户直接编辑 `graph.json` 或生成的 Markdown Views。用户修正统一通过 Main Codex、Proposal 和 Core 完成。

### 12.3 Git 策略

默认提交：

- `manifest.json`；
- `config.toml`；
- `graph.json`；
- `entity_refs.json`；
- `source_baseline.json`；
- `PROJECT.md`；
- `history/`；
- `views/`。

默认忽略 `.codecortex/.cache/`。

所有源码引用必须使用仓库相对路径，禁止把机器绝对路径写入可提交状态。

## 13. 跨机器恢复

安装 CodeCortex 的另一台机器在 Git clone 后不需要重新初始化：

```text
git clone
  ↓
首次显式使用 $codecortex
  ↓
Core 读取 manifest 和 graph
  ↓
读取 source baseline、history 并校验 graph 中的 approval 引用
  ↓
发现本地缓存缺失
  ↓
自动重建 Python AST 缓存
  ↓
按 address / fingerprint 解析 entity_refs
  ↓
用 source baseline 对比当前逐文件源码摘要
  ↓
一致：validate_graph 后进入正常查询
不一致：执行 Fact Preflight，Semantic Cognition Sync 按查询范围延迟执行
```

缓存重建：

- 不调用 Analyzer；
- 不生成新语义认知；
- 不创建认知 Proposal；
- 不改变 graph revision。

缓存重建完成后若源码摘要与 cognition baseline 不一致，Core 用正式 source baseline 精确恢复 added/modified/deleted 文件；唯一同摘要的删除/新增对才可标为 rename，否则保留为删除加新增。实体历史仅能从正式 entity refs 完整恢复，因此必要时标记 `entity_diff_completeness=partial`。Core 随后计算 potentially affected scope，不阻塞当前无关查询。后续 Semantic Cognition Sync 可以按范围调用 Main Codex 或 Analyzer，并遵守标准 Proposal 审批规则。换言之，“恢复缓存”是确定性的，“判断认知是否仍然成立”是独立且按需执行的语义步骤。

实体无法解析时标记为 stale/unresolved，并向 Main Codex 报告；Core 不根据猜测静默改写已提交认知。

## 14. MCP 接口

### 14.1 Main Codex 与 Analyzer 共用的只读工具

```text
repository_overview
repository_facts(scope, cursor, limit)
cognitive_freshness
pending_changes
effective_query_freshness(node_ids?, entity_ids?)
history_event(event_id)
cognitive_graph
inspect_node(node_id, depth)
analysis_scope(scope)
search_cognitive_graph(query, kinds, limit)
resolve_entity_context(entity_uid?, path?, address?, relation_types?, limit?)
get_discussion_context(node_ids?, entity_ids?, depth, max_nodes, max_entities, max_evidence)
validate_graph
```

### 14.2 仅 Main Codex 可用的维护与修改工具

```text
initialize_repository
sync_repository_facts
create_cognitive_proposal
revise_cognitive_proposal
cognitive_proposal(proposal_id)
apply_cognitive_proposal(proposal_id, approval_record) -> event_id, graph_revision
advance_cognition_baseline(change_set_id, reason, decision_record, approval_record?)
```

`initialize_repository` 幂等创建 revision 0 技术骨架，不产生项目理解。`sync_repository_facts` 只更新确定性事实、Freshness 和 ChangeSet，不修改正式认知图，因此不需要用户审批。Proposal 工具遵守第 11 节的语义写入边界。`advance_cognition_baseline` 只在语义同步确认无需改图时使用：`no_semantic_change` 不需要用户审批，`user_accepted` 必须携带 approval record。

Analyzer 的 MCP 配置中不注册修改工具。权限隔离不能只依赖提示词。

Core 不提供 `grep_code`、`read_file` 或通用 Shell MCP；进入源码后，Main Codex 和 Analyzer 使用 Codex 原生搜索与读取能力。

## 15. CLI 与安装

### 15.1 安装模型

```text
全局用户环境
├── CodeCortex Core
├── codecortex CLI
├── MCP Server
├── CodeCortex Skill
└── Analyzer 配置

每个仓库
└── .codecortex/
```

示例安装：

```bash
pipx install codecortex
codecortex install-codex
```

示例暂统一使用 `codecortex` 作为包名、CLI 名和 Skill 触发名；正式发布前仍需核查命名可用性。发布渠道不属于本设计的接口承诺。

### 15.2 CLI 范围

```bash
codecortex install-codex [--dry-run] [--force]
codecortex doctor [--json]
codecortex validate [--json]
codecortex mcp --profile main
codecortex mcp --profile analyzer
```

CLI 只承担安装、诊断、校验和 MCP 启动。面向产品的事实同步、认知查看和语义操作通过 `$codecortex` 与 MCP 完成，避免 CLI 和 Skill 形成两套工作流。

MVP 不提供语义含糊的 CLI `codecortex init`。完整语义初始化只能通过 Codex 中显式调用 `$codecortex init` 完成。

开发使用 Conda 管理 Python 3.14 环境，并通过 `pyproject.toml` + Hatchling 打包；普通用户首选 pipx 安装，不要求安装 Conda。

## 16. 基于认知图的问题讨论

### 16.1 目标与边界

CodeCortex 复用当前 Codex 对话提供类似 Repository Wiki 的项目讨论能力，不建设独立聊天界面，也不把问答记录保存为第二套项目状态。

讨论支持从以下任意起点发起：

- 整个仓库；
- Responsibility、Behavior 或 Capability；
- 文件、Class、Method 或 Function；
- 没有明确起点的任意自然语言问题。

当前多轮讨论由 Codex 会话维护。普通问答不修改认知图；只有用户要求将讨论结论形成认知修改时，Main Codex 才创建 Proposal，并继续遵守已有审批流程。

### 16.2 检索策略

认知图是优先路由层，不是唯一知识来源：

```text
用户问题
  ↓
同步完成 Fact Preflight
  ↓
Main Codex 判断问题范围和可能起点
  ↓
搜索相关认知节点或反向解析 CodeEntity
  ↓
计算候选范围的 Effective Query Freshness
  ↓
获取预算受限的认知子图和 Implementation Mapping
  ↓
按认知覆盖情况路由
  ├─ 认知充分：Graph-guided Targeted Search
  ├─ L3/L4 未加载：从认知锚点动态加载事实和源码
  ├─ Behavior unmaterialized：询问是否展开，或 transient 回答
  └─ 没有任何语义锚点：Native Codex Search
  ↓
必要时派生 Analyzer（只读 MCP profile）
  ↓
Main Codex 综合证据并回答
```

Core 的 `search_cognitive_graph` 只对节点标题、摘要、别名和结构化字段进行确定性候选召回，不替代 Main Codex 做最终语义判断。`get_discussion_context` 通过 `depth`、`max_nodes` 和 `max_entities` 限制返回规模，超出范围时返回截断标记，由 Main Codex 决定继续局部查询还是派生 Analyzer。

从源码实体发起问题时，`resolve_entity_context` 按 CodeEntity uid、address 或路径反向查找对应 Capability、Behavior 和 Responsibility。若实体没有 Implementation Mapping，Main Codex 仍可以直接读取源码回答，并明确当前认知图尚未覆盖该实体。

L3/L4 动态加载属于 graph-guided retrieval，不视为 fallback。只有找不到 Responsibility、Behavior 或 Capability 语义锚点时，才进入真正的 Native Codex fallback。

### 16.3 Fallback 保证

讨论链路分为：

```text
认知图完整覆盖
  → Graph-guided Targeted Search

存在认知锚点但 L3/L4 未加载
  → Graph-guided Dynamic Facts / Source Load

相关 Behavior 尚未 materialize
  → 用户选择完整展开并形成 Proposal
    或使用已有认知 + 代码事实 + 源码 transient 回答

没有语义锚点或 MCP 不可用
  → Native Codex Search
```

完整展开的第一次用户同意只是允许进行分析，不等于批准尚未生成的认知 Patch。Analyzer/Main 完成分析后仍须展示 Proposal 并取得正式批准。用户拒绝展开后，本次工作流不重复询问同一 Behavior。

以下规则属于硬性运行机制约束：

- Skill 不得把源码读取限制在 Implementation Mapping 指向的文件；
- 图中没有某个能力不表示源码中不存在；
- 当前问题与 affected scope 相交或影响范围无法确定时，不得只依赖图中摘要作答；
- 图失效、节点缺失或 MCP 查询失败时必须恢复原生 Codex 搜索；
- fallback 和 transient 回答都不自动补图，只提示当前认知覆盖缺口；
- 需要补充认知时必须显式进入 Proposal 和审批流程。

“图外问题不因 CodeCortex 发生系统性退化”属于质量验收目标，而不是逐问题可证明的硬保证。固定 Benchmark 必须预先定义评价规则和 non-regression threshold；Codex + CodeCortex 在图外问题上的总体表现不得相对 Native Codex 出现系统性或超过阈值的退化。

### 16.4 Analyzer 路由

以下问题使用 Analyzer：

- 跨多个 Responsibility；
- 需要读取大量文件；
- 用户要求全面或全局分析；
- Main Codex 判断大范围探索会明显占用主讨论上下文。

单节点解释、已有映射清楚、少量源码即可回答的问题由 Main Codex 直接处理。Analyzer 返回一份压缩且有大小上限的结构化 `AnalysisReport`，包括候选认知数据、证据、覆盖范围和不确定项；它不直接写数据库。默认上限为序列化 512 KiB、节点 300、边 1000、Mapping 2000、Evidence 2000、单条描述 240 Unicode code points，且总字节上限优先。超限时提高语义粒度并明确列出未覆盖区域。Main Codex 接收报告后立即创建或修订 Proposal，并负责最终回答。

### 16.5 回答证据标准

回答应根据问题复杂度区分：

- **已确认认知**：来自认知节点和 graph revision；
- **代码事实**：来自 CodeEntity、相对文件路径和源码位置；
- **Agent 推断**：明确标记为推断，并说明证据和不确定性。

简单问题可以只提供结论和关键源码引用。架构或跨模块问题应展示必要的认知路径、关键源码依据和当前不确定项。关键结论必须可追溯，但不强制所有回答使用冗长固定模板。

### 16.6 对话状态

- `$codecortex ask` 显式启动当前讨论；
- 同一主题的连续追问自动沿用当前认知和源码上下文；
- 明显切换话题或用户要求结束后退出当前讨论语境；
- `.codecortex/` 不保存完整聊天记录；
- 新会话通过正式认知图恢复项目理解，不回放旧聊天；
- 只有用户批准的认知结论进入 `graph.json`。

## 17. 错误处理

- Analyzer 失败：不创建 Proposal，不改变正式图；Main Codex 返回可重试错误。
- Analyzer 输出结构无效：Main Codex 不提交，必要时要求 Analyzer 修正一次。
- AST 单文件解析失败：记录文件级诊断；若影响初始化覆盖范围，在 Proposal 前向用户说明。
- Proposal 过期：拒绝 apply，要求重新读取当前 graph 和相关 CodeEntity。
- graph 中的 approval 引用找不到对应 History Event：校验失败，保留文件原状并报告悬空引用，不静默删除审批信息。
- History Event 写入失败：graph、manifest、entity refs、source baseline 和 Views 均不得推进正式 revision。
- entity ref 无法解析：保留认知节点，标记映射 unresolved，不删除用户认知。
- Markdown View 生成失败：graph 不推进 revision；修复生成问题后整体重试。
- 跨机器 schema 不兼容：报告所需最低 Core 版本，不隐式降级或丢字段。
- 认知图查询失败：向用户说明 fallback，并继续使用原生 Codex 搜索。
- preflight 事实收敛失败：不得把 Cognition Freshness 标记为 fresh；报告失败范围，并仅对能够直接核验的源码继续降级回答。
- 认知影响分析失败：保留 ChangeSet 和旧 cognition baseline，不修改正式图；用户可以重试或继续使用原生 Codex。
- 问题无法从当前证据可靠回答：明确列出不确定项，不用认知节点摘要冒充源码事实。

## 18. 测试策略

### 18.1 确定性单元测试

- Python AST 实体和源码位置；
- SQLite cache schema、必要索引、外键、批量查询和 graph/source revision 校验；
- SQLite 增量更新与全量重建结果等价，cache 损坏时原子替换；
- 目标实体改变或删除后，未变化来源文件的 incoming relations 重新解析，relation identity 不依赖解析目标；
- contains、import declaration 和 declared base 的确定性提取；
- imports、inherits、tested_by 和 calls 的证据、置信度与 resolver 版本；
- CodeEntity address、fingerprint 和 uid 保留规则；
- graph schema 和边约束；
- node revision 与 graph/base graph revision 的字段区分；
- epistemic status 枚举及其与 approval 的独立性；
- Flow Step 顺序与引用；
- Logical Flow 的 unmaterialized / materialized / not_applicable 状态；
- Proposal 状态机、base graph revision 和 source preconditions；
- `evt_`、`prop_`、`chg_` ID 命名空间和引用类型校验；
- apply 分配 Event ID、写入节点 provenance 并返回 Event ID 的一致性；
- affected scope 完整度和 Effective Query Freshness 计算；
- Core 的 affected scope 输出不包含语义变化判断；
- 已映射修改、带历史 Mapping 的删除项和无法连接的新项分别得到 complete/partial 的保守结果；
- approval record 结构校验；
- applied Proposal 和 baseline advanced History Event 的不可变性；
- baseline advanced 的 `no_semantic_change` / `user_accepted` 原因及 approval record 要求；
- Applied Event 的 change set summary 在删除 cache 后仍然自包含；
- graph approval 引用与 History Event 的完整性；
- 原子写入失败回滚；
- Markdown Views 的确定性输出；
- entity refs 的跨目录解析；
- cache 删除后的无损重建；
- cache 删除后用 source baseline 恢复精确文件级差异，旧实体不全时明确标记 partial；
- cache 删除后 applied History Event 仍可读取；
- 受管理源码摘要的确定性和未提交修改检测；
- Source Digest Profile 的路径排序、分隔符、LF/CRLF 规范化和版本失配处理；
- Source、Fact、Cognition 三层 Freshness 状态转换；
- ChangeSet 聚合、去重和 baseline 推进规则；
- 认知图搜索的候选范围和稳定排序；
- CodeEntity 到认知节点的反向解析；
- discussion context 的深度和预算限制。

### 18.2 Codex 集成测试

使用 20～50 个 Python 文件的固定仓库验证：

- 用户显式触发后 Skill 才启动；
- 每次显式 `$codecortex` invocation 至多汇总提醒一次 pending Proposal，连续追问不重复提醒；
- 普通 Codex 编码不启动 CodeCortex 或维护认知状态；
- 每次 CodeCortex 工作前都同步完成 Fact Preflight；
- 源码无变化或当前查询与 complete affected scope 无交集时不调用 Analyzer；
- 当前查询与 affected scope 相交时进入 Source-first degraded mode；
- affected scope 为 partial/unknown 时保守核验源码，但不自动启动全仓 Analyzer；
- Semantic Cognition Sync 确认仅事实变化时自动推进 baseline，且不创建 Proposal；
- 自动推进 baseline 时写入 `reason: no_semantic_change` 的正式 History Event；
- 用户确认图仍然有效时写入带 approval record 的 `reason: user_accepted` Event；
- Semantic Cognition Sync 确认语义变化时按 ChangeSet 聚合为一份 Proposal；
- init 必定派生 Analyzer，且 Analyzer MCP 无写工具；
- Analyzer 无法访问写 MCP；
- Analyzer 返回不超过 512 KiB 的压缩 AnalysisReport，明确未覆盖区域，且不写临时分析数据库；
- Analyzer 分析期间源码摘要变化时报告被拒绝；
- Main Codex 能从 AnalysisReport 创建整体 Proposal；
- 仅从源码归纳出的语义节点不会自动标记为 established；
- 未收到批准时不调用 apply；
- 用户修订后 Proposal 内容同步变化；
- Proposal 作用域源码变化时 apply 进入 STALE；
- Proposal 生成后即使只改了无关源码，也必须重新分析或 revise 后才能 apply；
- apply 缺少或携带不匹配的 approval record 时拒绝写入；
- 批准后 graph、entity refs、source baseline 和 Views 一致更新；
- 批准后不可变 History Event 与 graph revision 一致更新；
- 另一台机器 clone 后能够解析 graph 中的 approval history 引用；
- 不同换行风格和路径分隔符不会制造跨机器虚假源码变化；
- inspect 小范围由 Main Codex 处理；
- 全局 reinitialize 使用 Analyzer 且不静默覆盖用户确认内容；
- 项目级、认知节点级和 CodeEntity 级问题均可回答；
- 连续追问保持当前讨论上下文；
- 问答默认不创建或应用认知修改；
- L3/L4 未加载时从认知锚点动态加载，不误判为 fallback；
- Behavior unmaterialized 时支持“展开并持久化”与“transient 回答”两条路径；
- 用户拒绝展开后，本次工作流不重复询问同一区域；
- 跨 Responsibility 问题按规则派生 Analyzer；
- MCP 查询失败时恢复原生 Codex 搜索；
- 图外问题完整恢复 Native Codex Search，不限制原生源码工具和搜索范围。

Codex 集成验收使用两个相同 commit 的临时 Git 副本，普通单轮 Benchmark 分别运行独立 `codex exec --ephemeral --json` 进程测试 Native Codex 与 CodeCortex；两边固定模型、推理设置、sandbox、问题和预算，记录 MCP 调用、最终回答、token 与耗时。需要 resume 的自动化审批流程不能使用 `--ephemeral`：它在隔离的临时 Codex home 中使用独立测试配置，将 apply 的 host approval 设为 `approve`，再用 `codex exec resume` 验证第一轮未批准不写、第二轮用户批准和 `approval_record` 校验，完成后清理测试 home；产品配置始终保留 `prompt`，并另做一次 VS Code 人工 smoke test。

### 18.3 中型仓库验收

选择一个 100～500 个 Python 文件的真实仓库，验证：

- L0 Responsibility 能形成清晰 Big Picture；
- L1 Behavior 不退化为源码目录镜像；
- 关键 Capability 可以被多个 Behavior 共享引用；
- Implementation Mapping 能定位到真实源码；
- L2/L3 可以按需局部 materialize；
- Main Codex 接收 Analyzer 受限且压缩的 AnalysisReport，不接收大范围源码探索过程；
- 不使用源码向量 RAG；
- 删除 `.cache` 后能够恢复；
- 另一台机器 clone 后无需重新语义初始化；
- 全局重新分析完整经过 Proposal 和审批。
- 基于认知图的问题能展示相关认知路径和关键源码证据；
- 认知图部分覆盖的问题能从语义锚点动态加载事实/源码完成回答，完全无语义锚点的问题能通过 Native Codex Search 完成回答；
- 图外问题在固定 Benchmark 上不出现超过预设 non-regression threshold 的系统性退化。

### 18.4 项目理解质量基准

使用固定仓库 revision 和固定问题集比较：

- 原生 Codex；
- Codex + CodeCortex；
- 可选的 DeepWiki 对照组。

所有组保持模型、推理设置、仓库 revision 和问题文本一致。Native 和 CodeCortex 组使用两个相同 commit 的临时 Git 副本以及互不继承上下文的 Child Codex 进程。问题集覆盖项目 Big Picture、跨模块 Behavior、共享 Capability、实现定位、图内问题、动态 L3/L4、未 materialize Behavior、局部 stale、图外问题和带错误前提的问题。评价规则、重复试验方式和 non-regression threshold 必须在运行 Benchmark 前固定，不能观察结果后修改通过标准。

主要指标包括：答案正确性、关键源码证据准确率、重要职责覆盖率、错误认知引用率，以及回答所需时间和上下文开销。初始化成本单独报告，同时给出在多次问答中的摊销结果，不能把一次性建图成本隐藏在对比之外。

## 19. 里程碑

### 19.1 M0：Codex 集成骨架

在投入完整 AST 和真实仓库分析前，先验证最小纵向链路：

```text
显式触发 $codecortex
  → Main Codex 调用一个只读 MCP 工具
  → Main Codex 派生 Analyzer（只读 MCP profile）
  → Analyzer MCP 只暴露只读工具
  → Main Codex 创建最小 Proposal
  → 未收到用户明确批准时 Main Codex 不得调用 apply
  → 用户批准后 Main Codex 提交结构化 approval record
  → Core 校验 Proposal、approval record 和版本前置条件后原子 apply
  → 新 Codex 会话能够读取持久化结果
```

M0 可以使用极小固定图和测试仓库，但必须验证真实 Codex Skill、MCP 注册、Analyzer 上下文隔离、工具权限和审批闭环。它不依赖普通 Codex 的任务结束 Hook。

M0 明确不包含 Python AST、Freshness、ChangeSet、增量索引、文件系统 watcher 或真实认知建图。版本前置条件可以使用固定测试摘要；M0 的唯一目标是证明真实 Codex 环境中的 Skill、MCP、Analyzer 权限、Proposal 审批、不可变 History Event 和跨会话持久化链路成立。

### 19.2 M1a：Repository → Cognitive Graph

在一个 20～50 文件的测试仓库中完成：

```text
安装 CodeCortex
  → 在 Codex 中显式执行 $codecortex init
  → Core 建立真实 AST 事实
  → Main Codex 派生 Analyzer（只读 MCP profile）
  → Analyzer 返回 L0/L1 建议
  → Main Codex 创建整体 Proposal
  → 用户讨论并批准
  → Core 写入 graph.json 和 Markdown Views
  → 用户展开一个 Behavior
  → 跳转到对应 Class / Method / Function
```

M1a 验证真实 AST 事实、Analyzer 建树、审批写入、渐进展开和源码定位。

### 19.3 M1b：Persistent Understanding

在 M1a 的固定测试仓库上继续完成：

```text
使用 $codecortex ask 发起项目问题
  → 围绕该 Behavior 发起问题并连续追问
  → 从认知锚点动态加载一个 L3/L4 问题
  → 对一个未 materialize Behavior 验证展开/不展开两条路径
  → 对一个真正图外问题成功回退到原生 Codex 搜索
  → 普通 Codex 修改源码但不触发 CodeCortex
  → 下一次 $codecortex ask 先同步完成 Fact Preflight
  → 无关变更不阻塞当前问题
  → 相关或不确定变更进入 Source-first degraded mode
  → $codecortex sync 按需完成 Semantic Cognition Sync
  → 删除本地 cache 后成功恢复
```

M0 以集成链路、权限边界和审批闭环可靠为目标；M1a 验证从仓库到认知图，M1b 验证持续问答、Freshness 收敛和可恢复状态。认知图一次生成完美不是任一里程碑的通过条件。

## 20. 后续演进

当前 MVP 稳定后，按独立阶段增加：

1. 更可靠的数据流事实；
2. 更高性能的大仓库增量索引；
3. CodeCortex Core 活跃期间的可选文件系统 watcher；
4. 可选的普通 Codex 被动维护或任务边界集成；
5. 需求—现有设计适配分析；
6. 编码前治理与设计张力；
7. 用户批准的局部重构闭环；
8. 独立模型 API Runtime；
9. 轻量 VS Code Project Map View；
10. 多语言代码分析。

这些阶段复用同一 CodeCortex Core 和认知图，不改变 MVP 中 Codex、Analyzer、Core 与审批边界的基本职责划分。
