# CodeCortex M1b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the M1a graph into persistent, locally trustworthy project understanding that reconciles source facts before every CodeCortex operation, degrades only affected queries, supports graph-guided discussion and optional materialization, recovers after cache loss, and demonstrates non-regression against Native Codex.

**Architecture:** Every explicit CodeCortex entry runs deterministic Fact Preflight and computes one baseline-to-current ChangeSet. Query-level freshness combines its conservative affected scope with graph-guided retrieval; semantic synchronization remains an on-demand Main/Analyzer responsibility and changes formal cognition only through Proposal approval or a separately audited baseline-advance event.

**Tech Stack:** Completed M0/M1a stack, SQLite fact/baseline snapshots, deterministic source digests, Codex Skill routing, subprocess-based isolated `codex exec` evaluation, JSONL trace parsing, pytest benchmark fixtures.

**Spec:** `docs/CodeCortex_M1b_Detailed_Design.md`, `docs/CodeCortex_Technical_Architecture.md`, and `docs/CodeTree_Understanding_MVP.md` v0.6.2.

## Global Constraints

- Start only after the complete M1a gate passes on a clean branch.
- Every M1b operation requires manifest `cognition_initialized=true`; otherwise return `NOT_INITIALIZED` even if graph revision is positive.
- No watcher or ordinary-Codex task hook: every explicit `$codecortex` operation begins with Fact Preflight.
- Fact Preflight and affected-scope calculation are deterministic Core code and never call Main/Analyzer.
- Source is the final implementation truth; pending repository status does not invalidate unaffected nodes.
- Maintain exactly one cache ChangeSet from formal cognition baseline to current source, replacing the prior effective ChangeSet.
- File diff remains complete after cache deletion through formal `source_baseline.json`; entity diff may be partial and must say so.
- `scope_confidence=complete` is allowed only under every strict proof condition in M1b section 6.
- Question answering does not automatically update formal cognition. Materialization and semantic changes require one displayed Proposal and explicit user approval.
- A declined materialization prompt is not repeated in the same invocation.
- Graph absence never limits Native Codex source search; fallback remains fully available.
- Each task is test-first, independently reviewable, and ends in a focused commit.
- Test helpers named in examples live beside their tests or in `tests/conftest.py`; freshness/recovery helpers must mutate real temporary repositories and formal/cache files, while only Codex process boundaries may be represented by a test double in unit tests.

---

## Planned File Map

```text
src/codecortex/domain/freshness.py             ChangeSet and repository/query freshness models
src/codecortex/application/change_detection.py baseline-to-current file/entity diff
src/codecortex/application/affected_scope.py   deterministic graph/fact impact propagation
src/codecortex/application/preflight.py        mandatory entry preflight orchestration
src/codecortex/application/freshness.py        effective query freshness
src/codecortex/application/baseline.py         no-graph-change baseline advance
src/codecortex/application/discussion.py       graph-guided query/context plans
src/codecortex/application/recovery.py         deterministic cache reconstruction
src/codecortex/infrastructure/persistence/freshness.py
src/codecortex/interfaces/mcp/tools.py          M1b freshness tools
src/codecortex/integrations/codex/resources/SKILL.md
tests/fixtures/m1b_repo/                        mutable freshness/discussion fixture
tests/benchmark/questions.yaml                  frozen question and evidence oracle
tests/e2e/test_codex_m1b.py
scripts/run_codecortex_benchmark.py
docs/testing/M1B_ACCEPTANCE.md
tests/conftest.py                               real repository/formal/cache fixtures inherited from M1a
```

## Task 1: Compute One Baseline-to-Current ChangeSet

**Files:**
- Create: `src/codecortex/domain/freshness.py`
- Create: `src/codecortex/application/change_detection.py`
- Create: `src/codecortex/infrastructure/persistence/freshness.py`
- Create: `tests/unit/application/test_change_detection.py`
- Create: `tests/integration/test_change_set_persistence.py`

**Interfaces:**
- Produces: `ChangeSet`, `FileChanges`, `EntityChanges`, `DiffCompleteness`
- Produces: `ChangeDetector.detect(formal: FormalState, facts: FactsDatabase) -> ChangeSet | None`
- Produces: `FreshnessStore.replace_effective(change_set: ChangeSet | None) -> None`

- [ ] **Step 1: Write failing file/entity diff tests**

