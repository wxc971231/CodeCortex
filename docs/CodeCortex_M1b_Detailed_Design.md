# CodeCortex M1b 详细设计：Persistent Understanding

**状态：** Implementation Baseline

**前置里程碑：** M1a 通过

**目标：** 在源码持续变化时保持局部可信的项目理解，并支持基于认知图、代码事实和 Native Codex 的任意问题讨论。

## 1. 范围

M1b 实现：

- 每次 CodeCortex 工作前的确定性 Fact Preflight；
- cognition baseline、单一 ChangeSet 和 affected scope；
- 仓库级提示状态与查询级局部 Freshness；
- 按需 Semantic Cognition Sync；
- 图引导动态 L3/L4；
- 未 materialize Behavior 的可选完整展开；
- 图内、部分覆盖、受影响和真正图外问题；
- Source-first degraded mode 与 Native Codex fallback；
- bounded discussion context；
- cache recovery；
- 独立 Child Codex 对照 Benchmark。

M1b 不实现后台文件监听、向量检索、完整调用图、数据流引擎或自动批准认知变化。

## 2. Freshness 基本原则

1. 普通 Codex 编码不维护 CodeCortex cache 或认知图；
2. 用户显式进入 `$codecortex` 工作时先执行 Fact Preflight；
3. Fact Preflight 完全由 Core 固定代码完成，不调用 Agent；
4. 源码事实必须更新，语义认知可以按问题范围延迟同步；
5. 仓库 pending 不代表所有节点都不可信；
6. 认知图是导航和已批准理解，当前源码是实现事实的最终来源；
7. 问答不以更新认知图为前置条件。

## 3. Fact Preflight

每次 `$codecortex ask / inspect / sync / reinitialize / expand`：

```text
重新枚举全部 Managed Source Set
→ 流式计算全部规范化文件摘要
→ 与 SQLite source_files 比较；cache 缺失时与正式 source_baseline 比较
→ 只 AST 解析摘要变化的文件
→ 原子更新事实索引
→ 计算当前 repository source digest
→ 与 cognition baseline 对比
```

目标仓库为 100～500 个 Python 文件，因此每次哈希全部受管理文件，避免 mtime 快路径的漏检复杂性。只有变化文件重新解析。相同输入调用幂等，`index_generation` 只在事实内容发生变化时推进。

解析失败不删除上次正式认知；该文件当前事实标为 parse_error，并进入 unmapped/uncertain scope。

## 4. Cognition Baseline

manifest 保存：

```yaml
cognition_baseline:
  source_digest: "sha256:..."
  graph_revision: 12
  digest_profile_version: 1
  accepted_event_id: evt_...
```

本节所有 Freshness 流程都要求 manifest `cognition_initialized=true`。baseline 表示“正式认知已经针对这份源码状态完成语义处理”，不是最后一次 Git commit。graph revision 可以不变而 baseline 推进，例如确认代码重构没有改变认知语义；若该标志为 false，M1b 工具返回 `NOT_INITIALIZED`，不得把 M0 技术 revision 当成可查询认知。

`.codecortex/source_baseline.json` 与 manifest baseline 属于同一正式状态，保存当时全部 Managed Source Set 的规范化相对路径和逐文件 digest。manifest 提供快速总摘要，source baseline 提供 cache 删除或跨机器恢复时的文件级比较依据；两者不一致属于 `FORMAL_STATE_CORRUPT`。它不保存源码、AST 或每个历史版本，只保存当前正式 baseline。

## 5. 单一有效 ChangeSet

Core 始终计算：

```text
正式 cognition baseline → 当前 Managed Source Set
```

而不是保存每次文件修改产生的一长串差异。当前变化再次发生时，用新结果替换 cache 中旧的有效 ChangeSet；已经绑定旧 digest 的 Proposal 通过 source preconditions 变为 stale。

ChangeSet schema：

```yaml
schema_version: 1
change_set_id: chg_...
baseline_source_digest: "sha256:..."
current_source_digest: "sha256:..."
created_at: "...Z"
changed_files:
  added: []
  modified: []
  deleted: []
  renamed: []
changed_entities:
  added: []
  modified: []
  missing: []
  moved: []
file_diff_completeness: complete
entity_diff_completeness: complete
affected_nodes: []
affected_flows: []
scope_confidence: complete
unmapped_changes: []
diagnostics: []
```

