# CodeCortex M1a 详细设计：Repository → Cognitive Graph

**状态：** Implementation Baseline

**前置里程碑：** M0 通过

**目标：** 把中型 Python 仓库转换为有源码证据、可审批、可恢复、可逐层查看的正式认知图。

## 1. 范围

M1a 实现：

- Python Managed Source Set 和规范化源码摘要；
- AST CodeEntity 与代码关系索引；
- 高效 SQLite cache；
- Responsibility、Behavior、Capability、Logical Flow 和 Mapping 正式 schema；
- 首次初始化 Analyzer 流程；
- 全局 reinitialize；
- 领域 Proposal、审批、History 和原子 apply；
- Markdown Views、inspect 和源码定位。

M1a 不实现完整 M1b Freshness 问答路由；但所有正式数据必须带 M1b 所需的 digest、evidence 和 revision。

## 2. Managed Source Set

### 2.1 文件发现

Git 仓库是前置条件。Core 使用无 shell 的子进程调用：

```text
git ls-files -z --cached --others --exclude-standard -- "*.py"
```

随后应用版本化配置 include/exclude，并无条件排除：

- `.git/`
- `.codecortex/`
- 虚拟环境目录；
- build/dist/site-packages；
- Python cache。

输出必须去重并按规范化 relative POSIX path 的 UTF-8 字节顺序排序。符号链接指向仓库外时拒绝读取并产生 diagnostic。未被 Git ignore 的 untracked Python 文件必须进入集合。

### 2.2 文本与摘要

使用 `tokenize.detect_encoding` 识别 Python 源码编码，解码后只规范化：

- `CRLF` 和单独 `CR` → `LF`；
- relative path 分隔符 → `/`。

不做 Unicode normalization、不删除尾随空格、不格式化源码。每个文件：

```text
file_digest = SHA256(normalized_text_utf8)
```

仓库摘要：

```text
repository_digest = SHA256(
  profile_version + NUL +
  sorted(relative_path + NUL + file_digest + LF)
)
```

Digest Profile 和 Managed Source Set 算法版本保存在 manifest。规则版本变化必须全量重新扫描，不能继承旧 baseline。

与正式 cognition baseline 同步提交的 `.codecortex/source_baseline.json` 只保存基线文件清单和摘要，不保存源码或 AST：

```yaml
schema_version: 1
digest_profile_version: 1
managed_source_set_version: 1
repository_source_digest: "sha256:..."
files:
  - relative_path: src/codecortex/application/query.py
    content_digest: "sha256:..."
```

`files` 按规范化相对路径排序且路径唯一。文件中的仓库摘要必须与 manifest cognition baseline 相同。它只在初始化 apply、带图修改的 apply 或 baseline advance 正式事务中整体替换；普通 Fact Sync 绝不改它。这样 SQLite 被删除或换机器后，Core 仍能从“已接受的逐文件基线”与当前源码精确恢复 added/modified/deleted 文件集合。

当 manifest `cognition_initialized=false` 时，cognition baseline 和 `repository_source_digest` 必须同时为 null，`files=[]`；M0 技术验收即使推进 graph revision 也保持该状态。首次真实初始化 apply 原子设置 `cognition_initialized=true` 并建立非空 baseline，此后不再允许 null。

### 2.3 首次认知初始化与 revision

是否已经完成项目理解只能由 `cognition_initialized` 判断，不能由 `graph_revision` 判断。M0 允许用户为审批链路测试创建正式节点，因此一个尚未理解项目的仓库可以处于任意 `r >= 0` 的 graph revision，同时仍满足 `cognition_initialized=false`。

M1a 的首次认知初始化规则为：

