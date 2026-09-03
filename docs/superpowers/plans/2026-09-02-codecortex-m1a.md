# CodeCortex M1a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend a completed M0 into a reliable Python repository-to-cognitive-graph pipeline with deterministic facts, indexed queries, Analyzer reports, approved semantic graph creation, source baseline persistence, Markdown views, and source navigation.

**Architecture:** The Fact Engine discovers and parses Python without executing it, stores replaceable current facts plus baseline entity snapshots in SQLite, and exposes bounded read APIs. Analyzer returns an evidence-grounded, size-limited AnalysisReport; Main turns it into the only pending semantic Proposal, and Core atomically applies approved graph/source-baseline state.

**Tech Stack:** M0 stack plus Python 3.14 standard-library `ast`, `tokenize`, `sqlite3`, Git subprocesses without a shell, Pydantic DTO boundaries, pytest fixtures for Python 3.9–3.14 syntax.

**Spec:** `docs/CodeCortex_M1a_Detailed_Design.md`, `docs/CodeCortex_Technical_Architecture.md`, and `docs/CodeTree_Understanding_MVP.md` v0.6.2.

## Global Constraints

- Start only after the complete M0 gate passes on a clean branch.
- Analyze Python 3.9–3.14 syntax while running on Python `>=3.14,<3.15`; `feature_version` is best effort, not an alternate historical parser guarantee.
- Use `git ls-files -z --cached --others --exclude-standard -- "*.py"` through `subprocess` without `shell=True`.
- Never execute/import target repository code; normalize only newline style and relative path separators.
- SQLite is a disposable query replica, never formal truth; formal graph, entity refs, source baseline, history, and views are Git files.
- All list/query interfaces require scope plus limit/cursor; no full-database or N+1 query path is allowed.
- AnalysisReport serialized size is at most 512 KiB, with the stricter per-kind limits in the spec.
- Any Managed Source Set change after Proposal creation makes it stale; M1a does not implement local rebase.
- Analyzer MCP remains write-free; standard Agent configuration requests read-only sandbox, while source-digest validation protects report consumption.
- Each task follows red-green-refactor and ends in its own commit.
- Test fixtures/factories named in examples live in the same test file or `tests/conftest.py`; parser/sync/query fixtures must use real temporary Git repositories and SQLite databases rather than mocking the code path being verified.

---

## Task 0: Harden M0 Boundaries Before M1a

**Purpose:** Remove M0 assumptions that would otherwise invalidate persistent M1a facts. This task is intentionally small and must land before Task 1.

**Files:**
- Modify: `src/codecortex/application/services.py`
- Modify: `src/codecortex/interfaces/mcp/server.py`
- Modify: `src/codecortex/interfaces/cli/main.py`
- Modify: `src/codecortex/infrastructure/formal.py`
- Modify: `pyproject.toml`
- Modify: `tests/unit/interfaces/test_cli.py`
- Modify: `tests/unit/interfaces/test_mcp_profiles.py`
- Modify: `tests/integration/test_transaction_recovery.py`
- Modify: `tests/integration/test_apply_transaction.py`
- Modify: `tests/integration/test_codex_install_files.py`
- Modify: `docs/CodeCortex_M1a_Detailed_Design.md`
- Modify: `docs/CodeTree_Understanding_MVP.md`

- [x] Add explicit `recover_formal_state()` under the exclusive repository lock. Main STDIO startup and `codecortex validate` invoke it before serving formal state; Analyzer never invokes it, so its read-only contract remains intact. Test interrupted-transaction recovery through the public Main entry rather than through a direct `FormalStore` call.
- [x] Validate every required M0 `cognitive_proposal_applied` audit field and its cross-field bindings: Event/Proposal namespace, revisions, digest, affected scope, Proposal snapshot, and user approval. Future M1a event extensions may add fields but must not weaken this baseline.
- [x] Make the wheel-resource test self-contained by building into `tmp_path`; declare `hatchling` in the development extra so its required no-isolation build is reproducible after `conda env update`.
- [x] Define, in the detailed and system specifications, that first cognition initialization keys off `cognition_initialized`, uses current revision `r` and commits `r + 1`, and preserves any M0-approved objects unless an approved Proposal explicitly changes them.
- [x] Define `view_manifest.json` and legacy-M0 admission: the first M1a approved apply validates old rendered views and atomically adds digests; normal reads never rewrite legacy state. Define that M0's unbounded `cognitive_graph()` is compatibility-only and must be capped in Task 8.

**Verification:** `ruff check src tests`, `mypy src`, `python -m build`, and `pytest -q` must run successfully from a freshly created `codecortex-dev` environment. The two Codex E2E tests remain opt-in.

---

## Planned File Map

```text
src/codecortex/domain/facts.py               source files, entities, relations, diagnostics
src/codecortex/domain/graph.py               full cognitive graph, flow, mapping, evidence constraints
src/codecortex/domain/analysis.py            AnalysisReport limits and coverage
src/codecortex/application/fact_sync.py       full/incremental deterministic synchronization
src/codecortex/application/query.py           bounded fact/graph/context queries
src/codecortex/application/initialize.py      init/reinitialize semantic workflow support
src/codecortex/application/proposals.py       complete M1a Patch validation and apply preparation
src/codecortex/infrastructure/python/discovery.py
src/codecortex/infrastructure/python/digest.py
src/codecortex/infrastructure/python/parser.py
src/codecortex/infrastructure/python/resolver.py
src/codecortex/infrastructure/persistence/facts_db.py
src/codecortex/infrastructure/persistence/graph_replica.py
src/codecortex/infrastructure/persistence/entity_refs.py
src/codecortex/infrastructure/rendering/markdown.py
src/codecortex/interfaces/mcp/tools.py         M1a tool additions
src/codecortex/integrations/codex/resources/SKILL.md
src/codecortex/integrations/codex/resources/codecortex-analyzer.toml
tests/fixtures/python_syntax/                  versioned syntax fixtures
tests/fixtures/m1a_repo/                       20–50 file semantic fixture repository
tests/conftest.py                              shared real repository/Core fixtures inherited from M0
```