无变化时不保留 ChangeSet，repository cognition status 为 fresh。

文件级差异由正式 source baseline 和当前摘要确定，因此在 cache 丢失后仍可完整恢复 added/modified/deleted。仅当删除与新增文件具有唯一相同 content digest 时标记为 renamed；否则保守保留为 added + deleted。`file_diff_completeness` 正常必须为 `complete`，基线文件缺失/非法时拒绝继续而不是猜测。

`entity_diff_completeness` 为 `complete | partial`。SQLite 连续存在时，`baseline_entity_snapshots` 保留正式 baseline 的实体快照，当前变化不会覆盖它，因此可按 baseline 比较新旧实体；只有 cognition baseline 推进时才整体刷新该表。cache 丢失后，Core 能精确恢复当前实体以及 `entity_refs.json` 中被正式认知引用的旧实体，但无法列举未被正式引用的所有旧实体，因此标记 `partial`。这个字段只描述实体清单完整度，不能被误用为 affected scope 已经完整。

## 6. Affected Scope 算法

固定算法按顺序执行：

1. 找出变化/消失 CodeEntity；
2. 查询直接 Implementation Mapping；
3. 查询正式 node/edge/flow evidence；
4. Flow Step 向所属 Behavior 传播；
5. Behavior 向唯一 Responsibility parent 传播；
6. Capability 向直接 uses 它的 Behavior 传播；
7. 必要时沿 resolved 本地 import/inherits/calls 进行最多一跳传播；
8. 收集未解析文件、动态关系和无正式映射变化。

不进行无限递归依赖扩散。`scope_confidence=complete` 必须同时满足：全部变化/删除文件已识别且解析成功；每项变化都能连接到正式 Mapping/Evidence，或被规则化地证明与任何正式认知无关；相关关系没有 unresolved；传播边界内不存在无法归属的变化。`entity_diff_completeness=partial` 不会自动导致 scope partial，但只有上述条件都由正式引用和当前事实证明时才允许 complete。

任一条件不满足时设置 `scope_confidence=partial|unknown` 并保存具体 `unmapped_changes` / diagnostics。Core 不启动 Agent 猜测范围，也不得因为“暂时没找到映射”乐观标记 complete。

## 7. Freshness 状态

### 7.1 仓库级

```text
fresh       当前 digest 等于 baseline
pending     存在 ChangeSet，影响范围至少部分可知
unresolved  变化存在但范围无法可靠界定
```

它用于状态提示，不直接决定每个问题能否使用图。

### 7.2 查询级

`effective_query_freshness(node_ids?, entity_ids?)` 返回：

```text
current
unaffected_current
affected_source_first
unknown_source_first
```

- fresh 仓库 → current；
- pending 且查询节点与 affected scope 不相交、scope complete → unaffected_current；
- 相交 → affected_source_first；
- 有可能相关的 unmapped/unknown → unknown_source_first。

结果附带 matched affected nodes、unmapped changes 和依据，Main 不自行重算。

## 8. Semantic Cognition Sync

Fact Preflight 不自动触发 Analyzer。语义同步发生在：

- 用户显式 `$codecortex sync`；
- 当前问题需要受影响区域的最新完整认知；
- 用户选择展开未 materialize Behavior；
- reinitialize；
- Main 在讨论自然检查点建议并获用户同意。

小范围由 Main Codex 读取最新事实和源码；大范围、跨 Responsibility 或范围不确定时必须使用 Analyzer。

结果三类：

1. **有语义变化：** 创建聚合 Proposal，批准 apply 后 graph、manifest baseline 和 source baseline 一起推进；
2. **无语义变化：** 调用 `advance_cognition_baseline(reason=no_semantic_change)`，自动写正式 Event，不需要用户审批；
3. **用户确认旧认知仍有效：** `reason=user_accepted`，必须携带 approval record。

baseline advance Event、manifest 与 source baseline 更新使用同一正式状态事务；新的 source baseline 从已重新核验 digest 的当前 Managed Source Set 生成。它不能引用已经过期的 ChangeSet digest。

## 9. 问答路由总览

```text
用户问题
→ Fact Preflight
→ search_cognitive_graph 候选召回
→ Main 判断语义相关节点
→ effective_query_freshness
→ bounded context + 动态事实/源码
→ Main 回答
```

Core 的 search 是确定性候选召回，不替代 Main 理解自然语言。

