# CodeCortex M1a 全分支终审报告（只读 review worker）

- 对象：worktree `.worktrees/codecortex-m1a`，branch `feature/codecortex-m1a`，HEAD `8285872`，`git diff main...HEAD`（101 文件，+16,378/-174），工作树干净（已核实）。
- 方法：按模块通读全部新增/修改源码（domain/application/infrastructure/interfaces/integrations/scripts）与规格文档，抽查测试断言真实性；对可疑点做了两次独立运行时实证（临时目录、不动工作树），并用已批准的 `conda run` 前缀跑了聚焦回归（`test_m1a_apply.py` + `test_transaction_recovery.py` + `test_mcp_profiles.py`：**38 passed**，与基线一致）。
- 规格权威顺序：MVP v0.6.2 → 技术架构 → M1a 详细设计 → 计划 → handoff 最新现场。

## 总体结论：CHANGES_REQUIRED

安全链、事务、路径安全、有界性、文档纪律整体扎实；但发现 **2 个 Critical**，它们共同使"真实 M1a 初始化 apply 之后的全部有界读接口"在生产接线下不可用，恰好落在 opt-in E2E（本机未实跑）的盲区内。两个修复都很小，但必须先修再合并。

---

## Findings

### Critical 1 — 生产组合根未给 `GraphReplica` 接 entity_refs/history_events provider：真实初始化 apply 后读平面永久损坏

- 位置：
  - `src/codecortex/interfaces/cli/main.py:141` — `_default_services` 以 `GraphReplica(cache_directory / "cognitive.sqlite3")` 构造，**不传** `entity_refs=`/`history_events=` provider；
  - 同样缺接线：`scripts/run_m1a_acceptance.py:118`、`tests/integration/test_m1a_mcp_tools.py:62`；
  - 触发点：`src/codecortex/infrastructure/persistence/graph_replica.py` `rebuild()` → `_validated_entity_refs()`（providers 默认为空 tuple，凡是 `graph.entity_uids` 非空即抛 `FORMAL_STATE_CORRUPT`）；
  - 吞没点：`src/codecortex/application/proposals.py:439-446`（`_refresh_caches` 把该异常降级为 `CACHE_REBUILD_REQUIRED` warning）。
- 证据（实证，非推断）：用生产 `_default_services()` 在临时 git 仓库走完整真实链路（init → full sync → `begin_analysis` → 带 `candidate_mappings` 的 AnalysisReport → 批准 → apply）。apply 正式提交成功（revision 1），但 `cache_warnings` 为 `"CACHE_REBUILD_REQUIRED: cognitive replica refresh failed after the formal commit: Formal entity references do not cover the graph's entity UIDs"`；随后 `repository_facts("pkg.core")` 与 `search_cognitive_graph("greet")` 均失败 `CACHE_REBUILD_REQUIRED`。全代码库中除 `ProposalService._refresh_caches` 外**没有第二个 replica rebuild 调用点**，而后者的 replica 对象同样无 provider——即该状态在生产中不可自愈。
- 规格冲突：M1a 设计 §19 完成条件"cache 查询按 scope/limit 完成""inspect 可从认知节点到当前源码"；§7"正式 graph apply 后认知副本在同一仓库锁内刷新"；handoff Task 7 记录的偏差"entity refs/history events 由构造器 provider 在 rebuild 时求值"本身已批准，但组合根从未供给 provider。
- 为何漏网：所有带 mapping 的 apply 集成测试（`tests/conftest.py:90-92`）都显式传了 provider；验收脚本只跑 revision-0 空图的 facts 路径；唯一走真实全链路的 Child Codex E2E 为 opt-in 且本机 skip。
- 修复方向：在 `cli/main.py` 组合点注入从 `FormalStore.load()` 派生 `EntityRefRecord`/`HistoryEventRecord` 的 provider（等价于 conftest `_entity_ref_records` 的生产实现），同步修验收脚本与 MCP 集成测试的 `_compose`；新增一条**用 `_default_services` 真实接线**做"带 entity mapping 的 apply → `repository_facts`/`search_cognitive_graph` 可读"的回归测试。

### Critical 2 — 两套图校验器对 inferred 边的 evidence 要求不一致：无证据 contains 边可正式提交但必然击垮 replica rebuild