## Task 1: Discover the Managed Source Set and Compute Portable Digests

**Files:**
- Create: `src/codecortex/domain/facts.py`
- Create: `src/codecortex/infrastructure/python/discovery.py`
- Create: `src/codecortex/infrastructure/python/digest.py`
- Create: `tests/unit/python/test_discovery.py`
- Create: `tests/unit/python/test_digest.py`

**Interfaces:**
- Produces: `SourceFileInput(relative_path: str, absolute_path: Path)`
- Produces: `SourceDiagnostic(relative_path: str, code: str, message: str)`
- Produces: `discover_python_source_set(repository: Repository, config: SourceConfig) -> SourceDiscoveryResult`
- Produces: `discover_python_sources(repository: Repository, config: SourceConfig) -> tuple[SourceFileInput, ...]`
- Produces: `digest_source_file(source: SourceFileInput) -> SourceFileDigest`
- Produces: `repository_digest(files: Sequence[SourceFileDigest], profile: DigestProfile) -> str`

- [x] **Step 1: Write failing discovery and normalization tests**

```python
def test_discovery_includes_untracked_and_excludes_ignored(git_repo):
    git_repo.write("tracked.py", "x = 1\n", tracked=True)
    git_repo.write("untracked.py", "y = 2\n", tracked=False)
    git_repo.write("ignored.py", "z = 3\n", tracked=False, ignored=True)
    paths = [item.relative_path for item in discover_python_sources(git_repo.repository, SourceConfig())]
    assert paths == ["tracked.py", "untracked.py"]


def test_crlf_and_lf_have_same_digest(source_factory):
    assert digest_source_file(source_factory("a.py", b"x = 1\r\n")).content_digest == \
        digest_source_file(source_factory("a.py", b"x = 1\n")).content_digest
```

- [x] **Step 2: Run tests and confirm missing Fact Engine modules**

Run: `pytest tests/unit/python/test_discovery.py tests/unit/python/test_digest.py -q`

Expected: collection fails on missing discovery/digest imports.

- [x] **Step 3: Implement versioned source rules and digest profile**

Invoke Git with a list of arguments and NUL-delimited output. Normalize repository-relative paths to `/`, sort by UTF-8 bytes, exclude external symlinks with a structured diagnostic, detect Python encoding with `tokenize.detect_encoding`, convert CRLF/CR to LF, and hash normalized UTF-8. Compute repository digest as profile version, NUL, then sorted `path + NUL + digest + LF`. Return a diagnostic for unreadable/undecodable files rather than silently omitting them.

```python
completed = subprocess.run(
    ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
    cwd=repository.root,
    check=True,
    capture_output=True,
)
paths = tuple(sorted(filter(None, completed.stdout.decode().split("\0")), key=lambda value: value.encode("utf-8")))
```

- [x] **Step 4: Run cross-platform-style digest fixtures**

Run: `pytest tests/unit/python/test_discovery.py tests/unit/python/test_digest.py -q`

Expected: tracked/untracked/ignored, include/exclude, stable ordering, external symlink, CRLF, CR, non-UTF-8 Python encoding, trailing whitespace, and Unicode-preservation cases pass.

- [x] **Step 5: Commit discovery and digesting**

```bash
git add src/codecortex/domain/facts.py src/codecortex/infrastructure/python tests/unit/python
git commit -m "feat: discover and digest Python sources"
```

## Task 2: Parse Python Entities and Stable Identity Candidates

**Files:**
- Create: `src/codecortex/infrastructure/python/parser.py`
- Create: `tests/unit/python/test_parser.py`
- Create: `tests/fixtures/python_syntax/py39.py`
- Create: `tests/fixtures/python_syntax/py310.py`
- Create: `tests/fixtures/python_syntax/py311.py`
- Create: `tests/fixtures/python_syntax/py312.py`
- Create: `tests/fixtures/python_syntax/py313.py`
- Create: `tests/fixtures/python_syntax/py314.py`

**Interfaces:**
- Produces: `ParsedFile`, `CodeEntityCandidate`, `SyntacticRelation`, `ParseDiagnostic`
- Produces: `parse_python_file(source: SourceFileDigest, previous: Sequence[EntityIdentityHint]) -> ParsedFile`

- [x] **Step 1: Write failing nested-entity and parse-error tests**

```python
def test_parser_emits_nested_qualified_addresses(parsed):
    entity = parsed.entity_by_address("pkg.mod:Service.run.inner")
    assert entity.kind == "function"
    assert entity.parent_address == "pkg.mod:Service.run"
    assert (entity.start_line, entity.end_line) == (8, 9)


def test_syntax_error_is_a_file_diagnostic(source_factory):
    result = parse_python_file(source_factory("broken.py", b"def broken(:\n"), previous=())
    assert result.parse_status == "parse_error"
    assert result.diagnostics[0].code == "PYTHON_SYNTAX_ERROR"
```