## 10. 四层查询路由

### 10.1 已有语义锚点且认知充分

读取相关 node、关系、Flow、Mapping 和证据。fresh/unaffected 时以认知图组织答案，并按问题需要读取少量源码确认实现细节。

### 10.2 已有语义锚点，L3/L4 尚未加载

这不算 fallback。使用认知节点缩小范围：

```text
Responsibility/Behavior/Capability
→ Mapping
→ SQLite 当前 CodeEntity/关系
→ 按需读取 L4 源码
```

L3 是动态实现投影，L4 永远是真实源码，不要求持久化全部实现结构。

### 10.3 Behavior 未完整 materialize

只有完整展开会显著改善当前回答或未来讨论时，Main 才询问一次：

```text
A. 展开当前 Behavior，并将结果作为认知 Proposal
B. 暂不持久化，基于已有认知 + 代码事实 + 源码回答
```

选择 A 的第一次同意只是授权分析，不是批准未知 Patch。分析完成后展示 Proposal，用户再次明确批准才 apply。完整展开只覆盖当前 Behavior 的 Flow、关键 Capability 和 Mapping，不复制 L4 源码。

选择 B 时立即回答，不写正式认知。本次 CodeCortex 工作流中不重复询问同一 Behavior。Core 不识别 Codex thread；“已询问”由 Skill/Main 在当前对话上下文维护。

已经记录的 A/B 决定先于通用 affected/unknown 路由生效：A 先执行语义同步，再按最新事实/源码回答；B 直接 source-first。只有尚未回答的选择才展示一次询问。

### 10.4 没有 Responsibility/Behavior/Capability 锚点

才进入 Native Codex fallback：`rg`、目录探索、源码、配置、测试和文档。回答后可以建议未来增加认知覆盖，但不能强迫先建图。

“图中没有”不等于“源码不存在”。

## 11. Source-first Degraded Mode

如果相关节点受影响：

```text
旧认知（明确标为 baseline 理解）
→ 当前代码事实
→ 当前相关源码
→ Main/Analyzer 最新语义判断
→ 回答
```

旧图仍用于定位范围和解释变化历史，但不能作为当前实现结论。回答必须指出：哪些结论来自已确认图、哪些来自当前源码、哪些仍不确定。

回答问题不要求先同步正式图。需要保存新结论时才创建 Proposal。

## 12. Bounded Discussion Context

`get_discussion_context` 输入：

```yaml
node_ids: []
depth: 2
max_nodes: 40
max_entities: 80
max_evidence: 80
include_flows: true
include_source_locations: true
```

Core 使用 breadth-first traversal，按 edge type 和稳定 ID 排序。达到任一上限停止并返回：

```yaml
truncated: true
truncation_reasons: [max_entities]
continuation_hints: []
```

Main 选择下一局部，不允许自动把剩余图全部拉入上下文。所有 query tools 同样有 hard limit。

实现的 collection budget 对应关系（每类独立上限，跨所选 owner 累计）：

| Budget | Collections |
| --- | --- |
| `max_nodes` | nodes、semantic edges、aliases、所有 Flow steps、所有 step capability references；Flow 数量最多为选中的 Behavior 数量 |
| `max_entities` | implementation mapping **行数**（即使重复引用同一 entity），以及 distinct entity refs |
| `max_evidence` | 当前保留 owner 的 evidence 总数 |

SQL 在 materialize 前用 `LIMIT cap + 1` 探测溢出；anchor owner expansion 与 BFS
也只 fetch 有界的 distinct nodes。BFS 按 edge type、neighbor stable ID 排序，
edges 按 type/ID，steps 按 Behavior/order/ID，mappings/evidence 按 ID 排序。
未保留的 step/edge/mapping 不会额外展开其 evidence 或 mappings。
`include_flows=false` 不加载 Flow/steps/capability references，也不包含 step mappings
及其 evidence；这属于请求过滤，不单独设置 truncated。缓存将 Flow 顶层 evidence
归属到 Behavior node，因此这些 node evidence 仍保留。

`truncation_reasons` 使用 budget 名或 `budget:collection`（例如
`max_nodes:flow_steps`、`max_entities:mappings`）。`continuation_hints` 给出缩小
node/entity anchors、提高到 configured maximum、或直接读取相关 formal graph/source
文件的建议。兼容 `cursor`/`continuation` 仅编码有限的 omitted node anchors，不是
可提交的分页 cursor，也不保证枚举所有 omitted nodes；child collections 没有分页端点。