- formal state 不存在时，先执行幂等 `initialize_repository`，得到 revision 0 技术骨架；
- formal state 已存在且 `cognition_initialized=false` 时，以当前 revision `r` 为 AnalysisReport 和 Proposal 的 base revision；
- 初始化 Proposal 的 apply 产生 `r + 1`，并在同一事务设置 `cognition_initialized=true` 与非空 source baseline；只有从全新 skeleton 初始化时，这个值才恰好为 1；
- 已由 M0 审批写入的节点、边和 History 都是现有正式状态。Analyzer 可以建议删除、替换或保留它们，但必须作为当前 Proposal 的显式操作并经用户批准；M1a 不得为了“重新初始化”而静默清空它们。

因此，任何缓存 metadata、AnalysisReport、Proposal 或验收 fixture 都必须使用实际 current graph revision，而不是写死 0 或 1。

## 3. AST 解析

### 3.1 支持范围

CodeCortex 运行在 Python 3.14，使用标准库 `ast.parse`。支持目标仓库 Python 3.9～3.14 语法；`feature_version` 只作为 best-effort 兼容开关，不宣称等同历史 CPython parser。

提取：

- Module/File；
- Class；
- Function/AsyncFunction；
- Method/AsyncMethod；
- import/from import 原始声明；
- declared base 表达式；
- decorator；
- signature；
- docstring 摘要；
- start/end source location。

不 import 或执行目标代码。单文件语法或编码失败时写 diagnostic，其他文件继续。

### 3.2 实体地址

```yaml
uid: ent_01...
address: api.services.wiki:WikiService.generate
kind: method
module_name: api.services.wiki
qualname: WikiService.generate
relative_path: api/services/wiki.py
start_line: 42
end_line: 96
signature: "(self, request: WikiRequest) -> Wiki"
fingerprint: "sha256:..."
```

`__init__.py` 映射到包 module，普通文件映射到文件 stem。嵌套函数和类保留完整 qualname；`<locals>` 不写入稳定地址。

fingerprint 基于实体 kind、规范化 signature、decorator 名称和移除位置/无关格式后的 AST 结构计算，不包含实体自身名称，使改名有机会匹配；它只是身份线索，不是语义等价证明。

### 3.3 UID 保持

按顺序匹配：

1. 相同 address 且 kind 一致：保留 UID；
2. Git 高置信 rename 后相同 qualname/fingerprint：保留 UID；
3. 原实体消失且唯一候选具有相同 kind、signature 和 fingerprint：保留 UID；
4. 多候选或证据冲突：不迁移，旧实体标为 missing，新实体获得新 UID；
5. `entity_refs.json` 中的正式 UID 是 cache 重建时的身份种子。

不在正式 Mapping 中使用的临时实体 UID 可以在全量 cache 重建后变化。AnalysisReport 到 Proposal 创建之间必须重新验证 UID、address 和 fingerprint。

## 4. 代码关系

### 4.1 确定性语法事实

```text
contains
import_declaration
declared_base
```

它们直接来自 AST，confidence 固定为 `syntactic`。

### 4.2 解析/推断关系

```text
imports
inherits
tested_by
calls
```

每条必须保存：relation type、source、resolved/unresolved target、evidence、confidence、resolver version。`calls` 和 `tested_by` 是 best-effort；动态调用、monkey patch、dependency injection 和运行时注册无法完整恢复时必须明确 unresolved。

MVP 不实现数据流引擎，只保留关系 schema 的 evidence/confidence 扩展位置。

## 5. SQLite 文件与 metadata

数据库路径：`.codecortex/.cache/facts.sqlite3`。

`cache_metadata` 为单行表：

```sql
CREATE TABLE cache_metadata (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  cache_schema_version INTEGER NOT NULL,
  parser_version TEXT NOT NULL,
  digest_profile_version INTEGER NOT NULL,
  managed_source_set_version INTEGER NOT NULL,
  repository_source_digest TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  baseline_entity_snapshot_completeness TEXT NOT NULL,
  index_generation INTEGER NOT NULL,
  built_at TEXT NOT NULL
);
```

任何版本、digest 或 revision 不匹配都禁止把认知查询和源码事实拼接返回。