- [x] **Step 2: Run parser tests and confirm failure**

Run: `pytest tests/unit/python/test_parser.py -q`

Expected: collection fails because `parse_python_file` is absent.

- [x] **Step 3: Implement AST extraction without importing target modules**

Extract module/file, class, function, async function, and method entities; nesting, decorators, normalized signatures, docstring digest, start/end locations, qualname, address, kind, and fingerprint. Emit syntactic `contains`, `import_declaration`, and `declared_base`. Preserve a previous UID only for unique exact address/kind match or unique fingerprint-compatible rename; ambiguous matches get a new `ent_` UID plus diagnostic.

```python
tree = ast.parse(normalized_text, filename=relative_path, feature_version=target_minor)
collector = EntityCollector(module_name=module_name, relative_path=relative_path, identity_hints=previous)
collector.visit(tree)
return collector.result(parse_status="parsed")
```

- [x] **Step 4: Run all syntax and identity tests**

Run: `pytest tests/unit/python/test_parser.py -q`

Expected: fixtures for every supported syntax version, nested/async/decorated definitions, overload-like duplicates, moves, renames, duplicate fingerprints, encoding errors, and syntax errors pass.

- [x] **Step 5: Commit the parser**

```bash
git add src/codecortex/infrastructure/python/parser.py tests/unit/python/test_parser.py tests/fixtures/python_syntax
git commit -m "feat: parse Python code entities"
```

## Task 3: Create the SQLite Fact Schema and Bounded Repository API

**Files:**
- Create: `src/codecortex/infrastructure/persistence/facts_db.py`
- Create: `tests/unit/persistence/test_facts_schema.py`
- Create: `tests/integration/test_facts_queries.py`

**Interfaces:**
- Produces: `FactsDatabase.open_read()`, `open_write()`, `create_new(path)`
- Produces: `replace_files(parsed: Sequence[ParsedFile], metadata: CacheMetadata) -> None`
- Produces: `query_entities(scope: FactScope, cursor: str | None, limit: int) -> Page[CodeEntity]`
- Produces: `query_relations(entity_uids: Sequence[str], relation_types: Sequence[str], limit: int) -> Page[CodeRelation]`

- [x] **Step 1: Write failing schema and query-plan tests**

```python
def test_schema_has_foreign_keys_and_required_indexes(facts_db):
    assert facts_db.foreign_keys_enabled()
    assert facts_db.has_index("relations", ("target_uid", "relation_type"))
    assert facts_db.has_index("baseline_entity_snapshots", ("module_name", "fingerprint"))


def test_entity_query_is_bounded(facts_db_with_200_entities):
    page = facts_db_with_200_entities.query_entities(FactScope.module("pkg"), cursor=None, limit=25)
    assert len(page.items) == 25
    assert page.next_cursor is not None
    assert page.truncated is True
```

- [x] **Step 2: Run tests and confirm schema is absent**

Run: `pytest tests/unit/persistence/test_facts_schema.py tests/integration/test_facts_queries.py -q`

Expected: collection fails on missing `FactsDatabase`.

- [x] **Step 3: Implement the exact M1a DDL and connection policy**

Create `cache_metadata`, `source_files`, `entities` with self-parent `ON DELETE CASCADE`, `relations`, `relation_evidence`, `diagnostics`, and `baseline_entity_snapshots`, including every index listed in M1a sections 5–6. Enforce `foreign_keys=ON`, WAL, busy timeout, parameter binding, allowlisted sort/relation values, analyzer URI `mode=ro`, and cursor-based stable ordering. Store `baseline_entity_snapshot_completeness` in metadata.

```python
def open_read(self) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection
```

- [x] **Step 4: Run schema, foreign-key, pagination, and N+1 tests**

Run: `pytest tests/unit/persistence/test_facts_schema.py tests/integration/test_facts_queries.py -q`

Expected: `foreign_key_check` is empty; deleting a file removes descendants/relations; query-count assertions remain constant as result size grows; limit above configured maximum is rejected.

- [x] **Step 5: Commit fact persistence**

```bash
git add src/codecortex/infrastructure/persistence/facts_db.py tests/unit/persistence tests/integration/test_facts_queries.py
git commit -m "feat: index code facts in SQLite"
```

## Task 4: Resolve Imports, Inheritance, Calls, and Test Relations

**Files:**
- Create: `src/codecortex/infrastructure/python/resolver.py`
- Modify: `src/codecortex/infrastructure/persistence/facts_db.py`
- Create: `tests/unit/python/test_resolver.py`
- Create: `tests/integration/test_reverse_relation_invalidation.py`

**Interfaces:**
- Produces: `resolve_relations(declarations: Sequence[SyntacticRelation], symbol_index: SymbolIndex) -> tuple[ResolvedRelation, ...]`
- Produces: `relation_key(declaration: SyntacticRelation) -> str`
- Produces: `FactsDatabase.incoming_relation_sources(old_target_uids, module_names) -> tuple[RelationDeclarationRef, ...]`

- [x] **Step 1: Write failing stable-key and reverse-invalidation tests**