```python
def test_change_set_is_always_baseline_to_current(change_detector, repo):
    repo.write("a.py", "x = 2\n")
    first = change_detector.detect_current()
    repo.write("a.py", "x = 3\n")
    second = change_detector.detect_current()
    assert first.baseline_source_digest == second.baseline_source_digest
    assert second.current_source_digest == repo.current_digest()
    assert second.changed_files.modified == ("a.py",)


def test_unique_same_digest_pair_is_rename(change_detector, repo):
    repo.rename("old.py", "new.py", preserve_content=True)
    result = change_detector.detect_current()
    assert result.changed_files.renamed == (("old.py", "new.py"),)
    assert result.changed_files.added == result.changed_files.deleted == ()
```

- [ ] **Step 2: Run tests and confirm missing freshness domain**

Run: `pytest tests/unit/application/test_change_detection.py tests/integration/test_change_set_persistence.py -q`

Expected: collection fails on missing `ChangeSet` or detector.

- [ ] **Step 3: Implement source-baseline comparison and atomic replacement**

Validate manifest/source-baseline digest/profile/rule versions. Compare sorted path/digest maps for added, modified, deleted; classify rename only for a unique deleted/added identical digest pair. Compare current entities to `baseline_entity_snapshots`; use completeness from cache metadata. Generate `chg_` ID from stable baseline/current inputs plus creation metadata, store one JSON file under `.cache/change_sets/`, and atomically remove/replace the prior effective file referenced by `freshness.json`. If digests match, remove effective ChangeSet and return `None`.

```python
added = tuple(sorted(current_paths - baseline_paths))
deleted = tuple(sorted(baseline_paths - current_paths))
modified = tuple(sorted(path for path in current_paths & baseline_paths if current[path] != baseline[path]))
renamed, added, deleted = classify_unique_digest_renames(added, deleted, current, baseline)
```

- [ ] **Step 4: Run no-op, repeated-change, revert, rename, and partial-entity cases**

Run: `pytest tests/unit/application/test_change_detection.py tests/integration/test_change_set_persistence.py -q`

Expected: a source revert removes the ChangeSet; non-unique same-digest pairs remain added/deleted; cache-recovered snapshots yield `entity_diff_completeness=partial`; no stale ChangeSet is mixed with a new current digest.

- [ ] **Step 5: Commit ChangeSet detection**

```bash
git add src/codecortex/domain/freshness.py src/codecortex/application/change_detection.py src/codecortex/infrastructure/persistence/freshness.py tests
git commit -m "feat: compute baseline source changes"
```

## Task 2: Calculate Conservative Affected Cognitive Scope

**Files:**
- Create: `src/codecortex/application/affected_scope.py`
- Modify: `src/codecortex/domain/freshness.py`
- Create: `tests/unit/application/test_affected_scope.py`
- Create: `tests/integration/test_affected_scope_queries.py`

**Interfaces:**
- Produces: `AffectedScope`, `UnmappedChange`, `ScopeConfidence`
- Produces: `AffectedScopeCalculator.calculate(change_set: ChangeSet) -> AffectedScope`

- [ ] **Step 1: Write failing propagation and confidence tests**

```python
def test_flow_step_propagates_to_behavior_and_parent(scope_fixture):
    result = scope_fixture.change_entity_mapped_to_flow_step("behavior.train#step.save")
    assert result.affected_flows == ("behavior.train",)
    assert {"behavior.train", "responsibility.training"} <= set(result.affected_nodes)


def test_unmapped_new_file_forces_partial(scope_fixture):
    result = scope_fixture.add_unmapped_file("src/new_feature.py")
    assert result.scope_confidence == "partial"
    assert result.unmapped_changes[0].relative_path == "src/new_feature.py"
```

- [ ] **Step 2: Run tests and confirm missing calculator**

Run: `pytest tests/unit/application/test_affected_scope.py tests/integration/test_affected_scope_queries.py -q`

Expected: collection fails on `AffectedScopeCalculator`.

- [ ] **Step 3: Implement the exact eight-stage bounded algorithm**

Resolve changed/missing entities to formal mappings and evidence; propagate Flow step→Behavior→single Responsibility, Capability→direct using Behaviors, then at most one hop over resolved local imports/inherits/calls. Include parse diagnostics, dynamic/unresolved relations, and changes without formal mapping. Mark complete only when every changed/deleted file is identified and parsed, each change is formally connected or rule-proven irrelevant, no relevant unresolved relation remains, and bounded propagation has no unowned result. Otherwise emit concrete unmapped items and partial/unknown; never infer semantic change.

```text
changed_entities -> direct_mappings_and_evidence
                 -> flow_step_to_behavior
                 -> behavior_to_responsibility
                 -> capability_to_direct_users
                 -> one_resolved_dependency_hop
                 -> collect_unmapped_and_diagnostics
                 -> strict_confidence_decision
```