## 6. SQLite 事实表

```sql
CREATE TABLE source_files (
  file_id INTEGER PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  module_name TEXT,
  content_digest TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  parse_status TEXT NOT NULL,
  is_test INTEGER NOT NULL CHECK (is_test IN (0,1)),
  diagnostic_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE entities (
  uid TEXT PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES source_files(file_id) ON DELETE CASCADE,
  address TEXT NOT NULL,
  module_name TEXT NOT NULL,
  qualname TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  parent_uid TEXT REFERENCES entities(uid) ON DELETE CASCADE,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  signature TEXT,
  docstring_digest TEXT,
  fingerprint TEXT NOT NULL,
  resolution_status TEXT NOT NULL,
  UNIQUE(address, kind)
);

CREATE TABLE relations (
  relation_id INTEGER PRIMARY KEY,
  relation_type TEXT NOT NULL,
  source_uid TEXT REFERENCES entities(uid) ON DELETE CASCADE,
  source_file_id INTEGER NOT NULL REFERENCES source_files(file_id) ON DELETE CASCADE,
  target_uid TEXT REFERENCES entities(uid) ON DELETE SET NULL,
  target_module TEXT,
  raw_expression TEXT,
  resolution_status TEXT NOT NULL,
  confidence TEXT NOT NULL,
  resolver_version TEXT NOT NULL,
  relation_key TEXT NOT NULL UNIQUE
);

CREATE TABLE relation_evidence (
  evidence_id INTEGER PRIMARY KEY,
  relation_id INTEGER NOT NULL REFERENCES relations(relation_id) ON DELETE CASCADE,
  relative_path TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  evidence_kind TEXT NOT NULL,
  snippet_digest TEXT NOT NULL
);

CREATE TABLE diagnostics (
  diagnostic_id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES source_files(file_id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER
);

CREATE TABLE baseline_entity_snapshots (
  uid TEXT PRIMARY KEY,
  baseline_source_digest TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  address TEXT NOT NULL,
  module_name TEXT NOT NULL,
  qualname TEXT NOT NULL,
  kind TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  signature TEXT,
  UNIQUE(baseline_source_digest, address, kind)
);
```

必要索引：

```sql
source_files(module_name), source_files(content_digest)
entities(file_id), entities(module_name), entities(parent_uid)
entities(kind), entities(fingerprint), entities(address)
relations(source_uid, relation_type)
relations(target_uid, relation_type)
relations(target_module, relation_type)
diagnostics(file_id, severity)
baseline_entity_snapshots(relative_path)
baseline_entity_snapshots(module_name, fingerprint)
```

`baseline_entity_snapshots` 是可删除的本机辅助数据，不是正式真相。cognition baseline 推进时，因为 MVP 要求当前源码与 Proposal 分析摘要完全一致，cache 可以从当前 `entities` 复制一份 baseline entity 快照，并把 completeness 设为 `complete`。后续 Fact Sync 只更新当前实体，保留这份快照，用于始终按“正式 baseline → current”重算单一 ChangeSet。cache 丢失且当前源码已经偏离 baseline 时，只能用 `entity_refs.json` 恢复被正式引用的旧实体，completeness 必须为 `partial`；不能伪造完整快照。

`relation_key` 只由关系声明本身的稳定信息计算：source relative path、source declaration address、relation type、声明位置和规范化 raw expression；临时 entity UID 与 resolved target 都不进入 key。目标解析变化时更新同一关系，不制造一条“新关系”。

增量同步先在事务外解析变化文件，再获取独占仓库锁并重新验证输入摘要。更新变化/删除文件前，先按旧 `target_uid`、旧/新 module name 收集指向这些目标的 incoming relations，并包含可能被新增 module/entity 解析成功的同名 unresolved relations；替换实体和本文件派生关系后，对这些来源声明重新运行关系 resolver。该步骤只读取来源文件已有 AST 事实/声明，不重新解析未变化文件。它保证 A 文件的实体改名、增加或删除后，B 文件缓存的 imports/inherits/calls 不会继续错误指向旧目标或错过新目标。