```python
def test_relation_key_does_not_depend_on_resolved_target(import_declaration):
    unresolved = resolve_one(import_declaration, SymbolIndex.empty())
    resolved = resolve_one(import_declaration, SymbolIndex.with_module("pkg.target"))
    assert unresolved.relation_key == resolved.relation_key


def test_adding_target_re_resolves_unchanged_importer(sync_fixture):
    sync_fixture.write("consumer.py", "from target import Service\n")
    sync_fixture.sync()
    assert sync_fixture.relation("consumer.py").resolution_status == "unresolved"
    sync_fixture.write("target.py", "class Service: pass\n")
    sync_fixture.sync()
    assert sync_fixture.relation("consumer.py").target_address == "target:Service"
    assert sync_fixture.parse_count("consumer.py") == 1
```

- [x] **Step 2: Run resolver tests and confirm failure**

Run: `pytest tests/unit/python/test_resolver.py tests/integration/test_reverse_relation_invalidation.py -q`

Expected: missing resolver symbols or unchanged importer remains unresolved.

- [x] **Step 3: Implement evidence-bearing best-effort relations**

Create deterministic declaration-based keys from source path, source address, relation type, source location, and normalized raw expression—never UID or resolved target. Resolve local imports/inherits first; mark calls/tested_by best effort with evidence, confidence, and resolver version. Before replacing a changed/deleted target, capture incoming sources by old target UID and old/new module names plus same-name unresolved relations; after replacement, rerun only those declarations from stored facts, without reparsing unchanged Python files.

```python
def relation_key(declaration: SyntacticRelation) -> str:
    stable = (
        declaration.source_path,
        declaration.source_address,
        declaration.relation_type,
        declaration.start_line,
        declaration.start_column,
        declaration.normalized_expression,
    )
    return "sha256:" + hashlib.sha256(canonical_json_bytes(stable)).hexdigest()
```

- [x] **Step 4: Run target add/delete/rename/move matrix**

Run: `pytest tests/unit/python/test_resolver.py tests/integration/test_reverse_relation_invalidation.py -q`

Expected: unchanged sources resolve or unresolve correctly, relation keys remain stable, and incremental results equal a clean full rebuild.

- [x] **Step 5: Commit relation resolution**

```bash
git add src/codecortex/infrastructure/python/resolver.py src/codecortex/infrastructure/persistence/facts_db.py tests
git commit -m "feat: resolve and invalidate code relations"
```

## Task 5: Implement Full and Incremental Fact Synchronization

**Files:**
- Create: `src/codecortex/application/fact_sync.py`
- Modify: `src/codecortex/application/ports.py`
- Modify: `src/codecortex/application/services.py`
- Create: `tests/integration/test_fact_sync.py`
- Create: `tests/integration/test_cache_replacement.py`

**Interfaces:**
- Produces: `FactSyncService.sync(mode: Literal["auto", "full"]) -> FactSyncResult`
- Produces: `CacheMetadata` with source digest, graph revision, generation, and baseline snapshot completeness

- [ ] **Step 1: Write failing idempotency and race tests**

```python
def test_unchanged_sync_does_not_advance_generation(fact_sync):
    first = fact_sync.sync("auto")
    second = fact_sync.sync("auto")
    assert second.index_generation == first.index_generation
    assert second.parsed_files == 0


def test_source_change_after_parse_retries_before_commit(sync_with_parse_barrier):
    sync_with_parse_barrier.change_file_during_parse("a.py")
    result = sync_with_parse_barrier.run()
    assert result.retry_count == 1
    assert result.repository_source_digest == sync_with_parse_barrier.current_digest()
```

- [ ] **Step 2: Run tests and confirm missing sync service**

Run: `pytest tests/integration/test_fact_sync.py tests/integration/test_cache_replacement.py -q`

Expected: collection fails on `FactSyncService`.

- [ ] **Step 3: Implement parse-outside-lock and short transactional commit**

Enumerate/hash all files; parse changed files outside the exclusive lock; acquire lock; re-enumerate/re-hash; retry on digest drift; replace affected rows, reverse-resolve incoming relations, update metadata last, and commit once. Full rebuild writes a sibling database, runs `quick_check`, `foreign_key_check`, metadata/count checks, acquires exclusive repository lock, closes/checkpoints old connections, and atomically replaces database plus WAL sidecars. Cache mismatch returns rebuild-required rather than mixed data.

```text
discover_and_hash
  -> parse_changed_files_without_lock
  -> exclusive_lock
  -> rediscover_and_rehash
  -> retry_if_digest_changed
  -> replace_changed_rows_and_reresolve_incoming
  -> update_cache_metadata_last
  -> commit
```

- [ ] **Step 4: Run incremental/full equivalence and replacement tests**

Run: `pytest tests/integration/test_fact_sync.py tests/integration/test_cache_replacement.py -q`

Expected: no-op idempotency, add/modify/delete/rename, parse error, source race retry, corrupt cache, graph-revision mismatch, and full-vs-incremental equivalence pass.

- [ ] **Step 5: Commit Fact Sync**

```bash
git add src/codecortex/application src/codecortex/infrastructure/persistence tests/integration
git commit -m "feat: synchronize repository facts"
```

## Task 6: Model and Validate the Complete Cognitive Graph

**Files:**
- Create: `src/codecortex/domain/graph.py`
- Modify: `src/codecortex/domain/cognition.py`
- Create: `tests/unit/domain/test_graph_constraints.py`

**Interfaces:**
- Produces: `Responsibility`, `Behavior`, `Capability`, `CognitiveEdge`, `LogicalFlow`, `FlowStep`, `ImplementationMapping`, `Evidence`
- Produces: `validate_cognitive_graph(graph: CognitiveGraph) -> tuple[GraphViolation, ...]`