- [ ] **Step 4: Run complete/partial/unknown decision table**

Run: `pytest tests/unit/application/test_affected_scope.py tests/integration/test_affected_scope_queries.py -q`

Expected: mapped edit can be complete; parse error/unresolved relevant relation is partial or unknown; baseline entity snapshot partial only permits complete when formal refs and current facts prove every changed item; propagation stops after one dependency hop.

- [ ] **Step 5: Commit affected scope**

```bash
git add src/codecortex/application/affected_scope.py src/codecortex/domain/freshness.py tests
git commit -m "feat: calculate affected cognitive scope"
```

## Task 3: Derive Repository and Query-Level Freshness

**Files:**
- Create: `src/codecortex/application/freshness.py`
- Create: `tests/unit/application/test_effective_freshness.py`

**Interfaces:**
- Produces: `RepositoryCognitionStatus = fresh|pending|unresolved`
- Produces: `EffectiveQueryFreshness = current|unaffected_current|affected_source_first|unknown_source_first`
- Produces: `FreshnessService.for_query(node_ids: Sequence[str], entity_ids: Sequence[str]) -> QueryFreshnessResult`

- [ ] **Step 1: Write failing status-combination tests**

```python
@pytest.mark.parametrize(
    ("change_set", "nodes", "expected"),
    [
        (None, ("behavior.deploy",), "current"),
        (complete_scope("behavior.train"), ("behavior.deploy",), "unaffected_current"),
        (complete_scope("behavior.train"), ("behavior.train",), "affected_source_first"),
        (partial_scope("src/unknown.py"), ("behavior.deploy",), "unknown_source_first"),
    ],
)
def test_effective_query_freshness(freshness_service, change_set, nodes, expected):
    freshness_service.set_change_set(change_set)
    assert freshness_service.for_query(nodes, ()).status == expected
```

- [ ] **Step 2: Run tests and confirm missing service**

Run: `pytest tests/unit/application/test_effective_freshness.py -q`

Expected: collection fails on freshness service types.

- [ ] **Step 3: Implement transparent freshness decisions**

Return repository summary plus query result, matched affected nodes/entities, unmapped changes, confidence, baseline/current digests, and human-readable reason codes. A complete non-intersecting scope is unaffected-current; any intersection is source-first; partial/unknown that might be relevant is unknown-source-first. Do not persist per-node freshness.

```python
if change_set is None:
    status = "current"
elif query_scope.intersects(change_set.affected_scope):
    status = "affected_source_first"
elif change_set.scope_confidence == "complete":
    status = "unaffected_current"
else:
    status = "unknown_source_first"
```

- [ ] **Step 4: Run the complete decision matrix**

Run: `pytest tests/unit/application/test_effective_freshness.py -q`

Expected: all repository/query combinations pass and every result contains an auditable reason.

- [ ] **Step 5: Commit freshness derivation**

```bash
git add src/codecortex/application/freshness.py tests/unit/application/test_effective_freshness.py
git commit -m "feat: derive query-level cognition freshness"
```

## Task 4: Make Fact Preflight Mandatory and Idempotent

**Files:**
- Create: `src/codecortex/application/preflight.py`
- Modify: `src/codecortex/application/services.py`
- Create: `tests/integration/test_preflight.py`

**Interfaces:**
- Produces: `PreflightService.run() -> PreflightResult`
- Produces: `PreflightResult(fact_sync, change_set, repository_status)`

- [ ] **Step 1: Write failing operation-entry tests**

```python
@pytest.mark.parametrize("operation", ["ask", "inspect", "sync", "reinitialize", "expand"])
def test_every_codecortex_operation_runs_preflight_first(operation, traced_services):
    traced_services.invoke(operation)
    assert traced_services.calls[0] == "preflight"


def test_preflight_does_not_dispatch_an_agent(preflight, agent_spy):
    preflight.run()
    assert agent_spy.calls == []


def test_preflight_rejects_m0_technical_state(preflight):
    preflight.manifest.cognition_initialized = False
    with pytest.raises(CodeCortexError) as exc:
        preflight.run()
    assert exc.value.code is ErrorCode.NOT_INITIALIZED
```

- [ ] **Step 2: Run tests and confirm routes bypass preflight**

Run: `pytest tests/integration/test_preflight.py -q`

Expected: operation ordering assertions fail.

- [ ] **Step 3: Implement fixed preflight orchestration**