摘要未变化时，在一个短事务中按 file 批量替换记录、刷新 incoming relation resolution 并最后更新 metadata generation。摘要已经变化则放弃本批结果并重试，不把针对旧源码的 AST 写入 cache。读取接口不得逐实体发 SQL。

## 7. SQLite 认知查询副本

认知查询副本使用以下列级 schema（为简洁省略重复的 `CHECK` 枚举，枚举仍由 Core 和迁移测试锁定）：

```sql
CREATE TABLE cognitive_nodes (
  node_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  epistemic_status TEXT NOT NULL,
  intent TEXT,
  observed TEXT NOT NULL,
  node_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE cognitive_aliases (
  node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  PRIMARY KEY (node_id, position),
  UNIQUE (node_id, normalized_alias)
);

CREATE TABLE cognitive_edges (
  edge_id TEXT PRIMARY KEY,
  edge_type TEXT NOT NULL,
  source_node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  target_node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  epistemic_status TEXT NOT NULL,
  edge_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  UNIQUE (edge_type, source_node_id, target_node_id)
);

CREATE TABLE logical_flows (
  behavior_id TEXT PRIMARY KEY REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  materialization_status TEXT NOT NULL,
  flow_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE flow_steps (
  step_id TEXT PRIMARY KEY,
  behavior_id TEXT NOT NULL REFERENCES logical_flows(behavior_id) ON DELETE CASCADE,
  step_order INTEGER NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  UNIQUE (behavior_id, step_order)
);

CREATE TABLE flow_step_capabilities (
  step_id TEXT NOT NULL REFERENCES flow_steps(step_id) ON DELETE CASCADE,
  capability_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (step_id, capability_id),
  UNIQUE (step_id, position)
);

CREATE TABLE entity_reference_index (
  entity_uid TEXT PRIMARY KEY,
  last_known_address TEXT NOT NULL,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  signature TEXT,
  fingerprint TEXT NOT NULL,
  resolution_status TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE implementation_mappings (
  mapping_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  entity_uid TEXT NOT NULL REFERENCES entity_reference_index(entity_uid),
  role TEXT NOT NULL,
  resolution_status TEXT NOT NULL,
  evidence_note TEXT NOT NULL,
  mapping_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  UNIQUE (subject_kind, subject_id, entity_uid, role)
);

CREATE TABLE cognitive_evidence (
  evidence_id TEXT PRIMARY KEY,
  owner_kind TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  evidence_kind TEXT NOT NULL,
  entity_uid TEXT REFERENCES entity_reference_index(entity_uid),
  relative_path TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  observation TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE history_event_index (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  resulting_graph_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  relative_path TEXT NOT NULL
);

CREATE TABLE cognitive_search_terms (
  term TEXT NOT NULL,
  node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  weight INTEGER NOT NULL,
  PRIMARY KEY (term, node_id, field)
);
```

`subject_id` 和 evidence `owner_id` 是多态引用，SQLite 无法用一个外键表达 node/flow_step/edge/mapping 四种目标，因此由 graph importer 在同一事务内强校验，`foreign_key_check` 之外另运行 `validate_polymorphic_refs`。

必要索引：

```sql
cognitive_nodes(kind, node_id)
cognitive_aliases(normalized_alias, node_id)
cognitive_edges(source_node_id, edge_type)
cognitive_edges(target_node_id, edge_type)
logical_flows(materialization_status, behavior_id)
flow_steps(behavior_id, step_order)
implementation_mappings(subject_kind, subject_id)
implementation_mappings(entity_uid, resolution_status)
cognitive_evidence(owner_kind, owner_id)
cognitive_evidence(entity_uid)
cognitive_evidence(relative_path)
history_event_index(resulting_graph_revision, event_type)
cognitive_search_terms(term, weight, node_id)
```