- 位置：
  - `src/codecortex/domain/graph.py:432-441`（`_validate_edges`：`_requires_evidence` 对**所有** inferred/uncertain 边生效，含 `contains`）；
  - `src/codecortex/domain/cognition.py:579-597`（`_validate_m1a_graph_invariants`：仅 `uses`/`depends_on` 要求 evidence）；
  - `src/codecortex/application/proposals.py:380`（apply 只跑 `validate_formal_state`，不跑 `validate_cognitive_graph`）；
  - `src/codecortex/domain/analysis.py:720-726`（候选禁止 `established`，`contains` 边只能是 inferred/uncertain）。
- 证据（实证）：同上一链路，但报告里 `contains` 边不带 evidence（Analyzer 的正常输出形态——`analysis.py` 报告校验与 analyzer TOML 均未要求 contains 边带证据）。结果：formal commit 成功（revision 1），replica rebuild 抛"Cognitive graph violates formal invariants and cannot be indexed"被吞为 warning，之后所有 guarded read 永久 `CACHE_REBUILD_REQUIRED`。即即使修好 Critical 1，最常见的 Analyzer 输出仍会踩中本 bug。
- 规格冲突：M1a 设计 §8.2 并未要求结构 contains 边携带 evidence；apply 与索引两侧必须对同一份图达成一致判定，否则"正式真相合法但查询副本永远建不起来"。
- 修复方向：统一两处规则（建议：`contains` 结构边豁免 evidence 要求，或 apply 路径在 `validate_formal_state` 之外同步调用 `validate_cognitive_graph` fail-closed）；加"最小 report（contains 边无 evidence）→ apply → replica rebuild 成功 → 可读"的端到端回归。

### Important 1 — `ApplyResult` 丢弃 `cache_warnings`，cache 失败信号在 CLI/MCP 层完全消失

- 位置：`src/codecortex/application/services.py:391-400`（M1a apply 结果包装为 `ApplyResult(event_id, graph_revision, applied_proposal_id)`，`GraphApplyResult.cache_warnings` 被丢弃）；MCP `apply_cognitive_proposal` 输出也因此无 warning 字段。
- 影响：与 Critical 1/2 叠加时，用户看到的是一次"完全成功"的 apply，直到下一次读才失败，且无任何指向 cache 的信号。违反 §15"SQLite 刷新失败时……cache 下次重建"的可观测意图。
- 修复方向：`ApplyResult` 增加 `cache_warnings: tuple[str, ...]` 并在 MCP 输出 DTO 暴露。

### Important 2 — application 层直接 import infrastructure 具体类，偏离"infrastructure 实现 application ports"

- 位置：`application/proposals.py:68-72`、`application/query.py:40-52`、`application/fact_sync.py:27-40`、`application/services.py:50-52`（导入 `FactsDatabase`/`GraphReplica`/`RepositoryLock`/`view_manifest_for`/discovery/digest 等具体实现）。
- 依据：技术架构 §5"依赖只能向内……infrastructure 实现 application 定义的端口"。`query.py` 虽定义了窄 `FactQueryPort`/`GraphQueryPort` Protocol（好），但仍直接引用基础设施 DTO；`proposals.py` 以具体类型作构造签名。
- 评估：无循环依赖、无安全旁路，domain 纯净性（真正的硬规则）完好（已 grep 全量验证 domain 仅导入 domain 内部与标准库）。判 Important 而非 Critical：**不阻塞本次合并**，但应在 M1b 立项收敛（把 DTO 下沉 domain 或把组合移回 interfaces）。

### Minor 1 — `domain/graph.py:869` 使用 PEP 758 无括号多异常语法

`except TypeError, ValueError:` 仅 Python ≥3.14 合法。`requires-python` 已锁定 `>=3.14,<3.15`，ruff/mypy 现状通过，功能无问题；但任何旧解析器/IDE 会报 SyntaxError（本审查的 system python3 即误报）。零成本建议改回 `except (TypeError, ValueError):`。终审顺手修或留 M1b。

### Minor 2 — AnalysisReport 字节上限作用于 MCP 重序列化后的字节

`interfaces/mcp/tools.py:271-279`：`AnalysisProposalInput.analysis_report` 先被 Pydantic 解析为 dict，再 `json.dumps(sort_keys, compact)` 后才测 512 KiB。重复键在解析期被吞、线上原始字节与计量字节可不同。Core 侧保护目标基本达成，留 M1b。

### Minor 3 — `inspect_node` 的 mapping 解析是 per-mapping 查询循环

`application/query.py:627-664`：每个 mapping 1–2 次 `entity_by_uid`/`resolve_entity_reference` 点查。单节点 mapping 数受报告上限（2000）约束，非典型 N+1 灾难，但与 handoff"无 N+1"表述有差距。留 M1b（可批量 `IN` 查询）。

