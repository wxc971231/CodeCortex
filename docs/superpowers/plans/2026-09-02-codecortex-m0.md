# CodeCortex M0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the smallest real CodeCortex vertical slice: installable CLI, revision-0 repository state, two MCP profiles, Codex Skill/Analyzer installation, Proposal discussion boundary, approved atomic apply, concurrency safety, and real Child Codex acceptance.

**Architecture:** Build a Python package whose domain/application layers are independent of Codex and MCP. A single STDIO MCP implementation exposes a read/write Main profile and a read-only Analyzer profile over the same application services; Git-tracked JSON is formal truth, while pending Proposals and transaction journals live under `.codecortex/.cache/`.

**Tech Stack:** Python 3.14, Conda, Hatchling, Pydantic 2, MCP Python SDK 2.x (`mcp>=2,<3`, `MCPServer` API), tomlkit, pytest, pytest-cov, Ruff, mypy, standard-library `fcntl` and filesystem primitives.

**Spec:** `docs/CodeCortex_M0_Detailed_Design.md`, `docs/CodeCortex_Technical_Architecture.md`, and `docs/CodeTree_Understanding_MVP.md` v0.6.2.

## Global Constraints

- Runtime Python is exactly `>=3.14,<3.15`; supported platforms are Linux, macOS, and WSL, not native Windows.
- Core must not call an LLM, execute analyzed repository code, import target modules, run a daemon, or require a network connection.
- Domain code must not import MCP, Pydantic, SQLite, Codex, or filesystem infrastructure.
- Main and Analyzer use one server implementation with static profile allowlists; Analyzer profile must never register formal-state or cache-writing tools.
- STDIO stdout is protocol-only; all diagnostics go to stderr.
- Formal JSON is UTF-8, LF terminated, deterministic, repository-relative, and contains no machine absolute paths or credentials.
- `apply_cognitive_proposal` requires both current Proposal digest and a structured user approval record.
- Each task uses test-first development and ends with a focused commit; do not combine later tasks into an earlier commit.
- Before coding each task, re-read its referenced detailed-design section; when code and spec disagree, stop and correct the spec first.
- Test fixtures/factories named in examples are implemented in the same test file unless explicitly mapped to `tests/conftest.py`; they must construct real domain objects and temporary files, never mocks that bypass the behavior under test.

---

## Planned File Map

```text
pyproject.toml                         package metadata, runtime/dev dependencies, CLI entry point
environment.yml                       reproducible Conda developer environment
.gitignore                            Python/build/cache and `.codecortex/.cache/` rules
src/codecortex/__init__.py             package version
src/codecortex/__main__.py             `python -m codecortex` entry
src/codecortex/domain/errors.py        stable domain error codes
src/codecortex/domain/ids.py           typed ID validation and generation
src/codecortex/domain/cognition.py     M0 graph and provenance models
src/codecortex/domain/proposals.py     Patch, Proposal, approval, state machine
src/codecortex/application/ports.py    clock, lock, formal store, pending store interfaces
src/codecortex/application/services.py M0 use-case composition
src/codecortex/infrastructure/repository.py Git-root discovery and safe relative paths
src/codecortex/infrastructure/jsonio.py deterministic JSON and atomic single-file primitives
src/codecortex/infrastructure/locking.py POSIX shared/exclusive repository lock
src/codecortex/infrastructure/formal.py formal state loading, validation, transaction recovery
src/codecortex/infrastructure/pending.py pending Proposal persistence
src/codecortex/infrastructure/views.py deterministic M0 Markdown projection
src/codecortex/interfaces/cli/main.py   argparse commands and error-to-exit-code mapping
src/codecortex/interfaces/mcp/server.py MCPServer construction and STDIO launch
src/codecortex/interfaces/mcp/tools.py  DTO adapters and static profile registration
src/codecortex/integrations/codex/install.py idempotent user configuration installer
src/codecortex/integrations/codex/doctor.py installation diagnostics
src/codecortex/integrations/codex/resources/SKILL.md
src/codecortex/integrations/codex/resources/codecortex-analyzer.toml
docs/INSTALL.md                      pipx user install and Conda contributor workflow
tests/unit/                         pure unit tests
tests/integration/                  filesystem, process, MCP, locking, recovery tests
tests/e2e/                          real Codex harness, opt-in marker
tests/conftest.py                   temporary Git repository and ApplicationServices fixtures
```

## Task 1: Bootstrap the Installable Package and Quality Gates

**Files:**
- Create: `pyproject.toml`
- Create: `environment.yml`
- Modify: `.gitignore`
- Create: `src/codecortex/__init__.py`
- Create: `src/codecortex/__main__.py`
- Create: `src/codecortex/interfaces/cli/main.py`
- Create: `tests/unit/test_package.py`

**Interfaces:**
- Produces: `codecortex.interfaces.cli.main:main(argv: Sequence[str] | None = None) -> int`
- Produces: console command `codecortex`

- [ ] **Step 1: Write the package smoke test**