搜索文本使用 NFKC + casefold 规范化；Latin 文本提取字母数字词项，CJK 连续文本保留完整短词并生成二元/三元片段，query 使用同一算法。title、exact alias、alias、summary、observed 按固定权重递减。搜索先按 term 召回，按累计 weight、kind、node ID 确定性排序并强制 limit，不依赖各机器 SQLite 是否编译 FTS5。

关键约束：

- 每行带 `graph_revision`；
- `entity_reference_index` 来自正式 `entity_refs.json`，包括 missing/ambiguous 实体；
- Mapping 外键指向 `entity_reference_index`，不能强迫实体当前仍存在于 `entities`；
- 认知副本整体导入，不允许局部 revision 混合；
- 所有列表结果稳定排序并强制 limit。

正式 graph apply 后认知副本在同一仓库锁内刷新。失败时 metadata graph revision 保持旧值，使下一次读取触发重建。

## 8. `graph.json` schema

顶层：

```yaml
schema_version: 1
graph_revision: 1
nodes: []
semantic_edges: []
logical_flows: []
implementation_mappings: []
```

### 8.1 Node

```yaml
id: capability.source-grounded-retrieval
kind: capability
title: Source-grounded Retrieval
aliases: [源码检索]
summary: 使用认知范围和代码事实定位真实实现
epistemic_status: inferred
intent: null
observed: 当前实现先缩小范围，再读取相关源码
evidence: []
created_by: analyzer
last_modified_by: analyzer
node_revision: 1
approval:
  approval_event_id: evt_...
  approved_by: user
  approved_at: "...Z"
```

kind 只允许 responsibility、behavior、capability。`epistemic_status` 只允许 established、inferred、uncertain。源码推断默认不能自动成为 established。

### 8.2 Semantic Edge

```yaml
id: edge_...
type: uses
source_id: behavior.repository-question-answering
target_id: capability.source-grounded-retrieval
epistemic_status: inferred
evidence: []
edge_revision: 1
approval: {...}
```

允许：

```text
Responsibility -contains-> Behavior
Behavior       -uses-> Capability
Capability     -depends_on-> Capability
```

每个正式 Behavior 必须恰有一个 Responsibility parent；contains 必须无环。Capability dependency cycle 允许，但产生 diagnostic。

### 8.3 Logical Flow

```yaml
behavior_id: behavior.repository-question-answering
materialization_status: materialized
flow_revision: 1
steps:
  - id: behavior.repository-question-answering#step.resolve-scope
    order: 1
    title: Resolve cognitive scope
    summary: 检索候选认知节点并计算局部 freshness
    uses_capabilities: [capability.source-grounded-retrieval]
    evidence: []
    approval: {...}
approval: {...}
```

`materialization_status`：

- `unmaterialized`：尚未深入分析；
- `materialized`：已经建立 Flow；
- `not_applicable`：确认不适合流程表达。

Flow Step ID 局部稳定，order 从 1 开始且连续，不允许重复。改变状态、步骤语义或顺序都需要 Proposal。

### 8.4 Implementation Mapping

```yaml
id: map_...
subject_kind: flow_step
subject_id: behavior.repository-question-answering#step.resolve-scope
entity_uid: ent_...
role: primary
resolution_status: resolved
evidence_note: 该函数实现候选节点检索和范围裁剪
mapping_revision: 1
approval: {...}
```

subject 可以是 node 或 flow_step。role 为 primary/supporting。resolution status 为 resolved/missing/ambiguous。Mapping 是语义结论，新增、迁移和删除都要审批；高置信代码地址变化只更新 `entity_refs.json`，不改变 Mapping 语义。

### 8.5 Evidence

证据和 Mapping 分离。Evidence 至少包含：