Under application control: enumerate/hash all Managed Source files, run Fact Sync, detect baseline-to-current changes, calculate affected scope, atomically replace freshness cache, and return summary. If source changes during preflight, retry the full deterministic sequence. On partial parse/index failure, never report fresh; include failed files and allow only directly verifiable source-first work.

```python
def run(self) -> PreflightResult:
    self.require_cognition_initialized()
    synced = self.fact_sync.sync("auto")
    change_set = self.change_detector.detect(self.formal_store.load(), self.facts)
    scoped = None if change_set is None else change_set.with_scope(self.scope_calculator.calculate(change_set))
    self.freshness_store.replace_effective(scoped)
    return PreflightResult.from_state(synced, scoped)
```

- [ ] **Step 4: Run idempotency, source-race, and failure tests**

Run: `pytest tests/integration/test_preflight.py -q`

Expected: unchanged repeated preflight leaves generation/ChangeSet stable; every operation starts after successful preflight; agent spy stays empty; parse failure cannot produce fresh.

- [ ] **Step 5: Commit mandatory preflight**

```bash
git add src/codecortex/application/preflight.py src/codecortex/application/services.py tests/integration/test_preflight.py
git commit -m "feat: reconcile facts before CodeCortex work"
```

## Task 5: Expose M1b Freshness MCP Tools

**Files:**
- Modify: `src/codecortex/interfaces/mcp/tools.py`
- Create: `tests/integration/test_m1b_mcp_tools.py`

**Interfaces:**
- Adds both profiles: `cognitive_freshness`, `pending_changes`, `effective_query_freshness`
- Adds Main-only later in Task 6: `advance_cognition_baseline`

- [ ] **Step 1: Write failing profile and payload tests**

```python
@pytest.mark.anyio
async def test_freshness_tools_are_readable_by_both_profiles(mcp_profiles):
    expected = {"cognitive_freshness", "pending_changes", "effective_query_freshness"}
    assert expected <= await mcp_profiles.names("main")
    assert expected <= await mcp_profiles.names("analyzer")


@pytest.mark.anyio
async def test_pending_changes_is_bounded(mcp_client, large_change_set):
    result = await mcp_client.call("pending_changes", {"cursor": None, "limit": 20})
    assert len(result["changed_entities"]) <= 20
    assert result["truncated"] is True
```

- [ ] **Step 2: Run tests and confirm missing tools**

Run: `pytest tests/integration/test_m1b_mcp_tools.py -q`

Expected: tool-list assertions fail.

- [ ] **Step 3: Register bounded read DTOs**

Return schema version, repository status, baseline/current digest, completeness fields, affected scope, diagnostics, reason codes, cursor, and truncation. Inputs use explicit maximum limits. Keep the analyzer profile free of sync, Proposal, apply, and baseline-advance tools.

```python
READ_TOOLS_M1B = {
    "cognitive_freshness",
    "pending_changes",
    "effective_query_freshness",
}
MAIN_ONLY_M1B = {"advance_cognition_baseline"}
```

- [ ] **Step 4: Run protocol/profile tests**

Run: `pytest tests/integration/test_m1b_mcp_tools.py tests/unit/interfaces/test_mcp_profiles.py -q`

Expected: both read profiles expose exact new tools, responses are bounded, and Analyzer writes remain absent.

- [ ] **Step 5: Commit freshness MCP APIs**

```bash
git add src/codecortex/interfaces/mcp/tools.py tests/integration/test_m1b_mcp_tools.py tests/unit/interfaces/test_mcp_profiles.py
git commit -m "feat: expose cognition freshness tools"
```

## Task 6: Advance the Cognition Baseline Without a Graph Change

**Files:**
- Create: `src/codecortex/application/baseline.py`
- Modify: `src/codecortex/infrastructure/formal.py`
- Modify: `src/codecortex/infrastructure/persistence/facts_db.py`
- Modify: `src/codecortex/interfaces/mcp/tools.py`
- Create: `tests/unit/application/test_baseline_advance.py`
- Create: `tests/integration/test_baseline_transaction.py`

**Interfaces:**
- Produces: `advance_cognition_baseline(change_set_id: str, reason: Literal["no_semantic_change", "user_accepted"], decision_record: DecisionRecord, approval_record: ApprovalRecord | None) -> BaselineAdvanceResult`

- [ ] **Step 1: Write failing reason/approval and transaction tests**