```python
from importlib.metadata import version

from codecortex import __version__
from codecortex.interfaces.cli.main import main


def test_package_version_and_cli(capsys):
    assert __version__ == version("codecortex")
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
```

- [ ] **Step 2: Run the test and confirm the missing package failure**

Run: `pytest tests/unit/test_package.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'codecortex'`.

- [ ] **Step 3: Add packaging and the minimal CLI**

Use these project constraints in `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "codecortex"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = ["mcp>=2,<3", "pydantic>=2,<3", "tomlkit>=0.13,<1"]

[project.optional-dependencies]
dev = ["build>=1,<2", "pytest>=8,<9", "pytest-cov>=6,<7", "ruff>=0.12,<1", "mypy>=1.17,<2"]

[project.scripts]
codecortex = "codecortex.interfaces.cli.main:entrypoint"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["codex_e2e: requires an installed Codex CLI and configured model access"]

[tool.hatch.build.targets.wheel]
packages = ["src/codecortex"]
```

Implement `main()` with only `--version`; `entrypoint()` must raise `SystemExit(main())`. In `environment.yml`, install Python 3.14 and `pip install -e ".[dev]"`. Ignore `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, build artifacts, and `.codecortex/.cache/`, but do not ignore formal `.codecortex` JSON.

- [ ] **Step 4: Run package, lint, type, and build checks**

Run:

```bash
python -m pip install -e ".[dev]"
pytest tests/unit/test_package.py -q
ruff check src tests
mypy src
python -m build
```

Expected: every command exits 0; wheel and sdist appear under `dist/`.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add pyproject.toml environment.yml .gitignore src tests/unit/test_package.py
git commit -m "build: bootstrap CodeCortex package"
```

## Task 2: Establish Stable Errors, IDs, and Deterministic JSON

**Files:**
- Create: `src/codecortex/domain/errors.py`
- Create: `src/codecortex/domain/ids.py`
- Create: `src/codecortex/infrastructure/jsonio.py`
- Create: `tests/unit/domain/test_errors_and_ids.py`
- Create: `tests/unit/infrastructure/test_jsonio.py`

**Interfaces:**
- Produces: `ErrorCode`, `CodeCortexError`
- Produces: `new_id(prefix: IdPrefix) -> str`, `validate_id(value: str, prefix: IdPrefix) -> str`
- Produces: `canonical_json_bytes(value: object) -> bytes`, `write_json_atomic(path: Path, value: object) -> None`

- [ ] **Step 1: Write failing deterministic-serialization and ID tests**

```python
def test_canonical_json_is_stable_and_lf_terminated(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"z": 1, "a": [2, 1]})
    assert path.read_bytes() == b'{\n  "a": [\n    2,\n    1\n  ],\n  "z": 1\n}\n'


def test_validate_id_rejects_cross_namespace():
    with pytest.raises(CodeCortexError) as exc:
        validate_id("prop_01J00000000000000000000000", IdPrefix.EVENT)
    assert exc.value.code is ErrorCode.INVALID_ID
```

- [ ] **Step 2: Run the focused tests and confirm missing-symbol failures**

Run: `pytest tests/unit/domain/test_errors_and_ids.py tests/unit/infrastructure/test_jsonio.py -q`

Expected: collection fails because the new modules do not exist.

- [ ] **Step 3: Implement the exact error and serialization contracts**

Define `ErrorCode` with M0 codes `NOT_INITIALIZED`, `PATH_OUTSIDE_REPOSITORY`, `LOCK_TIMEOUT`, `ANALYSIS_REPORT_INVALID`, `PROPOSAL_STALE`, `GRAPH_REVISION_CONFLICT`, `APPROVAL_REQUIRED`, `APPROVAL_MISMATCH`, `FORMAL_STATE_CORRUPT`, `UNSUPPORTED_SCHEMA`, and `INVALID_ID`. `CodeCortexError` carries `code`, `message`, `retryable`, `details`, and `suggested_action`, and exposes `to_dict()`.

Use `enum.StrEnum` for prefixes `ent`, `edge`, `map`, `evid`, `prop`, `chg`, `evt`. Generate a real 128-bit ULID payload from a 48-bit UTC millisecond timestamp plus 80 cryptographically random bits, encode it as 26 uppercase Crockford Base32 characters, and validate timestamp/payload bounds plus the namespace prefix without importing an external ULID package. Tests inject clock and randomness for deterministic values. Serialize with `json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"`; write to a sibling temporary file, flush, `os.fsync`, then `os.replace`.

```python
def canonical_json_bytes(value: object) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return f"{text}\n".encode("utf-8")


def new_id(prefix: IdPrefix, now_ms: int, random_bytes: bytes) -> str:
    if not 0 <= now_ms < 2**48 or len(random_bytes) != 10:
        raise CodeCortexError(ErrorCode.INVALID_ID, "Invalid ULID components")
    payload = (now_ms << 80) | int.from_bytes(random_bytes, "big")
    return f"{prefix.value}_{encode_crockford_26(payload)}"
```

- [ ] **Step 4: Run focused tests and the full unit suite**

Run:

```bash
pytest tests/unit/domain/test_errors_and_ids.py tests/unit/infrastructure/test_jsonio.py -q
pytest tests/unit -q
ruff check src tests
mypy src
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the domain primitives**

```bash
git add src/codecortex/domain src/codecortex/infrastructure/jsonio.py tests/unit
git commit -m "feat: add stable domain primitives"
```

## Task 3: Locate Repositories and Enforce Path Boundaries

**Files:**
- Create: `src/codecortex/infrastructure/repository.py`
- Create: `tests/unit/infrastructure/test_repository.py`

**Interfaces:**
- Produces: `Repository(root: Path)`
- Produces: `find_repository(start: Path) -> Repository`
- Produces: `Repository.resolve_relative(relative: str) -> Path`
- Produces: `Repository.to_relative(path: Path) -> str`

- [ ] **Step 1: Write failing root-discovery and escape tests**

```python
def test_finds_nearest_git_root_and_normalizes_paths(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    repo = find_repository(nested)
    assert repo.root == root.resolve()
    assert repo.to_relative(root / "src" / "pkg") == "src/pkg"


def test_rejects_parent_escape(repository):
    with pytest.raises(CodeCortexError) as exc:
        repository.resolve_relative("../outside.py")
    assert exc.value.code is ErrorCode.PATH_OUTSIDE_REPOSITORY
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/unit/infrastructure/test_repository.py -q`

Expected: collection fails because `repository.py` is absent.

- [ ] **Step 3: Implement nearest-root and `realpath/commonpath` checks**

Walk resolved parents from `start` until `.git` exists; return `NOT_INITIALIZED` only from higher application services, and use `PATH_OUTSIDE_REPOSITORY` for absolute input, `..`, symlink escape, or a resolved path whose `os.path.commonpath` differs from the root. All returned state paths use `Path`; all persisted paths use POSIX strings.

```python
def find_repository(start: Path) -> Repository:
    cursor = (start if start.is_dir() else start.parent).resolve()
    for candidate in (cursor, *cursor.parents):
        if (candidate / ".git").exists():
            return Repository(candidate)
    raise CodeCortexError(ErrorCode.NOT_INITIALIZED, "No Git repository found")
```

- [ ] **Step 4: Verify normal, nested, symlink, and no-repository cases**

Run: `pytest tests/unit/infrastructure/test_repository.py -q`

Expected: tests for a file start path, nested Git roots, external symlinks, absolute input, and missing `.git` all pass.

- [ ] **Step 5: Commit repository boundaries**

```bash
git add src/codecortex/infrastructure/repository.py tests/unit/infrastructure/test_repository.py
git commit -m "feat: enforce repository path boundaries"
```

## Task 4: Add POSIX Repository Locking

**Files:**
- Create: `src/codecortex/application/ports.py`
- Create: `src/codecortex/infrastructure/locking.py`
- Create: `tests/integration/test_repository_lock.py`

**Interfaces:**
- Produces: `LockMode = Literal["shared", "exclusive"]`
- Produces: `RepositoryLock.acquire(mode: LockMode, timeout_seconds: float) -> ContextManager[None]`

- [ ] **Step 1: Write a failing two-process lock test**

```python
def test_exclusive_writer_times_out_behind_shared_reader(repo_path):
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    reader = multiprocessing.Process(target=hold_shared_lock, args=(repo_path, ready, release))
    reader.start()
    assert ready.wait(2)
    with pytest.raises(CodeCortexError) as exc:
        RepositoryLock(repo_path).acquire_once("exclusive", timeout_seconds=0.05)
    assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    release.set()
    reader.join(2)
    assert reader.exitcode == 0
```

- [ ] **Step 2: Run the test and confirm the lock implementation is missing**

Run: `pytest tests/integration/test_repository_lock.py -q`

Expected: collection fails on the missing `RepositoryLock`.

- [ ] **Step 3: Implement shared/exclusive `fcntl.flock` with monotonic timeout**

Create `.codecortex/.cache/repository.lock` with mode `0o600`. Poll non-blocking `LOCK_SH` or `LOCK_EX` until `time.monotonic()` exceeds the deadline, then close the descriptor and raise `LOCK_TIMEOUT`. Context exit always unlocks and closes; never delete the lock file.

```python
flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
deadline = time.monotonic() + timeout_seconds
while True:
    try:
        fcntl.flock(fd, flag | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            os.close(fd)
            raise CodeCortexError(ErrorCode.LOCK_TIMEOUT, "Repository lock timed out")
        time.sleep(0.01)
```

- [ ] **Step 4: Run process and crash-release tests**

Run: `pytest tests/integration/test_repository_lock.py -q`

Expected: shared/shared succeeds, shared/exclusive times out, exclusive/exclusive permits at most one holder, and a killed holder releases the OS lock.

- [ ] **Step 5: Commit locking**

```bash
git add src/codecortex/application/ports.py src/codecortex/infrastructure/locking.py tests/integration/test_repository_lock.py
git commit -m "feat: add repository process locking"
```

## Task 5: Define Revision-0 Formal State and Validation

**Files:**
- Create: `src/codecortex/domain/cognition.py`
- Create: `src/codecortex/infrastructure/formal.py`
- Create: `src/codecortex/application/services.py`
- Create: `tests/unit/domain/test_cognition.py`
- Create: `tests/integration/test_initialize_repository.py`

**Interfaces:**
- Produces: `Manifest`, `CognitiveGraph`, `EntityRefs`, `SourceBaseline`
- Produces: `FormalStore.load() -> FormalState`
- Produces: `ApplicationServices.initialize_repository() -> RepositoryOverview`
- Produces: `ApplicationServices.validate_graph() -> ValidationResult`

- [ ] **Step 1: Write failing revision-0 initialization tests**

```python
def test_initialize_creates_valid_empty_formal_state(app, repo_root):
    overview = app.initialize_repository()
    assert overview.graph_revision == 0
    assert overview.cognition_initialized is False
    assert overview.cognition_baseline_source_digest is None
    assert json.loads((repo_root / ".codecortex/source_baseline.json").read_text())["files"] == []
    assert app.validate_graph().valid is True


def test_initialize_is_idempotent(app, repo_root):
    app.initialize_repository()
    before = digest_tree(repo_root / ".codecortex")
    app.initialize_repository()
    assert digest_tree(repo_root / ".codecortex") == before
```

- [ ] **Step 2: Run the tests and confirm missing models/services**

Run: `pytest tests/unit/domain/test_cognition.py tests/integration/test_initialize_repository.py -q`

Expected: collection fails on missing formal-state types.

- [ ] **Step 3: Implement exact revision-0 files**

Create `.codecortex/manifest.json`, `config.toml`, `graph.json`, `entity_refs.json`, `source_baseline.json`, `PROJECT.md`, empty history/views directories, and deterministic empty views. `Manifest` has schema version 1, graph revision 0, `cognition_initialized=false`, null cognition baseline, and supported Core version. `SourceBaseline` has schema/profile/rule version 1, null repository digest, and empty files. Validation requires null/empty baseline exactly while `cognition_initialized=false`, rejects dangling approval IDs, wrong ID namespaces, non-empty graph without provenance, and mismatched manifest/graph revisions. M0 test apply may increase graph revision but must not change `cognition_initialized`.

```python
manifest = Manifest(schema_version=1, graph_revision=0, cognition_initialized=False, cognition_baseline=None)
baseline = SourceBaseline(
    schema_version=1,
    digest_profile_version=1,
    managed_source_set_version=1,
    repository_source_digest=None,
    files=(),
)
formal_store.initialize(FormalState.empty(manifest=manifest, source_baseline=baseline))
```

- [ ] **Step 4: Run initialization and corruption tests**

Run:

```bash
pytest tests/unit/domain/test_cognition.py tests/integration/test_initialize_repository.py -q
pytest tests/unit tests/integration -q
```

Expected: initialization is byte-idempotent; half-initialized and malformed states return `FORMAL_STATE_CORRUPT` without overwriting files.

- [ ] **Step 5: Commit formal state initialization**

```bash
git add src/codecortex/domain/cognition.py src/codecortex/application src/codecortex/infrastructure/formal.py tests
git commit -m "feat: initialize revision zero formal state"
```

## Task 6: Implement Proposal, Patch Digest, Revision, and Approval

**Files:**
- Create: `src/codecortex/domain/proposals.py`
- Create: `src/codecortex/infrastructure/pending.py`
- Modify: `src/codecortex/application/services.py`
- Create: `tests/unit/domain/test_proposals.py`
- Create: `tests/integration/test_pending_proposals.py`

**Interfaces:**
- Produces: `Proposal`, `ProposalRevision`, `ApprovalRecord`, `PatchOperation`
- Produces: `canonical_patch_digest(operations: tuple[PatchOperation, ...]) -> str`
- Produces: `create_cognitive_proposal`, `revise_cognitive_proposal`, `cognitive_proposal`

- [ ] **Step 1: Write failing state-machine and approval tests**

```python
def test_revision_changes_patch_digest_and_invalidates_old_approval(proposal):
    old = ApprovalRecord.for_test(proposal)
    revised = proposal.revise((add_node_operation("behavior.new"),), reason="add behavior")
    assert revised.patch_digest != proposal.patch_digest
    with pytest.raises(CodeCortexError) as exc:
        revised.verify_approval(old)
    assert exc.value.code is ErrorCode.APPROVAL_MISMATCH


def test_patch_digest_ignores_json_key_order():
    assert canonical_patch_digest((operation_a(),)) == canonical_patch_digest((operation_a_reordered(),))
```

- [ ] **Step 2: Run tests and confirm missing Proposal types**

Run: `pytest tests/unit/domain/test_proposals.py tests/integration/test_pending_proposals.py -q`

Expected: collection fails on missing Proposal symbols.

- [ ] **Step 3: Implement M0 Proposal operations and pending storage**

Support `add_node`, `update_node`, `remove_node`, `add_edge`, `update_edge`, and `remove_edge` addressed by stable IDs. A Proposal includes `prop_` ID, status, base graph revision, nullable analyzed source digest, source preconditions, operations, affected nodes, reason, evidence, uncertainties, revision log, patch digest, and UTC creation time. Pending files are deterministic JSON under `.codecortex/.cache/pending_proposals/`; revise appends a revision record and atomically replaces the pending file. Approval verification checks Proposal ID, current digest, `approved_by == "user"`, RFC 3339 UTC time, and a non-blank summary of at most 500 Unicode code points.

```python
def verify_approval(proposal: Proposal, approval: ApprovalRecord) -> None:
    if approval.proposal_id != proposal.proposal_id or approval.patch_digest != proposal.patch_digest:
        raise CodeCortexError(ErrorCode.APPROVAL_MISMATCH, "Approval does not match current proposal")
    if approval.approved_by != "user" or not approval.approval_summary.strip():
        raise CodeCortexError(ErrorCode.APPROVAL_REQUIRED, "Explicit user approval is required")
```

- [ ] **Step 4: Run all Proposal tests**

Run: `pytest tests/unit/domain/test_proposals.py tests/integration/test_pending_proposals.py -q`

Expected: illegal transitions, stale graph revision, digest mismatch, wrong namespaces, blank reasons, and unsupported operations are rejected with stable error codes.

- [ ] **Step 5: Commit Proposal lifecycle**

```bash
git add src/codecortex/domain/proposals.py src/codecortex/infrastructure/pending.py src/codecortex/application/services.py tests
git commit -m "feat: add cognitive proposal lifecycle"
```

## Task 7: Apply Approved Proposals with Recoverable Formal Transactions

**Files:**
- Create: `src/codecortex/infrastructure/views.py`
- Modify: `src/codecortex/infrastructure/formal.py`
- Modify: `src/codecortex/application/services.py`
- Create: `tests/integration/test_apply_transaction.py`
- Create: `tests/integration/test_transaction_recovery.py`

**Interfaces:**
- Produces: `ApplicationServices.apply_cognitive_proposal(proposal_id: str, approval: ApprovalRecord) -> ApplyResult`
- Produces: `FormalStore.recover() -> RecoveryResult`
- Produces: `render_views(graph: CognitiveGraph) -> Mapping[str, bytes]`

- [ ] **Step 1: Write failing happy-path and fault-injection tests**

```python
def test_apply_writes_event_graph_views_and_manifest_as_one_revision(app, repo_root, approved_proposal):
    result = app.apply_cognitive_proposal(approved_proposal.id, approved_proposal.approval)
    assert result.graph_revision == 1
    event = json.loads((repo_root / f".codecortex/history/events/{result.event_id}.json").read_text())
    graph = json.loads((repo_root / ".codecortex/graph.json").read_text())
    assert event["proposal_snapshot"]["proposal_id"] == approved_proposal.id
    assert graph["nodes"][0]["approval_event_id"] == result.event_id
    assert json.loads((repo_root / ".codecortex/manifest.json").read_text())["cognition_initialized"] is False


@pytest.mark.parametrize("fail_after", ["event", "graph", "entity_refs", "source_baseline", "views"])
def test_recovery_exposes_only_old_or_new_revision(transaction_fixture, fail_after):
    transaction_fixture.crash_after(fail_after)
    recovered = transaction_fixture.restart_and_recover()
    assert recovered.visible_revision in {0, 1}
    assert recovered.is_internally_consistent
```

- [ ] **Step 2: Run tests and confirm apply/recovery failures**

Run: `pytest tests/integration/test_apply_transaction.py tests/integration/test_transaction_recovery.py -q`

Expected: tests fail because formal transaction and renderer are missing.

- [ ] **Step 3: Implement journaled multi-file commit**

Under an exclusive repository lock: reload preconditions; apply the Patch in memory; validate the entire graph; assign an `evt_` ID; render all views; stage event, graph, entity refs, unchanged revision-0 source baseline, and views; fsync files and staging directory; save journal plus old-file backups; replace non-manifest targets; replace manifest last as commit marker; fsync parent directories; mark transaction complete. Recovery restores backups when manifest is old, completes cleanup when manifest is new, and raises `FORMAL_STATE_CORRUPT` when neither state can be proven. Do not silently repair unknown mixtures.

```text
exclusive_lock
  -> reload_and_verify_preconditions
  -> build_and_validate_new_state_in_memory
  -> stage_and_fsync_all_targets
  -> persist_journal_and_backups
  -> replace(event, graph, entity_refs, source_baseline, views)
  -> replace_manifest_commit_marker_last
  -> fsync_parent_directories
  -> mark_transaction_complete
```

- [ ] **Step 4: Run fault matrix, immutability, and full tests**

Run:

```bash
pytest tests/integration/test_apply_transaction.py tests/integration/test_transaction_recovery.py -q
pytest -q
ruff check src tests
mypy src
```

Expected: every injected interruption resolves to a complete old or new revision; an existing History Event cannot be overwritten.

- [ ] **Step 5: Commit formal apply**

```bash
git add src/codecortex/infrastructure src/codecortex/application/services.py tests
git commit -m "feat: apply approved cognition atomically"
```

## Task 8: Complete the CLI Contract

**Files:**
- Modify: `src/codecortex/interfaces/cli/main.py`
- Modify: `src/codecortex/__main__.py`
- Create: `tests/unit/interfaces/test_cli.py`
- Create: `tests/integration/test_cli_process.py`

**Interfaces:**
- Produces: `codecortex install-codex`, `doctor`, `validate`, and `mcp --profile main|analyzer`
- Consumes: `ApplicationServices`

- [ ] **Step 1: Write failing parser and exit-code tests**

```python
@pytest.mark.parametrize(
    ("error", "expected"),
    [(ErrorCode.NOT_INITIALIZED, 3), (ErrorCode.FORMAL_STATE_CORRUPT, 4), (ErrorCode.LOCK_TIMEOUT, 5)],
)
def test_stable_error_exit_codes(cli, error, expected):
    cli.services.validate_graph.side_effect = CodeCortexError(error, "failed")
    assert cli.run(["validate", "--json"]) == expected


def test_json_mode_keeps_diagnostics_off_stdout(run_codecortex):
    result = run_codecortex("validate", "--json")
    assert json.loads(result.stdout)["valid"] is True
    assert "INFO" not in result.stdout
```

- [ ] **Step 2: Run CLI tests and confirm unimplemented commands**

Run: `pytest tests/unit/interfaces/test_cli.py tests/integration/test_cli_process.py -q`

Expected: parser rejects `doctor`, `validate`, and `mcp` as unknown commands.

- [ ] **Step 3: Implement argparse adapters without business logic**

Add the exact commands from M0 section 3. Map exit codes 0, 2, 3, 4, 5, and 10; JSON errors use `CodeCortexError.to_dict()`. Human diagnostics and tracebacks go to stderr. `python -m codecortex` and the console script call the same `main()` function. Leave `install-codex`, `doctor`, and MCP behavior delegated through injected functions so unit tests do not touch the user home.

```python
ERROR_EXIT = {
    ErrorCode.NOT_INITIALIZED: 3,
    ErrorCode.FORMAL_STATE_CORRUPT: 4,
    ErrorCode.UNSUPPORTED_SCHEMA: 4,
    ErrorCode.LOCK_TIMEOUT: 5,
}
```

- [ ] **Step 4: Run parser, process, and stdout-purity tests**

Run: `pytest tests/unit/interfaces/test_cli.py tests/integration/test_cli_process.py -q`

Expected: all commands parse; bad profile exits 2; unexpected exception exits 10 without corrupting JSON stdout.

- [ ] **Step 5: Commit CLI contract**

```bash
git add src/codecortex/interfaces/cli src/codecortex/__main__.py tests
git commit -m "feat: expose CodeCortex CLI contract"
```

## Task 9: Expose Static Main and Analyzer MCP Profiles

**Files:**
- Create: `src/codecortex/interfaces/mcp/server.py`
- Create: `src/codecortex/interfaces/mcp/tools.py`
- Create: `tests/unit/interfaces/test_mcp_profiles.py`
- Create: `tests/integration/test_mcp_stdio.py`

**Interfaces:**
- Produces: `build_server(profile: Literal["main", "analyzer"], services: ApplicationServices) -> MCPServer`
- Produces Analyzer tools: `repository_overview`, `cognitive_graph`, `inspect_node`, `history_event`, `validate_graph`
- Produces Main tools: Analyzer set plus `initialize_repository`, `create_cognitive_proposal`, `revise_cognitive_proposal`, `cognitive_proposal`, `apply_cognitive_proposal`

- [ ] **Step 1: Write failing exact-allowlist tests**

```python
ANALYZER_TOOLS = {"repository_overview", "cognitive_graph", "inspect_node", "history_event", "validate_graph"}
MAIN_TOOLS = ANALYZER_TOOLS | {
    "initialize_repository", "create_cognitive_proposal", "revise_cognitive_proposal",
    "cognitive_proposal", "apply_cognitive_proposal",
}


@pytest.mark.anyio
async def test_profile_tool_lists(services):
    assert await listed_tool_names(build_server("analyzer", services)) == ANALYZER_TOOLS
    assert await listed_tool_names(build_server("main", services)) == MAIN_TOOLS
```

- [ ] **Step 2: Run tests and confirm missing server failure**

Run: `pytest tests/unit/interfaces/test_mcp_profiles.py tests/integration/test_mcp_stdio.py -q`

Expected: collection fails because MCP adapters are absent.

- [ ] **Step 3: Implement MCP SDK 2.x server and DTO adapters**

Import `MCPServer` from `mcp.server`, register functions with `@server.tool()`, and return Pydantic DTOs or JSON-compatible dictionaries containing `schema_version`. Build a fresh server per process and register from the static profile set; never register then hide write tools dynamically. Convert `CodeCortexError` to a tool error with its stable code and suggested action. Launch with STDIO transport and configure Python logging to stderr before protocol startup.

```python
def build_server(profile: Profile, services: ApplicationServices) -> MCPServer:
    server = MCPServer("CodeCortex")
    register_read_tools(server, services)
    if profile == "main":
        register_main_write_tools(server, services)
    return server
```

- [ ] **Step 4: Run in-memory and subprocess protocol tests**

Run:

```bash
pytest tests/unit/interfaces/test_mcp_profiles.py -q
pytest tests/integration/test_mcp_stdio.py -q
```

Expected: exact tool lists match; Analyzer call to `apply_cognitive_proposal` returns tool-not-found; two sequential STDIO requests succeed; deliberate stderr log does not alter protocol stdout; closing client stdin makes the MCP process finish its active atomic operation, close resources, and exit without a child daemon.

- [ ] **Step 5: Commit MCP profiles**

```bash
git add src/codecortex/interfaces/mcp tests/unit/interfaces/test_mcp_profiles.py tests/integration/test_mcp_stdio.py
git commit -m "feat: expose main and analyzer MCP profiles"
```

## Task 10: Install Codex Resources Idempotently

**Files:**
- Create: `src/codecortex/integrations/codex/install.py`
- Create: `src/codecortex/integrations/codex/resources/SKILL.md`
- Create: `src/codecortex/integrations/codex/resources/codecortex-analyzer.toml`
- Create: `docs/INSTALL.md`
- Create: `tests/unit/integrations/test_codex_install.py`
- Create: `tests/integration/test_codex_install_files.py`

**Interfaces:**
- Produces: `install_codex(home: Path, executable: Path, dry_run: bool, force: bool) -> InstallResult`
- Consumes: `RepositoryLock`-style exclusive installer lock and `tomlkit`

- [ ] **Step 1: Write failing idempotency and preservation tests**

```python
def test_second_install_has_no_diff_and_preserves_unknown_config(tmp_home, codecortex_exe):
    config = tmp_home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[features]\ncustom = true\n', encoding="utf-8")
    first = install_codex(tmp_home, codecortex_exe, dry_run=False, force=False)
    second = install_codex(tmp_home, codecortex_exe, dry_run=False, force=False)
    assert first.changed is True
    assert second.changed is False
    assert tomlkit.parse(config.read_text())["features"]["custom"] is True
```

- [ ] **Step 2: Run installer tests and confirm missing implementation**

Run: `pytest tests/unit/integrations/test_codex_install.py tests/integration/test_codex_install_files.py -q`

Expected: collection fails because `install_codex` is missing.

- [ ] **Step 3: Implement the ten-step installer algorithm**

Install packaged resources to `$HOME/.agents/skills/codecortex/SKILL.md` and `$HOME/.codex/agents/codecortex-analyzer.toml`; merge this exact product configuration while preserving unrelated TOML:

```toml
[mcp_servers.codecortex]
command = "/home/alice/.local/bin/codecortex"
args = ["mcp", "--profile", "main"]
required = false
startup_timeout_sec = 10
tool_timeout_sec = 120

[mcp_servers.codecortex.tools.apply_cognitive_proposal]
approval_mode = "prompt"
```

Acquire `.codex/.codecortex-install.lock`, reject symlink/non-regular targets, calculate dry-run changes, create timestamp-plus-random backups only when bytes change, atomically replace, reparse all outputs, and restore this run's backups on failure. `--force` only replaces CodeCortex-managed resource content and keys. Document `pipx install dist/codecortex-0.1.0-py3-none-any.whl` plus `codecortex install-codex` for users, and the exact `conda env create -f environment.yml` / `conda activate codecortex-dev` workflow for contributors; user installation must not require Conda.

- [ ] **Step 4: Run idempotency, rollback, and packaging tests**

Run:

```bash
pytest tests/unit/integrations/test_codex_install.py tests/integration/test_codex_install_files.py -q
python -m build
python -c "import zipfile,glob; z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]); assert any(n.endswith('resources/SKILL.md') for n in z.namelist())"
```

Expected: comments and unknown keys survive; second install is byte-identical; a forced validation failure restores every changed target; wheel contains both resources.

- [ ] **Step 5: Commit Codex installation**

```bash
git add src/codecortex/integrations/codex pyproject.toml docs/INSTALL.md tests
git commit -m "feat: install CodeCortex into Codex"
```

## Task 11: Add `doctor` and End-to-End Local Diagnostics

**Files:**
- Create: `src/codecortex/integrations/codex/doctor.py`
- Modify: `src/codecortex/interfaces/cli/main.py`
- Create: `tests/unit/integrations/test_doctor.py`
- Create: `tests/integration/test_doctor_cli.py`

**Interfaces:**
- Produces: `run_doctor(home: Path, executable: Path, repository: Repository | None) -> DoctorReport`

- [ ] **Step 1: Write failing structured diagnostic tests**

```python
def test_doctor_reports_actionable_mcp_mismatch(tmp_home, codecortex_exe):
    install_codex(tmp_home, codecortex_exe, dry_run=False, force=False)
    replace_mcp_command(tmp_home, "/missing/codecortex")
    report = run_doctor(tmp_home, codecortex_exe, repository=None)
    check = report.by_code("CODEX_MCP_COMMAND")
    assert check.ok is False
    assert check.action == "Run `codecortex install-codex` again."
```

- [ ] **Step 2: Run tests and confirm missing diagnostics**

Run: `pytest tests/unit/integrations/test_doctor.py tests/integration/test_doctor_cli.py -q`

Expected: collection fails because `DoctorReport` is missing.

- [ ] **Step 3: Implement read-only checks**

Check Python/package version, resolved executable, packaged and installed resource digests, Analyzer TOML parse, Main MCP command/args/approval mode, writable parent directories without modifying them, repository formal file presence, schema compatibility, and exact profile allowlists. Return `ok|warning|error` per check and an overall nonzero CLI result only for errors. Never print TOML secret values and never auto-repair.

```python
checks = (
    check_python_version(),
    check_executable(executable),
    check_installed_resources(home),
    check_main_mcp_config(home, executable),
    check_profile_allowlists(),
    check_repository_state(repository),
)
return DoctorReport(checks=checks)
```

- [ ] **Step 4: Run human and JSON diagnostic tests**

Run: `pytest tests/unit/integrations/test_doctor.py tests/integration/test_doctor_cli.py -q`

Expected: clean install passes; missing resource, wrong executable, malformed TOML, unsupported schema, and secret redaction cases produce stable codes and actions.

- [ ] **Step 5: Commit doctor**

```bash
git add src/codecortex/integrations/codex/doctor.py src/codecortex/interfaces/cli/main.py tests
git commit -m "feat: diagnose CodeCortex Codex integration"
```

## Task 12: Verify Concurrency and Real Child Codex Behavior

**Files:**
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_codex_m0.py`
- Create: `tests/fixtures/m0_repo/README.md`
- Create: `scripts/run_codex_e2e.py`
- Create: `docs/testing/M0_ACCEPTANCE.md`

**Interfaces:**
- Consumes: installed `codex` CLI, packaged CodeCortex, temporary user configuration
- Produces: sanitized JSONL traces under a caller-selected temporary artifact directory

- [ ] **Step 1: Write the opt-in acceptance tests and precondition skips**

```python
@pytest.mark.codex_e2e
def test_unapproved_then_resumed_approved_flow(codex_harness):
    first = codex_harness.start_persistent("$codecortex create the fixture proposal but do not approve it")
    assert first.graph_revision == 0
    second = codex_harness.resume(first.thread_id, "I approve the displayed current patch digest")
    assert second.graph_revision == 1
    assert second.history_event["approval_record"]["approved_by"] == "user"


@pytest.mark.codex_e2e
def test_native_codex_survives_broken_codecortex(codex_harness):
    result = codex_harness.run_ephemeral("Read README.md and return its first heading", break_mcp=True)
    assert result.final_answer.strip() == "M0 Fixture"
```

- [ ] **Step 2: Run without credentials and confirm an explicit skip**

Run: `pytest tests/e2e/test_codex_m0.py -m codex_e2e -q`

Expected: tests skip with `Codex CLI/model access not configured`, rather than fail or silently fake Codex.

- [ ] **Step 3: Implement the isolated harness**

Create a temporary Git repository and task-specific temporary Codex home. Single-turn cases use `codex exec --ephemeral --json`; the approval case omits `--ephemeral`, captures the thread ID, then uses `codex exec resume`. Its test-only config sets host `approval_mode = "approve"`, while assertions still require first-turn non-application and Core approval-record validation. Sanitize tokens and absolute paths from traces. Add a separate manual procedure using the product `prompt` configuration in VS Code.

```python
single_turn = ["codex", "exec", "--ephemeral", "--json", prompt]
approval_first = ["codex", "exec", "--json", first_prompt]
approval_second = ["codex", "exec", "resume", thread_id, approval_prompt]
```

- [ ] **Step 4: Run the complete automated M0 gate**

Run:

```bash
pytest -q
ruff check src tests scripts
mypy src
python -m build
python scripts/run_codex_e2e.py --artifact-dir /tmp/codecortex-m0-artifacts
git status --short
```

Expected: unit/integration tests pass; configured Child Codex cases pass; repository status is clean; manual VS Code host-prompt result is recorded in `docs/testing/M0_ACCEPTANCE.md` before declaring M0 complete.

- [ ] **Step 5: Commit the M0 acceptance harness**

```bash
git add tests/e2e tests/fixtures scripts/run_codex_e2e.py docs/testing/M0_ACCEPTANCE.md
git commit -m "test: add M0 Codex acceptance gate"
```

## M0 Completion Gate

Run from a clean checkout in the Python 3.14 Conda environment:

```bash
pytest -q
ruff check src tests scripts
mypy src
python -m build
codecortex doctor --json
python scripts/run_codex_e2e.py --artifact-dir /tmp/codecortex-m0-final
git diff --check
git status --short
```

M0 is complete only when all automated commands exit 0, the worktree is clean, the Analyzer MCP tool list contains no writes, concurrent apply exposes one complete revision, recovery passes every fault point, and the separate VS Code `prompt` smoke test is recorded as passed.