```yaml
id: evid_...
kind: code_entity
entity_uid: ent_...
relative_path: src/codecortex/application/query.py
start_line: 20
end_line: 85
observation: 该用例按节点范围组织查询上下文
```

也可以引用 repository document。正式 Evidence ID 使用 `evid_` 命名空间并保持稳定；owner 由它所在的 node、edge、flow step 或 mapping 确定。证据位置是快照，查询时优先通过 UID 解析最新地址；无法解析时保留最后位置并标记 stale。

## 9. `entity_refs.json`

只保存正式图实际引用的 UID：

```yaml
schema_version: 1
graph_revision: 1
entities:
  - uid: ent_...
    last_known_address: codecortex.application.query:QueryService.execute
    kind: method
    relative_path: src/codecortex/application/query.py
    signature: "(...)"
    fingerprint: "sha256:..."
    resolution_status: resolved
```

apply 根据 graph 中全部 Mapping/Evidence 重新计算集合，不保留无引用实体。cache rebuild 先用这些记录尝试恢复正式 UID。

## 10. AnalysisReport

Analyzer 通过 Codex subagent 返回一份完整、压缩、结构化报告。这里的“完整”只表示报告包含 Analyzer 本次选择提交的全部候选数据，不表示覆盖整个仓库：

```yaml
schema_version: 1
base_graph_revision: <当前 graph_revision>
analyzed_source_digest: "sha256:..."
analysis_scope:
  mode: repository
  files: 240
  modules: 58
coverage:
  analyzed_partitions: []
  unexamined_partitions: []
candidate_nodes: []
candidate_edges: []
candidate_flows: []
candidate_mappings: []
evidence: []
uncertainties: []
unmapped_regions: []
diagnostics: []
```

报告不保存到独立 analysis database，也没有 analysis lifecycle。Main 收到后立即调用 `create_cognitive_proposal`；Proposal 成为唯一临时工作状态。

默认安全上限：序列化报告 512 KiB、节点 300、边 1000、Mapping 2000、Evidence 2000、单条 description/observation 240 Unicode code points。总字节上限优先于各项数量上限。预计超限时 Analyzer 必须提高语义层级、减少低价值候选，并在 `unexamined_partitions` / `unmapped_regions` 中明确列出未覆盖区域，不分块偷偷提交，也不把“报告完整”描述成“仓库完整覆盖”。

## 11. 初始化流程

```text
$codecortex init
→ initialize_repository（若 formal state 尚不存在）
→ sync_repository_facts（全量）
→ analysis_scope（推荐分区）
→ Main 强制派生 Analyzer
→ Analyzer 分区读取事实和关键源码
→ 返回 AnalysisReport
→ Main 创建整体 Proposal
→ 用户查看 Big Picture、讨论和修订
→ 用户批准 current patch_digest
→ apply 产生当前 revision + 1 和 cognition baseline
```

Analyzer 顺序：项目文档/入口 → package/module 分区 → 分区职责行为 → 跨区依赖 → 关键 Capability → 全局汇总。初始化优先生成 L0/L1 和关键 L2，不为每个函数制造 Capability。

创建 Proposal 前 Core 重新同步事实并检查 AnalysisReport 的 source digest。Analyzer 开始分析时记录 digest，Main 消费报告时必须再次检查；期间源码有任何变化时，本报告整体作废并重新分析，不使用 affected-scope 规则替旧报告续命。Proposal 创建后，任何 Managed Source Set 摘要变化都使它 stale；MVP 不实现“证明变化无关后继续 apply”的复杂 rebase 优化。

## 12. Reinitialize

`$codecortex reinitialize` 必须使用 Analyzer，并读取已有 graph、intent、审批 history 和当前源码。输出全局差异 Proposal：新增、删除、移动、合并、拆分、冲突。

它不清空旧图。已有 user-confirmed intent 或修正不能静默覆盖；冲突必须单列。真正清空是单独破坏性操作，不属于 MVP。

## 13. Proposal 领域操作