```python
def test_no_semantic_change_needs_decision_but_not_user_approval(baseline_service, current_change_set):
    result = baseline_service.advance(current_change_set.id, "no_semantic_change", analyzer_decision(), None)
    assert result.event.reason == "no_semantic_change"


def test_user_accepted_requires_matching_approval(baseline_service, current_change_set):
    with pytest.raises(CodeCortexError) as exc:
        baseline_service.advance(current_change_set.id, "user_accepted", main_decision(), None)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
```

- [ ] **Step 2: Run tests and confirm baseline advance is absent**

Run: `pytest tests/unit/application/test_baseline_advance.py tests/integration/test_baseline_transaction.py -q`

Expected: collection or MCP-list failure.

- [ ] **Step 3: Implement audited baseline advance**

Acquire exclusive lock; rerun Fact Sync; require ChangeSet ID and current digest match; validate `decision_record` source, evidence summary, actor, and UTC time. `no_semantic_change` accepts Main/Analyzer decision without user approval; `user_accepted` requires approval bound to current ChangeSet digest. Write immutable `cognition_baseline_advanced` Event, manifest baseline, sorted source baseline, high-confidence current addresses for already-referenced entities, and unchanged graph revision in one formal transaction; refresh baseline entity snapshots from current entities as complete; remove effective ChangeSet only after success. Ambiguous/missing entity refs retain their prior last-known address and resolution status.

```python
if reason == "user_accepted":
    verify_baseline_approval(change_set, approval_record)
elif reason != "no_semantic_change":
    raise CodeCortexError(ErrorCode.APPROVAL_MISMATCH, "Unsupported baseline reason")
formal_store.commit_baseline_advance(event, manifest, source_baseline, refreshed_entity_refs)
```

- [ ] **Step 4: Run both reasons and every fault injection point**

Run: `pytest tests/unit/application/test_baseline_advance.py tests/integration/test_baseline_transaction.py tests/integration/test_transaction_recovery.py -q`

Expected: stale ID/digest and missing/mismatched approval fail without writes; crash exposes old or new complete baseline; History is self-contained after cache deletion.

- [ ] **Step 5: Commit baseline advance**

```bash
git add src/codecortex/application/baseline.py src/codecortex/infrastructure src/codecortex/interfaces/mcp/tools.py tests
git commit -m "feat: advance cognition baseline with audit history"
```

## Task 7: Build Graph-Guided Discussion Plans and Source-First Routing

**Files:**
- Create: `src/codecortex/application/discussion.py`
- Create: `tests/unit/application/test_discussion_routing.py`
- Create: `tests/integration/test_discussion_context.py`

**Interfaces:**
- Produces: `DiscussionRoute = graph_current|graph_unaffected|source_first|native_fallback|offer_materialization`
- Produces: `DiscussionPlanner.plan(question_scope: QuestionScope, candidates: Sequence[GraphHit], invocation: InvocationState) -> DiscussionPlan`

- [ ] **Step 1: Write failing routing table tests**

```python
@pytest.mark.parametrize(
    ("anchors", "freshness", "flow", "expected"),
    [
        (("behavior.deploy",), "current", "materialized", "graph_current"),
        (("behavior.deploy",), "unaffected_current", "materialized", "graph_unaffected"),
        (("behavior.train",), "affected_source_first", "materialized", "source_first"),
        ((), "current", None, "native_fallback"),
        (("behavior.evaluate",), "current", "unmaterialized", "offer_materialization"),
    ],
)
def test_discussion_routes(planner, anchors, freshness, flow, expected):
    assert planner.plan_for_test(anchors, freshness, flow).route == expected


def test_follow_up_reuses_only_current_invocation_context(planner, invocation):
    planner.plan_for_test(("behavior.deploy",), "current", "materialized", invocation=invocation)
    assert planner.plan_follow_up("what calls it?", invocation).anchor_ids == ("behavior.deploy",)
    assert invocation.serialized_chat_history is None
```

- [ ] **Step 2: Run tests and confirm planner is absent**

Run: `pytest tests/unit/application/test_discussion_routing.py tests/integration/test_discussion_context.py -q`

Expected: collection fails on `DiscussionPlanner`.

- [ ] **Step 3: Implement graph guidance without constraining Native Codex**

Search graph for candidate anchors; Main confirms semantic relevance; request effective freshness; pull bounded node/flow/mapping/evidence context and current source entities. Dynamic L3/L4 loading from mappings remains graph-guided, not fallback. Affected/unknown uses old cognition only as labeled baseline navigation and requires current source. No anchor returns a plan explicitly permitting unrestricted Native Codex `rg`, directory, source, config, test, and documentation exploration. Follow-ups reuse only Main's live invocation context; Core and `.codecortex/` never persist full chat history.