- [ ] **Step 1: Write failing graph-invariant tests**

```python
def test_behavior_requires_exactly_one_responsibility_parent(valid_graph):
    invalid = valid_graph.with_second_parent("behavior.answer", "responsibility.other")
    assert violation_codes(invalid) == {"BEHAVIOR_PARENT_COUNT"}


def test_unmaterialized_flow_has_no_steps(valid_behavior):
    invalid = valid_behavior.with_flow(status="unmaterialized", steps=(flow_step(),))
    assert violation_codes(invalid) == {"UNMATERIALIZED_FLOW_HAS_STEPS"}
```

- [ ] **Step 2: Run graph tests and confirm missing schema**

Run: `pytest tests/unit/domain/test_graph_constraints.py -q`

Expected: collection fails because full graph types are absent.

- [ ] **Step 3: Implement all node, edge, flow, mapping, evidence, epistemic, and provenance rules**

Allow only Responsibility/Behavior/Capability nodes and the specified edge matrix; require one Responsibility parent per Behavior; prohibit hierarchy cycles; validate Flow ordering and materialization states; constrain Mapping target kinds and confidence; require evidence for inferred/uncertain relations; validate repository-relative source locations; require `approval_event_id` on formal semantic objects; keep `intent` nullable and `epistemic_status` independent from approval.

```python
ALLOWED_EDGES = {
    ("responsibility", "contains", "behavior"),
    ("behavior", "uses", "capability"),
    ("capability", "depends_on", "capability"),
}
if (source.kind, edge.edge_type, target.kind) not in ALLOWED_EDGES:
    violations.append(GraphViolation("EDGE_KIND_NOT_ALLOWED", edge.edge_id))
```

- [ ] **Step 4: Run allowed/forbidden graph matrix**

Run: `pytest tests/unit/domain/test_graph_constraints.py -q`

Expected: every allowed edge passes; every forbidden edge, cycle, dangling target, duplicate ID/alias, invalid Flow, invalid Mapping, and wrong approval namespace fails with one stable violation code.

- [ ] **Step 5: Commit graph domain**

```bash
git add src/codecortex/domain/graph.py src/codecortex/domain/cognition.py tests/unit/domain/test_graph_constraints.py
git commit -m "feat: model the cognitive graph"
```

## Task 7: Build the SQLite Cognitive Query Replica

**Files:**
- Create: `src/codecortex/infrastructure/persistence/graph_replica.py`
- Create: `tests/unit/persistence/test_graph_replica_schema.py`
- Create: `tests/integration/test_graph_replica.py`

**Interfaces:**
- Produces: `GraphReplica.rebuild(graph: CognitiveGraph, graph_revision: int) -> None`
- Produces: `GraphReplica.search(query: str, kinds: Sequence[str], limit: int) -> tuple[GraphHit, ...]`
- Produces: `GraphReplica.context(request: ContextRequest) -> DiscussionContext`

- [ ] **Step 1: Write failing replica consistency tests**

```python
def test_replica_rejects_revision_mismatch(graph_replica):
    graph_replica.set_metadata(graph_revision=11)
    with pytest.raises(CodeCortexError) as exc:
        graph_replica.search("checkpoint", (), 10, expected_revision=12)
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_search_uses_alias_and_stable_tie_break(graph_replica_with_graph):
    hits = graph_replica_with_graph.search("resume", ("behavior",), 10, expected_revision=1)
    assert [hit.node_id for hit in hits] == ["behavior.checkpoint-resume"]
```

- [ ] **Step 2: Run tests and confirm missing replica**

Run: `pytest tests/unit/persistence/test_graph_replica_schema.py tests/integration/test_graph_replica.py -q`

Expected: collection fails on `GraphReplica`.

- [ ] **Step 3: Implement the complete cognitive replica DDL and importer**

Create all tables/indexes from M1a section 7. Rebuild within one SQLite transaction after validating graph revision and polymorphic subject/evidence references. Search uses NFKC + casefold, Latin alphanumeric terms, and full short CJK terms plus CJK bi/trigrams; weight exact alias, alias, title, summary, and observed fields according to the detailed design, then break ties by kind and stable node ID without relying on FTS5. Context traversal applies depth, node/entity/evidence maxima before materializing DTOs and reports truncation/continuation.

```python
def normalize_search_terms(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    latin = LATIN_TOKEN.findall(normalized)
    cjk_runs = CJK_RUN.findall(normalized)
    cjk = [term for run in cjk_runs for size in (len(run), 2, 3) for term in ngrams(run, size)]
    return tuple(sorted(set((*latin, *cjk))))
```

- [ ] **Step 4: Run import/query/limit tests**

Run: `pytest tests/unit/persistence/test_graph_replica_schema.py tests/integration/test_graph_replica.py -q`

Expected: graph import is all-or-nothing; polymorphic dangling refs fail; repeated rebuild is deterministic; bounded context executes a fixed number of queries.

- [ ] **Step 5: Commit graph replica**

```bash
git add src/codecortex/infrastructure/persistence/graph_replica.py tests
git commit -m "feat: index cognition for bounded queries"
```

## Task 8: Add M1a Read APIs and MCP Tools

**Files:**
- Create: `src/codecortex/application/query.py`
- Modify: `src/codecortex/interfaces/mcp/tools.py`
- Create: `tests/unit/application/test_query_service.py`
- Create: `tests/integration/test_m1a_mcp_tools.py`