不使用数组位置敏感的 RFC 6902 JSON Patch。操作类型：

```text
add_node / update_node / remove_node
add_edge / update_edge / remove_edge
set_logical_flow
add_mapping / update_mapping / remove_mapping
```

每个操作按稳定 ID 寻址，包含 before precondition 和 after value。删除节点必须显式处理相关 edge、flow、mapping，Core 不执行不可见级联。

完整 Proposal 包含：base revision、AnalysisReport source digest、细粒度 source preconditions、operations、结构化 before/after summary、affected nodes、evidence、uncertainties、revision log 和 canonical patch digest。

revision 后 digest 改变，旧批准无效。apply 时要求当前 repository source digest 与 `analyzed_source_digest` 完全一致，并再次检查路径摘要和 entity fingerprint；任一源码变化都标记 STALE。后续版本可增加有证据的局部 rebase，MVP 不承担这套复杂度。

## 14. History 与 baseline

M1a 正式事件：

- `cognitive_proposal_applied`：独立 event ID、Proposal 完整快照、Patch、approval、source preconditions 和 change set summary；
- `cognition_baseline_advanced`：无图修改时推进 baseline，M1b 使用。

节点/边/Flow/Mapping 的 approval 指向最新改变它的 applied Event；Event 内保存 Proposal ID，形成 `Object → Event → Proposal Snapshot`。

初始化 apply 原子设置 `cognition_initialized=true`，并和后续认知 Proposal apply 一样，把已经重新核验的当前 source digest 设为 cognition baseline，从当前排序后的逐文件摘要在同一正式事务生成对应 `source_baseline.json`。History Event 不可修改；cache 中的 pending Proposal 可以删除。

## 15. 原子 apply

1. 仓库独占锁；
2. Fact Sync；
3. proposal、approval、graph/source preconditions 校验；
4. 内存应用全部操作；
5. 全图、entity refs、source baseline 和 History 引用校验；
6. 生成 Event ID、revision、source baseline、Views；
7. 写事务 staging/journal；
8. event、graph、entity refs、source baseline、views 替换；
9. manifest 最后提交；
10. 刷新 SQLite 认知副本。

View 生成失败时正式 revision 不推进。SQLite 刷新失败时正式 revision 保留，cache 下次重建。

## 16. Views 与渐进查看

生成：

```text
.codecortex/views/TREE.md
.codecortex/views/responsibilities/<node-id>.md
.codecortex/views/behaviors/<node-id>.md
.codecortex/views/capabilities/<node-id>.md
```

TREE 展示 Responsibility → Behavior 和共享 Capability 引用。节点页展示 summary、intent、observed、epistemic status、关系、Flow、Mapping、证据和相对源码链接。

Markdown 是 graph 的确定性投影。Core 保存 view digest；手工修改被检测并提示，重新生成以 graph 为准。用户手工背景写 `PROJECT.md`。

View digest 使用 Git 携带的 `.codecortex/view_manifest.json`，而不是 SQLite：

```yaml
schema_version: 1
graph_revision: 7
files:
  - relative_path: views/TREE.md
    content_digest: sha256:...
```

`files` 按 relative POSIX path 排序且必须精确覆盖 `views/` 中由 Core 管理的文件。每次 M1a 正式 apply 在渲染 Markdown 后一起写入该 manifest；读取正式状态时重新计算字节摘要，缺失、额外、符号链接或摘要不一致都属于 formal state corruption。`PROJECT.md` 不在其中，始终允许用户编辑。

旧 M0 technical state 没有 `view_manifest.json`。M1a 必须把它识别为只读 legacy state：在第一次 M1a Proposal apply 之前先以 M0 renderer 验证现有 View 与 graph 一致；验证通过后，才把新的 View manifest 与该 approved apply 一起写入。验证失败时不得静默覆盖手工修改，必须要求用户恢复 View 或重新生成后重试。不得通过普通读请求自动迁移、重写 View 或推进 graph revision。