```python
if not confirmed_anchor_ids:
    return DiscussionPlan(route="native_fallback", native_search_unrestricted=True)
freshness = self.freshness.for_query(confirmed_anchor_ids, entity_ids)
if freshness.status in {"affected_source_first", "unknown_source_first"}:
    return DiscussionPlan.source_first(confirmed_anchor_ids, freshness)
return DiscussionPlan.graph_guided(confirmed_anchor_ids, freshness)
```

- [ ] **Step 4: Run graph/partial/affected/no-anchor context tests**

Run: `pytest tests/unit/application/test_discussion_routing.py tests/integration/test_discussion_context.py -q`

Expected: stale graph is never represented as current; dynamic L3/L4 retains graph anchors; no-anchor plans contain no path/search restriction; all context budgets truncate explicitly.

- [ ] **Step 5: Commit discussion planning**

```bash
git add src/codecortex/application/discussion.py tests
git commit -m "feat: route graph-guided project discussions"
```

## Task 8: Implement One-Time Materialization Choice and Semantic Sync Policy

**Files:**
- Modify: `src/codecortex/integrations/codex/resources/SKILL.md`
- Modify: `src/codecortex/application/discussion.py`
- Create: `tests/unit/application/test_invocation_state.py`
- Create: `tests/e2e/test_materialization_choice.py`

**Interfaces:**
- Produces: `InvocationState.record_materialization_decision(behavior_id: str, decision: Literal["expand", "transient"])`
- Consumes existing Proposal/Analyzer paths; creates no new formal write API

- [ ] **Step 1: Write failing A/B and no-repeat tests**

```python
def test_declined_materialization_is_not_offered_twice(planner, invocation):
    first = planner.offer_for("behavior.evaluate", invocation)
    assert first.should_ask_user is True
    invocation.record_materialization_decision("behavior.evaluate", "transient")
    second = planner.offer_for("behavior.evaluate", invocation)
    assert second.should_ask_user is False
    assert second.route == "source_first"


def test_pending_proposal_is_summarized_once_per_invocation(planner, invocation):
    assert planner.pending_summary(invocation).should_notify is True
    invocation.record_pending_summary_shown()
    assert planner.pending_summary(invocation).should_notify is False
```

- [ ] **Step 2: Run tests and confirm invocation decision is not tracked**

Run: `pytest tests/unit/application/test_invocation_state.py tests/e2e/test_materialization_choice.py -q`

Expected: second route asks again or state type is missing.

- [ ] **Step 3: Encode the exact Skill policy**

For a relevant unmaterialized Behavior, ask once: A expands now and may dispatch targeted Analyzer, creates one Proposal, answers from current evidence, and persists only after approval; B uses current graph coverage plus indexed facts/source transiently, answers without a Proposal, and remembers the choice only for the invocation. Explicit `$codecortex sync` performs small-scope Main analysis or large/cross-Responsibility Analyzer analysis. Summarize pending Proposals at most once per explicit invocation; after defer/reject, do not repeat in that invocation. High-impact deletion/migration may be mentioned only at a natural checkpoint. Never auto-apply cognition, interrupt ordinary Codex, or create per-file Proposals.

```python
if invocation.has_materialization_decision(behavior_id):
    return route_from(invocation.materialization_decision(behavior_id))
return MaterializationOffer(
    behavior_id=behavior_id,
    choices=("expand", "transient"),
    expansion_requires_later_patch_approval=True,
)
```

- [ ] **Step 4: Run deterministic and configured Child Codex choice tests**

Run: `pytest tests/unit/application/test_invocation_state.py tests/e2e/test_materialization_choice.py -q`

Expected: A produces at most one pending Proposal and no pre-approval graph change; B produces none and does not prompt twice; both answer using current source.

- [ ] **Step 5: Commit materialization policy**

```bash
git add src/codecortex/application/discussion.py src/codecortex/integrations/codex/resources/SKILL.md tests
git commit -m "feat: support optional behavior materialization"
```

## Task 9: Recover Cache and Freshness Deterministically Across Machines

**Files:**
- Create: `src/codecortex/application/recovery.py`
- Modify: `src/codecortex/application/preflight.py`
- Create: `tests/integration/test_cross_machine_recovery.py`

**Interfaces:**
- Produces: `RecoveryService.ensure_cache() -> CacheRecoveryResult`

- [ ] **Step 1: Write failing clean-clone recovery tests**