`inspect_node` 使用配置的 `query_max_nodes` / `query_max_entities` /
`query_max_evidence` 按上述映射裁剪。正式文件的 snapshot 仍须完整解析以校验坐标；
projection 采用有界选择，只有保留 mappings 才执行源码 resolution。
所有嵌套 owner 共享 evidence allowance，顶层 `evidence` 是其有界索引副本
（因此序列化最多包含两份该 allowance）。裁剪不修改正式 snapshot。

## 13. 回答契约

Skill 要求 Main 按问题需要组织，而非机械模板；但事实性回答应能给出：

- 直接结论；
- 相关 Responsibility/Behavior/Capability；
- 关键 Flow 或实现路径；
- 仓库相对源码位置和行号；
- 当前局部 freshness；
- 重要不确定项。

不强制每个答案都引用认知节点。图外问题按 Native Codex 自然回答。普通问答不自动修改 graph。

## 14. 审批与提醒频率

审批仍只针对正式认知变化。以下不审批：

- Fact Preflight；
- cache 重建；
- 动态 L3/L4 读取；
- transient 源码分析；
- 高置信实体地址更新；
- `no_semantic_change` baseline advance。

pending Proposal 按 ChangeSet 聚合，不按文件或保存次数创建。每次显式 CodeCortex 工作至多汇总提醒一次；用户暂缓后本次不重复。高影响删除/迁移可在自然检查点提醒，但不能打断普通 Codex 工作。

## 15. Cache Recovery

cache 缺失或不匹配时：

1. 读取并校验 manifest、graph、entity refs、source baseline 和 History；
2. 从当前源码全量重建事实 SQLite；
3. 从 graph/history 重建认知查询副本；
4. 恢复正式 entity UID；
5. 计算当前 digest；
6. 与 baseline 相同则 fresh；不同则用 source baseline 生成完整文件差异；
7. validate_graph；
8. 不调用 Agent。

恢复 cache 是确定性操作；此时未被正式引用的历史实体可能无法恢复，因此 ChangeSet 明确标记 `entity_diff_completeness=partial`。判断源码变化是否改变认知是之后按需执行的独立语义任务。

若 revision-0 正式骨架已经存在但 cache 缺失，Main 在显式 full Fact Sync 后只重建 bootstrap replica，不重复 initialize。恢复返回前必须再次探测 live Managed Source digest；发生竞争变化时 fail closed。Analyzer 只使用不创建、不修复的只读 handles，并在打开数据库和 sidecar 前逐级拒绝符号链接、越界路径和非普通文件。`-wal` 与 `-shm` 必须成对存在；WAL-only、孤立 `-shm` 或损坏数据库都返回 `CACHE_REBUILD_REQUIRED`，由 Main 恢复，Analyzer 不得创建 SHM、checkpoint、删除或改写文件。

## 16. M1b MCP 增量

两个 profile 可读：

| 工具 | 结果 |
|---|---|
| `cognitive_freshness()` | repository status、baseline/current digest、ChangeSet 摘要 |
| `pending_changes()` | 当前单一 ChangeSet，有界详情 |
| `effective_query_freshness(node_ids?, entity_ids?)` | 查询级状态和原因 |

仅 Main：

| 工具 | 作用 |
|---|---|
| `advance_cognition_baseline(change_set_id, reason, decision_record, approval_record?)` | 无图修改推进 baseline 并写 Event |

`sync_repository_facts` 由 M1a 提供并在所有 CodeCortex 工作前调用。MCP 不提供 `ask` 工具；回答由 Main Codex 完成。

第 4 节的 `cognition_initialized=true` 门槛适用于本节全部 M1b 工具及普通
认知查询，不因 M1a 首次初始化而放宽。M1a 仅为
`repository_facts`/`analysis_scope` 定义一个受 CacheGuard 约束的 bootstrap
读阶段；它不允许 freshness、graph、search、context 或 discussion 路由绕过
M1b Fact Preflight。该 bootstrap 例外在同一个 repository shared lock 内重读
正式状态并执行实际查询；任何并发初始化完成都会使例外立即失效。

`decision_record` 对 no_semantic_change 保存 decided_by、证据摘要、Analyzer/Main 来源和时间；user_accepted 额外要求与当前 digest 匹配的 approval record。