`inspect_node` 返回 node、父子/依赖、Flow、Mapping、证据、materialization、freshness 占位和 truncation。L3 由 Mapping + SQLite 当前实体动态投影；L4 始终读取真实源码，不复制到图。

## 17. M1a MCP 增量

两个 profile：

- `repository_facts(scope, cursor, limit)`
- `analysis_scope(scope?)`
- `resolve_entity_context(entity_uid?, path?, address?, relation_types?, limit?)`
- `get_discussion_context(node_ids?, entity_ids?, depth, max_nodes, max_entities, max_evidence)`
- `search_cognitive_graph(query, kinds, limit)`

仅 Main：

- `sync_repository_facts(mode = "auto" | "full")`

M0 Proposal 工具扩展为完整 M1a schema。所有列表返回 cursor 和 `truncated`。

M0 的无界 `cognitive_graph()` 仅保留为兼容诊断入口：M1a 自身的 Skill、Main 和 Analyzer 不得用它加载全图。Task 8 必须给它增加受配置上限保护；超过上限时返回明确错误并要求使用 `search_cognitive_graph`、`get_discussion_context` 或其他带 cursor/limit 的接口。新增和扩展的所有列表接口都必须返回 `cursor` 与 `truncated`，不能以 M0 兼容为由绕过上下文上限。

## 18. 测试

### 18.1 Parser/事实

- Python 3.9～3.14 语法 fixtures；
- CRLF/LF 跨机器 digest；
- tracked、untracked、gitignored 和 symlink；
- 嵌套定义、async、decorator、复杂 signature；
- 单文件语法/编码错误；
- import/inheritance resolved 与 unresolved；
- 目标实体修改/删除后，未变化来源文件的 incoming imports/inherits/calls 重新解析；
- relation key 不随 resolved target 改变，增量更新与全量重建一致；
- rename、duplicate fingerprint 和 ambiguous identity。

### 18.2 数据库

- DDL、外键、必要索引存在；
- baseline entity snapshot 在连续 cache 下保持不变，baseline 推进时才整体刷新；
- 增量更新等于全量重建结果；
- 作用域批量查询无 N+1；
- graph revision 不匹配不返回数据；
- cache replace、损坏和删除恢复；
- Analyzer `mode=ro` 无法写。

### 18.3 图和 Proposal

- 所有允许/禁止边；
- Behavior 唯一 parent；
- Flow materialization 和顺序；
- dangling approval/UID；
- Patch digest、revision、stale source；
- apply 故障注入和 History 不可变；
- View 可重复生成。
- source baseline 与 manifest digest 一致，事务失败不会只推进其中一方；
- 首次初始化同时设置 `cognition_initialized=true`；M0 技术状态即使 revision 大于 0 仍可明确识别为未初始化；
- Analyzer 开始后源码变化时 AnalysisReport 被拒绝；

### 18.4 真实初始化

在中型 Python fixture repo 运行 Child Codex：Analyzer 返回完整报告，Main 创建 Proposal，用户模拟批准后产生 base revision + 1；检查关键结论能跳转到真实源码，且原始大范围探索未进入 Main 输出。另以含 M0 审批测试节点、但 `cognition_initialized=false` 的仓库覆盖首次认知初始化，确认它不会静默清空既有正式对象。

## 19. M1a 完成条件

- 100～500 个 Python 文件仓库可以建立稳定事实索引；
- cache 查询按 scope/limit 完成，不整库加载；
- Analyzer 不通过 MCP 写状态，受限报告可以生成 Proposal；
- 初始化后有可理解的 L0/L1 和关键 Capability；
- 所有正式语义修改有 approval Event；
- inspect 可从认知节点到当前源码；
- 删除 cache 或换机器后能恢复相同正式图、实体引用和精确文件级 baseline 差异；
- reinitialize 不静默覆盖用户确认内容。