**Interfaces:**
- Produces: `repository_facts`, `analysis_scope`, `resolve_entity_context`, `get_discussion_context`, `search_cognitive_graph`
- Adds Main-only: `sync_repository_facts(mode: Literal["auto", "full"])`

- [ ] **Step 1: Write failing exact-profile and bounded-response tests**

```python
@pytest.mark.anyio
async def test_m1a_read_tools_exist_in_both_profiles(mcp_profiles):
    expected = {"repository_facts", "analysis_scope", "resolve_entity_context", "get_discussion_context", "search_cognitive_graph"}
    assert expected <= await mcp_profiles.names("main")
    assert expected <= await mcp_profiles.names("analyzer")
    assert "sync_repository_facts" not in await mcp_profiles.names("analyzer")


def test_discussion_context_reports_truncation(query_service):
    result = query_service.get_discussion_context(ContextRequest(depth=2, max_nodes=3, max_entities=4, max_evidence=2))
    assert len(result.nodes) <= 3
    assert result.truncated is True
```

- [ ] **Step 2: Run tests and confirm missing tools**

Run: `pytest tests/unit/application/test_query_service.py tests/integration/test_m1a_mcp_tools.py -q`

Expected: tool-list and service-method assertions fail.

- [ ] **Step 3: Implement query orchestration and explicit DTO versions**

Every request validates scope/limit, verifies cache source digest and graph revision, then performs batch queries. `analysis_scope` partitions by package/module and reports diagnostics; `resolve_entity_context` accepts exactly one anchor form; discussion context returns nodes, flows, mappings, current entity locations, evidence, and truncation metadata. Add static MCP registrations without widening Analyzer writes.

```python
def get_discussion_context(self, request: ContextRequest) -> DiscussionContext:
    self.cache_guard.require_current(request.expected_source_digest, request.expected_graph_revision)
    nodes = self.graph_replica.breadth_first(request.node_ids, request.depth, request.max_nodes)
    entities = self.facts.batch_entities_for_nodes(nodes.ids, request.max_entities)
    evidence = self.graph_replica.batch_evidence(nodes.ids, request.max_evidence)
    return DiscussionContext.from_bounded(nodes, entities, evidence)
```

- [ ] **Step 4: Run application and protocol tests**

Run: `pytest tests/unit/application/test_query_service.py tests/integration/test_m1a_mcp_tools.py -q`

Expected: invalid anchors/limits fail, cache mismatch never returns mixed facts, and both profiles return schema-versioned bounded payloads.

- [ ] **Step 5: Commit M1a read APIs**

```bash
git add src/codecortex/application/query.py src/codecortex/interfaces/mcp/tools.py tests
git commit -m "feat: expose bounded repository context"
```

## Task 9: Validate Analyzer Reports and Protect Main Context

**Files:**
- Create: `src/codecortex/domain/analysis.py`
- Create: `src/codecortex/application/initialize.py`
- Modify: `src/codecortex/integrations/codex/resources/codecortex-analyzer.toml`
- Create: `tests/unit/domain/test_analysis_report.py`
- Create: `tests/integration/test_analysis_report_freshness.py`

**Interfaces:**
- Produces: `AnalysisReport`, `AnalysisCoverage`, `AnalysisLimits`
- Produces: `validate_analysis_report(payload: bytes, expected_graph_revision: int, expected_source_digest: str) -> AnalysisReport`

- [ ] **Step 1: Write failing byte-limit and freshness tests**

```python
def test_report_rejects_serialized_payload_over_512_kib(valid_report_bytes):
    oversized = valid_report_bytes + b" " * (512 * 1024)
    with pytest.raises(CodeCortexError) as exc:
        validate_analysis_report(oversized, 0, "sha256:source")
    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_report_is_rejected_when_source_changes(report_consumer):
    report_consumer.begin_at("sha256:a")
    report_consumer.set_current_digest("sha256:b")
    with pytest.raises(CodeCortexError) as exc:
        report_consumer.consume(valid_report("sha256:a"))
    assert exc.value.code is ErrorCode.PROPOSAL_STALE
```

- [ ] **Step 2: Run tests and confirm missing report schema**

Run: `pytest tests/unit/domain/test_analysis_report.py tests/integration/test_analysis_report_freshness.py -q`

Expected: collection fails on missing AnalysisReport.

- [ ] **Step 3: Implement strict report validation and Analyzer instructions**

Enforce 512 KiB total, 300 nodes, 1000 edges, 2000 mappings, 2000 evidence items, and 240 Unicode code points per description/observation. Require coverage, unexamined partitions, unmapped regions, uncertainties, graph revision, and source digest. Record digest before dispatch; Fact Sync and compare again before consumption; reject any change. Update Analyzer TOML to request read-only sandbox, use analyzer MCP only, avoid long source excerpts, lower semantic granularity on cap pressure, and always report uncovered areas.

```python
LIMITS = AnalysisLimits(
    serialized_bytes=512 * 1024,
    nodes=300,
    edges=1000,
    mappings=2000,
    evidence=2000,
    description_codepoints=240,
)
if current_source_digest != report.analyzed_source_digest:
    raise CodeCortexError(ErrorCode.PROPOSAL_STALE, "Source changed during analysis")
```

- [ ] **Step 4: Run boundary-value and stale-report tests**

Run: `pytest tests/unit/domain/test_analysis_report.py tests/integration/test_analysis_report_freshness.py -q`

Expected: every exact maximum passes, maximum-plus-one fails, malformed IDs/locations fail, and graph/source changes reject the entire report.