## 17. 独立 Child Codex 验收空间

测试 harness 创建同一 commit 的两个临时 Git 副本：

```text
eval-run/
├── native/
└── codecortex/
```

普通单轮 Benchmark 分别运行新进程：

```text
codex exec --ephemeral --json <fixed prompt>
```

Native 运行忽略 CodeCortex 用户配置；CodeCortex 运行使用测试 MCP 配置和临时仓库 Skill。两者固定相同模型、reasoning、sandbox、代码 commit、问题和时间预算。不得继承开发本系统的当前对话。

JSONL trace 保存：MCP calls、源码/命令访问、最终回答、turn result、token usage 和耗时。日志清除认证信息和机器绝对路径。

非交互验收专用 MCP 配置把 apply 的 host `approval_mode` 设为 `approve`，避免无法展示新 host prompt 导致命令直接失败；产品安装配置仍为 `prompt`。审批测试单独使用隔离的临时 Codex home，第一轮运行不带 `--ephemeral` 的 `codex exec --json` 并保存 thread ID，验证没有 CodeCortex 对话级明确批准时 graph 不变；再用 `codex exec resume <thread_id>` 发送第二轮批准，检查 Main 生成的 `approval_record` 和 Core 校验，最后清理临时 home。真实 VS Code 则使用产品默认 `prompt` 另做人工 smoke test。测试专用设置不得进入安装资源。

## 18. Benchmark 问题集

至少覆盖：

- 图内且 fresh；
- 图内但只需动态 L3/L4；
- Behavior unmaterialized，用户选 A；
- Behavior unmaterialized，用户选 B；
- affected node source-first；
- repository pending 但查询节点 unaffected；
- unknown scope；
- 完全图外；
- 跨 Responsibility；
- 认知与当前源码冲突；
- cache 删除后的首次查询。

每题保存仓库 commit、问题、预期关键事实、允许证据位置和禁止错误结论。预期答案由源码/测试人工校准，不能只让另一个 LLM 自我打分。

## 19. 评价指标

- 关键事实正确率；
- 源码路径/行号 grounding；
- 关键模块召回；
- stale cognition 误用次数；
- 不确定性是否如实披露；
- 回答可理解性；
- 输入/输出 token；
- 首次回答延迟；
- Analyzer MCP tool-call 次数（trace 没有进程启动事件时不得表述为启动次数）；
- 不必要审批/展开提示次数。

Native 与 CodeCortex 进行盲化人工抽查。产品约束是固定 Benchmark 上图外问题不出现系统性或统计显著退化，不承诺逐题必胜。

## 20. 测试

### 20.1 确定性测试

- 全文件哈希、只解析变化文件；
- baseline→current 单一 ChangeSet 重算；
- source baseline 的稳定排序、manifest 一致性和原子推进；
- cache 删除后精确恢复文件差异，并对不可恢复的旧实体标记 partial；
- mapped/unmapped affected scope；
- 只有满足全部严格条件时 scope confidence 才为 complete；
- 一跳传播边界；
- repository 与 query freshness 组合；
- baseline advance 两种 reason 和审批要求；
- cache recovery 不启动 Agent。

### 20.2 路由测试

- fresh graph context；
- 动态 L3/L4 不误判 fallback；
- materialization A/B 路由；
- 同一工作流不重复提示；
- source-first 不把旧图当当前事实；
- 无锚点才 Native fallback；
- context 截断和 continuation。

### 20.3 真实 Codex

- 对照进程确实独立；
- Main 小范围处理和大范围 Analyzer 委派；
- Analyzer MCP 不暴露写工具，标准 Agent 配置请求 read-only sandbox；
- 问答不自动写图；
- 用户批准前后 revision 行为；
- CodeCortex MCP 失败后 Native 问题仍完成。

## 21. M1b 完成条件

- 每次 CodeCortex 工作能发现 tracked/untracked 手工源码变化；
- Fact Preflight 不调用 Agent；
- 无关节点在 repository pending 时仍可正常使用；
- 受影响节点进入 source-first，不错误信任旧图；
- L3/L4 动态加载继续利用认知图导航；
- 未 materialize 区域提供一次性可选展开，不高频审批；
- 真正图外问题可以回到完整 Native Codex 能力；
- cache 删除/换机器后首次工作自动恢复；
- Child Codex Benchmark 和一次人工 VS Code smoke test 通过。