```python
def test_clone_with_changed_source_recovers_exact_file_diff(cloned_repo_without_cache):
    cloned_repo_without_cache.write("src/pkg/a.py", "changed = True\n")
    result = cloned_repo_without_cache.recover()
    assert result.change_set.changed_files.modified == ("src/pkg/a.py",)
    assert result.change_set.file_diff_completeness == "complete"
    assert result.change_set.entity_diff_completeness == "partial"
    assert result.agent_calls == 0
```

- [ ] **Step 2: Run tests and confirm recovery cannot reconstruct freshness**

Run: `pytest tests/integration/test_cross_machine_recovery.py -q`

Expected: recovery lacks exact file diff or incorrectly declares entity completeness.

- [ ] **Step 3: Implement the eight-step recovery sequence**

Validate manifest, graph, entity refs, source baseline, History, and view reproducibility; full-build current fact SQLite; rebuild graph replica; restore formal UIDs by address/fingerprint; compare current file digests with source baseline; seed baseline entity snapshots only from resolvable formal entity refs and mark partial when source differs; generate ChangeSet/affected scope; validate graph; never dispatch an Agent or modify graph/baseline. When current digest equals baseline, current entities are the exact baseline and snapshots may be complete.

```text
validate_formal_state
  -> rebuild_current_facts
  -> rebuild_graph_replica
  -> restore_formal_entity_uids
  -> compare_source_baseline_to_current
  -> seed_baseline_entity_snapshots_with_honest_completeness
  -> compute_changes_and_scope
  -> validate_graph
```

- [ ] **Step 4: Run deletion, corruption, clone, newline, and schema tests**

Run: `pytest tests/integration/test_cross_machine_recovery.py tests/integration/test_cache_replacement.py -q`

Expected: cache deletion/reclone recovers; current==baseline is fresh; current!=baseline has exact file changes and honest entity completeness; CRLF-only conversion creates no change; unsupported schema fails without mutation.

- [ ] **Step 5: Commit recovery**

```bash
git add src/codecortex/application/recovery.py src/codecortex/application/preflight.py tests/integration/test_cross_machine_recovery.py
git commit -m "feat: recover cognition cache across machines"
```

## Task 10: Freeze the Benchmark Corpus and Scoring

**Files:**
- Create: `tests/benchmark/questions.yaml`
- Create: `tests/benchmark/scoring.py`
- Create: `tests/benchmark/test_scoring.py`
- Create: `tests/fixtures/m1b_repo/`
- Create: `docs/testing/BENCHMARK_PROTOCOL.md`

**Interfaces:**
- Produces: `BenchmarkCase`, `EvidenceExpectation`, `ScoreCard`
- Produces: `score_answer(case: BenchmarkCase, answer: str, trace: Trace) -> ScoreCard`

- [ ] **Step 1: Write failing deterministic scoring tests**

```python
def test_score_penalizes_stale_claim_even_when_keywords_match(case_factory):
    case = case_factory(required_facts=("checkpoint is atomic",), forbidden_claims=("legacy writer is current",))
    score = score_answer(case, "Checkpoint is atomic. The legacy writer is current.", empty_trace())
    assert score.required_fact_recall == 1.0
    assert score.stale_claim_count == 1
    assert score.passed is False
```

- [ ] **Step 2: Run tests and confirm corpus/scorer are absent**

Run: `pytest tests/benchmark/test_scoring.py -q`

Expected: collection fails on missing scorer.

- [ ] **Step 3: Create the fixed repository states and question matrix**

Include fresh graph, dynamic L3/L4, unmaterialized A, unmaterialized B, affected source-first, pending-but-unaffected, unknown scope, graph-outside, cross-Responsibility, cognition/source conflict, cache-deleted, same-topic follow-up, and ordinary Native Codex coding cases. The ordinary-coding case must expect zero CodeCortex MCP calls and no cognition/cache mutation. For every case freeze fixture state ID and repository tree digest, exact prompt, required facts, allowed source evidence, forbidden claims, expected route, and whether Analyzer/materialization prompt is permitted. The harness creates a temporary Git commit from that verified tree and records its actual commit ID in every run artifact; the corpus does not try to hardcode the commit that contains itself. Scoring calculates required-fact recall, source grounding, stale misuse, uncertainty disclosure, route compliance, tokens, latency, Analyzer count, and prompt count; human review remains required for semantic correctness.

```yaml
id: affected-source-first
fixture_state: training_changed
repository_tree_digest: sha256:f16d05ec6b29248d2c61adb1e9263f78e4f7bace1b955014a2d17872cfe4064d
expected_route: source_first
required_facts: [current checkpoint writer is atomic]
forbidden_claims: [baseline checkpoint writer is still current]
analyzer_permitted: true
materialization_prompt_permitted: false
```