- [ ] **Step 5: Commit AnalysisReport boundaries**

```bash
git add src/codecortex/domain/analysis.py src/codecortex/application/initialize.py src/codecortex/integrations/codex/resources/codecortex-analyzer.toml tests
git commit -m "feat: validate bounded analyzer reports"
```

## Task 10: Extend Proposals and Apply Full Cognitive Graph State

**Files:**
- Create: `src/codecortex/application/proposals.py`
- Create: `src/codecortex/infrastructure/persistence/entity_refs.py`
- Modify: `src/codecortex/domain/proposals.py`
- Modify: `src/codecortex/infrastructure/formal.py`
- Modify: `src/codecortex/infrastructure/persistence/facts_db.py`
- Create: `tests/unit/application/test_graph_patch.py`
- Create: `tests/integration/test_m1a_apply.py`

**Interfaces:**
- Adds Patch operations: `set_logical_flow`, `add_mapping`, `update_mapping`, `remove_mapping`
- Produces: `create_proposal_from_analysis(report: AnalysisReport, reason: str) -> Proposal`
- Produces: formal `source_baseline.json` and rebuilt `entity_refs.json`

- [ ] **Step 1: Write failing full-apply and source-stale tests**

```python
def test_approved_initialization_advances_all_formal_state(m1a_app, approved_initial_report):
    proposal = m1a_app.create_proposal_from_analysis(approved_initial_report, "initialize understanding")
    result = m1a_app.apply_cognitive_proposal(proposal.id, approval_for(proposal))
    state = m1a_app.formal_store.load()
    assert result.graph_revision == approved_initial_report.base_graph_revision + 1
    assert state.manifest.cognition_initialized is True
    assert state.manifest.cognition_baseline.source_digest == approved_initial_report.analyzed_source_digest
    assert state.source_baseline.repository_source_digest == approved_initial_report.analyzed_source_digest
    assert state.entity_refs.entities


def test_any_source_change_makes_proposal_stale(m1a_app, proposal):
    m1a_app.repository.write("unrelated.py", "x = 1\n")
    with pytest.raises(CodeCortexError) as exc:
        m1a_app.apply_cognitive_proposal(proposal.id, approval_for(proposal))
    assert exc.value.code is ErrorCode.PROPOSAL_STALE
```

- [ ] **Step 2: Run tests and confirm M0 Patch/apply is insufficient**

Run: `pytest tests/unit/application/test_graph_patch.py tests/integration/test_m1a_apply.py -q`

Expected: new operations or baseline/entity-ref assertions fail.

- [ ] **Step 3: Implement complete Patch validation and formal baseline advance**

Convert the report to stable-ID operations, but keep it pending until approval. Apply only when graph revision and full current repository digest equal Proposal preconditions; validate every entity fingerprint/address; recompute entity refs from all formal mappings/evidence; set `cognition_initialized=true`; generate sorted source baseline from current fact digests; write self-contained applied Event and all formal files in the M0 transaction; then refresh graph replica and copy current entities into `baseline_entity_snapshots` with completeness `complete`. If cache refresh fails after formal commit, return success with rebuild-required warning.

```python
if current_digest != proposal.analyzed_source_digest:
    raise CodeCortexError(ErrorCode.PROPOSAL_STALE, "Repository source digest changed")
new_manifest = old_manifest.advance_graph(
    cognition_initialized=True,
    cognition_baseline=baseline_from_current_facts,
)
formal_store.commit(new_manifest, new_graph, entity_refs, source_baseline, event, views)
```

- [ ] **Step 4: Run operation, stale, transaction, and cache-refresh tests**

Run: `pytest tests/unit/application/test_graph_patch.py tests/integration/test_m1a_apply.py tests/integration/test_transaction_recovery.py -q`

Expected: every operation validates before mutation; unapproved or stale apply writes nothing; formal transaction cannot split graph/baseline/history; cache failure leaves recoverable formal truth.

- [ ] **Step 5: Commit full graph apply**

```bash
git add src/codecortex/application/proposals.py src/codecortex/domain/proposals.py src/codecortex/infrastructure tests
git commit -m "feat: apply repository cognition with source baseline"
```

## Task 11: Render Views, Inspect Nodes, and Navigate to Current Source

**Files:**
- Create: `src/codecortex/infrastructure/rendering/markdown.py`
- Modify: `src/codecortex/infrastructure/views.py`
- Modify: `src/codecortex/application/query.py`
- Create: `tests/unit/rendering/test_markdown.py`
- Create: `tests/integration/test_inspect_node.py`

**Interfaces:**
- Produces deterministic `TREE.md` and per-kind node pages
- Extends: `inspect_node(node_id: str) -> NodeInspection`

- [ ] **Step 1: Write failing golden-output and moved-source tests**

```python
def test_tree_view_matches_golden(valid_graph, golden):
    assert render_tree(valid_graph) == golden("TREE.md")


def test_inspect_resolves_current_location_after_entity_move(inspect_fixture):
    inspect_fixture.move_entity("src/old.py", "src/new.py")
    inspect_fixture.sync_facts()
    result = inspect_fixture.inspect("behavior.answer")
    assert result.mappings[0].current_location.relative_path == "src/new.py"
    assert result.mappings[0].resolution_status == "resolved"
```

- [ ] **Step 2: Run tests and confirm rendering/query gaps**

Run: `pytest tests/unit/rendering/test_markdown.py tests/integration/test_inspect_node.py -q`