### Minor 4 — `fact_sync.py:127` 锁超时硬编码 `10`

`self.repository_lock.acquire("exclusive", 10)` 未使用可配置值（其余组件为 `lock_timeout_seconds` 注入）。留 M1b。

### Minor 5 — 生产类内含测试辅助方法

`facts_db.py:1074/1108` `insert_test_entities`/`insert_test_relations` 仅供测试。可留 M1b 或关闭（无危害）。

### Minor 6 — `_checked_relative` 的 root 未先 resolve

`infrastructure/formal.py:1234-1253`：`os.path.commonpath((str(root), str(resolved)))` 中 root 若非真实路径（经 symlink 的仓库根），判定可能不一致。formal root 通常真实，留 M1b。

---

## 审查重点逐项结论

### 1. 安全链完整性 — 无发现（安全方向）

- Analyzer profile 无任何写路径：`server.py` 注册层静态白名单（Analyzer 无 `sync_repository_facts`/`create_*`/`apply_*`），`test_mcp_profiles.py` 有"Unknown tool"对抗用例，doctor `_profile_allowlist_check` 与码表一致；analyzer TOML `sandbox_mode = "read-only"` 且 `_recover_main_formal_state` 对 analyzer 不执行恢复写。
- Proposal→批准→apply 不可绕过：`domain/proposals.py:414-445` 校验 status、`canonical_patch_digest(operations) == patch_digest`、approval 绑定当前 proposal_id+patch_digest、`approved_by == "user"`。
- 源码/revision 变化 fail-closed：`apply_cognitive_proposal` 在独占锁内用 `source_probe` 重算全仓库 digest + 逐文件 preconditions，任一不符抛 `PROPOSAL_STALE`；对抗测试 `test_any_source_change_makes_the_proposal_stale`/`test_unapproved_apply_writes_nothing` 以正式文件字节指纹断言零写入；我的实证同样确认 stale/approval 拒绝先于任何写。
- formal 事务：staging+fsync→备份+journal→按 `event→graph→entity_refs→source_baseline→views` 顺序替换→**manifest 最后**→目录 fsync；recovery 只回滚到可证明的旧/新 revision，journal 损坏或 revision 两边不靠抛 `FORMAL_STATE_CORRUPT` 且不猜测修复；fault-injection 覆盖全部 7 个 stage + 恢复幂等 + manifest 后崩溃只清理不回滚。
- History 不可变：`formal.py:321-330` 拒绝覆盖已存在 event 文件；`_validate_applied_event` 要求自包含 proposal 快照。
- Critical 1/2 影响的是**读平面可用性**，不是安全链：正式提交本身始终正确、可恢复。

### 2. 分层纪律 — 1 个 Important（见 Important 2）

- domain 层纯净：全量 grep 确认 `domain/` 无 infrastructure/MCP/Pydantic/SQLite/Codex 导入。
- interfaces/integrations → application 方向正确；application → infrastructure 具体类的直接依赖见 Important 2。

### 3. 路径与持久化安全 — 无关键发现