- [ ] **Step 4: Run scorer repeatability and corpus-validation tests**

Run: `pytest tests/benchmark -q`

Expected: duplicate IDs, missing state/tree digest/evidence, post-hoc threshold fields, invalid routes, and non-reproducible ordering fail; repeated scoring is identical.

- [ ] **Step 5: Commit benchmark protocol**

```bash
git add tests/benchmark tests/fixtures/m1b_repo docs/testing/BENCHMARK_PROTOCOL.md
git commit -m "test: freeze CodeCortex benchmark corpus"
```

## Task 11: Run Independent Native-vs-CodeCortex Child Codex Evaluation

**Files:**
- Create: `scripts/run_codecortex_benchmark.py`
- Create: `tests/e2e/test_codex_m1b.py`
- Create: `tests/e2e/trace.py`
- Create: `docs/testing/M1B_ACCEPTANCE.md`

**Interfaces:**
- Produces: `run_benchmark(config: BenchmarkConfig) -> BenchmarkReport`
- Consumes: frozen corpus and isolated Child Codex processes

- [ ] **Step 1: Write failing process-isolation and trace-redaction tests**

```python
def test_native_and_codecortex_runs_use_distinct_clean_copies(benchmark_harness):
    run = benchmark_harness.prepare_case("graph-outside")
    assert run.native_repo != run.codecortex_repo
    assert run.native_commit == run.codecortex_commit
    assert run.native_codex_home != run.codecortex_codex_home


def test_trace_redacts_machine_paths_and_tokens(trace_sanitizer, raw_trace):
    sanitized = trace_sanitizer.sanitize(raw_trace)
    assert "/home/" not in sanitized
    assert "OPENAI_API_KEY" not in sanitized
```

- [ ] **Step 2: Run harness tests and confirm missing runner**

Run: `pytest tests/e2e/test_codex_m1b.py -q`

Expected: collection fails on missing benchmark harness.

- [ ] **Step 3: Implement isolated one-shot and approval test modes**

Create native/codecortex copies at the same commit, separate task-specific Codex homes, identical model/reasoning/sandbox/question/budget, and no inherited development conversation. Ordinary cases run `codex exec --ephemeral --json`; Native ignores CodeCortex config. Save sanitized JSONL calls, source/command access, final answer, usage, and time. Approval cases are separate: use non-ephemeral `codex exec --json` plus resume in a temporary Codex home, test-only host `approval_mode=approve`, first-turn graph unchanged, second-turn approval record valid, then delete the home. Product config remains `prompt` and gets one manual VS Code smoke test.

```python
native = ChildRun(repo=native_copy, codex_home=native_home, codecortex_enabled=False)
augmented = ChildRun(repo=codecortex_copy, codex_home=codecortex_home, codecortex_enabled=True)
assert native.commit == augmented.commit
results = (run_ephemeral(native, case.prompt), run_ephemeral(augmented, case.prompt))
```

- [ ] **Step 4: Run the full benchmark and acceptance gate**

Run:

```bash
pytest -q
ruff check src tests scripts
mypy src
python scripts/run_codecortex_benchmark.py --corpus tests/benchmark/questions.yaml --repetitions 3 --artifact-dir /tmp/codecortex-m1b-final
git diff --check
git status --short
```

Expected: deterministic tests pass; all three repetitions are recorded; stale cognition misuse is zero; graph-outside performance stays within the pre-frozen non-regression threshold; worktree is clean. Record blind human-review results and the separate VS Code host-prompt smoke result in `M1B_ACCEPTANCE.md`.

- [ ] **Step 5: Commit the M1b evaluation harness**

```bash
git add scripts/run_codecortex_benchmark.py tests/e2e docs/testing/M1B_ACCEPTANCE.md
git commit -m "test: evaluate persistent CodeCortex understanding"
```

## M1b Completion Gate

```bash
pytest -q
ruff check src tests scripts
mypy src
python -m build
python scripts/run_codecortex_benchmark.py --corpus tests/benchmark/questions.yaml --repetitions 3 --artifact-dir /tmp/codecortex-m1b-final
codecortex validate --json
git diff --check
git status --short
```

M1b is complete only when every explicit CodeCortex route performs preflight, ChangeSet/freshness decisions are conservative and explainable, unaffected queries remain immediate, affected queries use current source first, materialization prompts are optional and non-repeating, cache deletion/clone recovery is deterministic, formal baseline advances are auditable, Native fallback remains unrestricted, and the pre-frozen benchmark plus manual VS Code smoke test pass.