Expected: golden views or current-source fields fail.

- [ ] **Step 3: Implement deterministic projection and UID-first resolution**

Render sorted Responsibility→Behavior hierarchy, shared Capability links, summary/intent/observed/epistemic status, Flow, mappings, evidence, approval event, and repository-relative source links. `inspect_node` loads formal graph, resolves entity UID against current facts, falls back to address/fingerprint, preserves last known location when unresolved, and returns stale status rather than rewriting formal mapping. Create and validate the formal `view_manifest.json` alongside every M1a apply; for a legacy M0 state, verify the old generated views before the first approved migration rather than silently overwriting hand edits.

```python
def resolve_mapping(mapping: ImplementationMapping, facts: FactsDatabase, refs: EntityRefs) -> ResolvedMapping:
    current = facts.entity_by_uid(mapping.entity_uid)
    if current is None:
        current = facts.unique_entity_by_address_or_fingerprint(refs[mapping.entity_uid])
    return ResolvedMapping.from_current_or_last_known(mapping, current, refs[mapping.entity_uid])
```

- [ ] **Step 4: Run golden, portability, and unresolved tests**

Run: `pytest tests/unit/rendering/test_markdown.py tests/integration/test_inspect_node.py -q`

Expected: repeated rendering is byte-identical; no absolute path appears; moved entity resolves; ambiguous/missing entity is reported without deleting cognition.

- [ ] **Step 5: Commit rendering and inspection**

```bash
git add src/codecortex/infrastructure/rendering src/codecortex/infrastructure/views.py src/codecortex/application/query.py tests
git commit -m "feat: render and inspect cognitive views"
```

## Task 12: Orchestrate Init/Reinitialize and Run Real M1a Acceptance

**Files:**
- Modify: `src/codecortex/application/initialize.py`
- Modify: `src/codecortex/integrations/codex/resources/SKILL.md`
- Create: `tests/fixtures/m1a_repo/`
- Create: `tests/e2e/test_codex_m1a.py`
- Create: `scripts/run_m1a_acceptance.py`
- Create: `docs/testing/M1A_ACCEPTANCE.md`

**Interfaces:**
- Produces Skill flows `$codecortex init`, `inspect`, `reinitialize`
- Consumes Analyzer dispatch from Codex, not from Core

- [ ] **Step 1: Write fixture-grounded acceptance assertions**

```python
@pytest.mark.codex_e2e
def test_real_init_creates_grounded_graph(codex_m1a_harness):
    result = codex_m1a_harness.initialize_and_approve()
    assert result.graph_revision == 1
    assert {"responsibility.ingestion", "responsibility.reporting"} <= set(result.node_ids)
    mapping = result.mapping_for("behavior.import-records")
    assert mapping.relative_path == "src/m1a_fixture/ingestion/service.py"
    assert result.source_line_contains(mapping, "def import_records")
```

- [ ] **Step 2: Run without model access and confirm explicit skip; run deterministic fixture gate**

Run:

```bash
pytest tests/e2e/test_codex_m1a.py -m codex_e2e -q
pytest tests/unit tests/integration -q
```

Expected: real Codex test skips only when access is absent; deterministic tests pass.

- [ ] **Step 3: Implement Skill orchestration and the semantic fixture**

Skill must run Fact Sync, request analysis scope, force Analyzer for init/reinitialize, validate the returned report, create one aggregate Proposal, display Big Picture/diff/uncertainties, discuss revisions, and call apply only after explicit approval. Reinitialize reads existing intent/history and displays add/delete/move/merge/split/conflict without clearing old graph. Build a 20–50 Python-file fixture whose expected responsibilities, behaviors, shared capability, flows, and source anchors are human-authored in the test oracle. Also run fact indexing and bounded-query performance against one frozen real 100–500 Python-file repository; record its origin, commit, file count, timings, peak database size, and acceptance result in `M1A_ACCEPTANCE.md` rather than silently substituting the small fixture.

```text
fact_sync -> analysis_scope -> spawn_analyzer -> validate_report
          -> create_one_proposal -> show_and_discuss_diff
          -> require_current_patch_approval -> atomic_apply
```

- [ ] **Step 4: Run the complete M1a gate**

Run:

```bash
pytest -q
ruff check src tests scripts
mypy src
python scripts/run_m1a_acceptance.py --artifact-dir /tmp/codecortex-m1a-final
git diff --check
git status --short
```

Expected: all deterministic checks pass; configured Child Codex produces one bounded AnalysisReport and one approved revision; source links resolve; reinitialize preserves user-confirmed intent; worktree is clean.

- [ ] **Step 5: Commit M1a acceptance**

```bash
git add src/codecortex/application/initialize.py src/codecortex/integrations/codex/resources/SKILL.md tests/fixtures/m1a_repo tests/e2e/test_codex_m1a.py scripts/run_m1a_acceptance.py docs/testing/M1A_ACCEPTANCE.md
git commit -m "test: add M1a repository understanding gate"
```

## M1a Completion Gate

```bash
pytest -q
ruff check src tests scripts
mypy src
python -m build
python scripts/run_m1a_acceptance.py --artifact-dir /tmp/codecortex-m1a-final
git diff --check
git status --short
```

M1a is complete only when a 100–500-file Python repository can build/query facts without unbounded loads, incremental and full indexes are equivalent, incoming relations re-resolve correctly, Analyzer report limits hold, approved graph/source baseline/history commit atomically, views are deterministic, and cache deletion preserves formal cognition.