- `domain/graph.py::_valid_relative_path` 与 `domain/facts.py::_normalized_relative_path` 拒绝绝对路径、`\`、`..`、非规范形态；discovery `_git_paths` 对 git 输出做同样校验，`_source_input` resolve+commonpath 隔离仓库外 symlink（仅隔离该候选 + diagnostic，符合 §2.1）。
- `_checked_relative` resolve+commonpath 防 journal/manifest 路径逃逸（Minor 6 除外）；view manifest 读取校验常规文件、非 symlink、路径集合精确相等、排序、逐字节 digest；M0 legacy view 只在 `verify_legacy_views` 通过后才允许首次 M1a 迁移，读请求不做隐式迁移——与 §16 一致。
- canonical JSON：`jsonio.canonical_json_bytes` key 排序、UTF-8；`_unique_json_object` 拒绝重复键；所有正式写 `_write_staged`/`_write_bytes_atomic` + fsync。

### 4. 有界性 — 无关键发现

- 所有列表接口：`LIMIT ?+1` keyset 分页（cursor 编码带 kind 校验）、`max_page_size=100`/`max_limit=100`、relation UID 批量上限 200、context depth/node/entity/evidence/anchor 上限（10/200/500/500/100）、搜索 `GROUP BY`+确定性 tie-break+强制 limit。
- `CacheGuard.require_current` 三方（formal revision、facts metadata、replica metadata）+ caller pin 校验，任何不匹配 fail-closed `CACHE_REBUILD_REQUIRED`，不返回混合快照——符合 §5。
- AnalysisReport 上限 512KiB/300/1000/2000/2000/240 code points 与 §10、analyzer TOML 一致，且字节上限先于数量上限、新鲜度短路先于候选校验；`cognitive_graph()` 兼容入口有 500 对象上限并抛出带指引的 `CONTEXT_LIMIT_EXCEEDED`——符合 §17。
- 例外：Minor 2、Minor 3。

### 5. 测试质量 — 1 个结构性盲区（即两个 Critical 的成因）

- 断言真实观察行为：stale/未批准 apply 断言**正式文件字节指纹不变**且 events 目录为空；恢复测试逐 stage fault injection；search/context 断言确定性排序与 truncation；`test_mcp_profiles.py` 断言 Analyzer 调写工具得 "Unknown tool"。
- 关键不变量均有对抗用例：stale source、dangling ref（`recompute_entity_refs` 未知 UID 拒绝、`DANGLING_*` 域校验）、崩溃恢复矩阵、manifest/journal 篡改。
- **盲区**：没有任何测试用生产 `_default_services()` 组合跑"带 entity mapping 的 analysis apply → 随后 guarded read"。验收脚本自称"through the production service wiring"但只覆盖 facts/空图。opt-in E2E（唯一能抓到的网）本机未实跑。终审必须补这条回归（见 Critical 1 修复方向）。

### 6. 遗留 deferred 项逐条裁决建议

| 来源 | 事项 | 终审裁决建议 |
|---|---|---|
| Task 5 | Flow capability / Mapping entity UID 的 valid-namespace dangling 专项回归 | **可留 M1b**（邻近路径已被 `DANGLING_EVIDENCE_ENTITY`、mapping subject/entity 域校验与 `test_graph_constraints.py` 覆盖；非阻断） |
| Task 6 | pending save/delete adversarial parent-swap | **可关闭**（`test_pending_final_path_rejects_symlink_escape_for_every_operation` + `test_pending_load_anchors_parent_directory_against_replacement` 已覆盖 load/save/delete 面） |
| Task 7 | duplicate history event ID 复用 `ANALYSIS_REPORT_INVALID`（formal commit）/ `FORMAL_STATE_CORRUPT`（replica） | **可关闭**（两条路径均 fail-closed，码表语义可接受） |
| Task 7 | `formal.py` ~1290 行接近拆分点 | **可留 M1b**（结构债，非缺陷） |
| Task 8 | `--version <command>` 静默忽略命令 | **建议终审必须修**（一行 usage-error 判断；cli 是用户门面） |
| Task 8 | integration fixture 缺 `pending_proposals` | **可关闭**（`test_apply_transaction.py:41` 等已按生产 `_default_services` 补齐） |
| Task 8 | `mcp --profile` 缺 argparse `help=` | **可留 M1b**（HEAD 确认仍未补，`cli/main.py:212`） |
| Task 12 | 真实 Child Codex E2E 仅 opt-in，本机未实跑 | **保持 open；合并前强烈建议实跑一次**——它正是能抓住本报告 Critical 1/2 的那张网 |
| Task 12 | reinitialize 全局 diff 分类仅有 SKILL.md 契约、无模型实跑记录 | **可留 M1b**（契约为 prompt 层，MVP 范围可接受；但应在 M1b 首批评测中覆盖） |

### 7. 文档一致性 — 1 处需修正

- SKILL.md（init/reinitialize 编排、current patch_digest 显式批准、禁止用无界 `cognitive_graph`）与代码实际行为一致；doctor allowlist 与 `server.py` 两个工具集合一致且有测试锁定；MVP/M1a 设计文档的 §2.3 revision 语义、`view_manifest.json`、§17 上限条款与实现一致。
- **需修正**：`docs/testing/M1A_ACCEPTANCE.md` 的"M1a completion criteria status"声称验收覆盖到"生产服务接线"与"cache 删除后恢复正式认知"，但脚本实际只覆盖 facts 与 revision-0 空图 replica；在 Critical 1/2 修复并补生产接线回归后，应更新该文档的覆盖范围表述（或扩展脚本做一次带 mapping 的 apply+读）。

---

## 复审建议的最小修复包

1. `cli/main.py`（+验收脚本+MCP 测试 compose）给 `GraphReplica` 接 provider；新增 `_default_services` 全链路回归（Critical 1）。
2. 统一 contains 边 evidence 规则（建议结构边豁免）或 apply 时同步跑 `validate_cognitive_graph`（Critical 2）。
3. `ApplyResult`/MCP apply 输出透出 `cache_warnings`（Important 1）。
4. `graph.py:869` 括号化（Minor 1）；`--version` 带命令时报 usage error（Task 8 deferred）。
5. 条件齐备时实跑 opt-in E2E；更新 `M1A_ACCEPTANCE.md` 覆盖表述。

完成 1–3 并回归后，本审查可转为 APPROVED_WITH_MINORS。

---

## 复审结论（Main 复审，2026-09-04）

复审方式：Main 对 FIX 轮提交 `12ef197 fix: close review-critical replica wiring and evidence parity` 逐条核对本报告全部 finding 与最小修复包，并独立重跑全量门禁。

### 逐条裁决

| Finding | 裁决 | 证据 |
|---|---|---|
| Critical 1 — 生产组合根未接 GraphReplica provider | **RESOLVED** | 新增 `application/replica_providers.py`（从 `FormalStore.load()` 派生 entity_refs/history_events provider）；`cli/main.py` 组合根改用 `GraphReplica.create_new` 并接 provider；`initialize_repository` Main 写路径重建 replica；`scripts/run_m1a_acceptance.py` 与 `tests/integration/test_m1a_mcp_tools.py` compose 同步修正。 |
| Critical 2 — 两套图校验器 evidence 规则不一致 | **RESOLVED** | 按建议方向统一：结构 `contains` 边在两处校验器均豁免 evidence（§8.2），`uses`/`depends_on` inferred/uncertain 仍 fail-closed。 |
| Important 1 — `ApplyResult` 丢弃 cache_warnings | **RESOLVED** | `ApplyResult.cache_warnings: tuple[str, ...]` 新增，MCP apply 输出 DTO 同步透出。 |
| Important 2 — application 直 import infrastructure 具体类 | **DEFERRED → M1b** | 分层收敛留 M1b，已记录于 handoff 遗留清单。 |
| Minor 1 — PEP 758 裸 except tuple | **RESOLVED** | `graph.py:869` 已括号化为 `except (TypeError, ValueError):`。 |
| Minor 2/3/4/5/6 | **DEFERRED → M1b** | 已记录于 handoff 遗留清单。 |
| 顺手项 — `--version <command>` 静默忽略（Task 8 deferred） | **RESOLVED** | 现为 usage error，含 CLI 单测锁定。 |
| §7 文档一致性 — M1A_ACCEPTANCE 覆盖表述 | **RESOLVED** | 验收脚本已扩展为经 `_default_services` 的完整链路（analysis-backed apply + 读后断言零 cache warning），文档表述据实更新。 |
| §5 测试质量 — 生产接线结构性盲区 | **RESOLVED** | 新增 `tests/integration/test_default_services_chain.py`：带 entity mapping 的 apply → 可读、无证据 contains 边 apply → 可读，两条均走生产 `_default_services` 并断言 `cache_warnings == ()`。 |

### 复审门禁

- 全量 pytest：`483 passed, 3 skipped`（escalated `conda run`）。
- `ruff check src tests scripts` 通过；`mypy src` 47 文件无问题；`python -m build` 通过；`git diff --check` 干净。
- 验收脚本 `/tmp/codecortex-m1a-final2`：9/9 PASS（260 文件冻结 pytest 仓库，含 `analysis_backed_apply_and_reads`：revision 1、零 cache warning、读可读）。

### 总结论

**APPROVED_WITH_MINORS**。2 个 Critical 与 1 个 Important 全部修复并回归锁定；剩余 Important 2 与 Minor 2–6 经裁决留 M1b。合并前唯一强烈建议事项为真实 Child Codex E2E 的 opt-in 实跑。

### 后续闭环记录（2026-09-04，FIX 轮 2）

上述唯一开放建议已闭环：真实 Child Codex E2E 实跑通过（commit `5809bc9 test: complete real Codex M1a acceptance`，833s，1 passed）。该轮实证并修复两个实跑根因（`codex exec` approval policy `never` 与 MCP 工具默认 prompt 模式冲突；Analyzer 提示层缺 M1a 边矩阵/边级 evidence 规则导致首轮提案被 Core fail-closed 拒绝），测试改为真实两轮批准加有界重试循环，详见 `docs/testing/M1A_ACCEPTANCE.md` 的 2026-09-04 记录行。
