"""End-to-end regression over the production CLI composition root.

These tests exist because every pre-review apply test wired replica providers
by hand while the production root did not, which let the read plane break
unnoticed.  They compose through ``_default_services`` exactly as the CLI/MCP
entrypoints do and assert that guarded reads work before and after an
analysis-backed apply, with zero cache warnings.
"""

from __future__ import annotations

import gc
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codecortex.application.services import ApplicationServices
from codecortex.domain.proposals import ApprovalRecord
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.interfaces.cli.main import _default_services
from codecortex.interfaces.mcp.server import build_server

APPROVED_AT = "2026-09-03T08:00:00Z"


def _tool_error_code(error: ToolError) -> str:
    message = str(error)
    payload = json.loads(message[message.index("{") :])
    return payload["error"]["code"]


def _write_test_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text(
        '"""Package."""\n', encoding="utf-8"
    )
    (root / "pkg" / "core.py").write_text(
        '"""Core module."""\n\n\n'
        "def greet(name: str) -> str:\n"
        '    return f"hello {name}"\n',
        encoding="utf-8",
    )


def _cache_snapshot(root: Path) -> dict[str, bytes]:
    cache = root / ".codecortex" / ".cache"
    if not cache.exists():
        return {}
    return {
        path.relative_to(cache).as_posix(): path.read_bytes()
        for path in sorted(cache.rglob("*"))
        if path.is_file()
    }


def _normalize_sqlite_sidecars(database: FactsDatabase | GraphReplica) -> None:
    connection = database.open_write()
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()
    assert checkpoint is not None and checkpoint[0] == 0
    gc.collect()
    for suffix in ("-wal", "-shm"):
        Path(f"{database.path}{suffix}").unlink(missing_ok=True)


def _ulid(prefix: str, index: int) -> str:
    suffix = format(index, "X")
    return f"{prefix}_01J{'0' * (23 - len(suffix))}{suffix}"


def _initialize_cognition(services: ApplicationServices) -> None:
    services.initialize_repository()
    services.synchronize_facts("full")
    _approve_and_apply(
        services,
        _report_payload(
            services,
            nodes=[
                {
                    "id": "responsibility.read-plane",
                    "kind": "responsibility",
                    "title": "Read plane",
                }
            ],
            edges=[],
            mappings=[],
        ),
    )


@pytest.fixture
def production_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ApplicationServices:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    services = _default_services()
    services.initialize_repository()
    services.synchronize_facts("full")
    return services


@pytest.mark.anyio
async def test_main_mcp_bootstrap_reads_require_full_sync_but_not_cognition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    services = _default_services()
    server = build_server("main", services)

    with pytest.raises(ToolError) as overview:
        await server.call_tool("repository_overview", {})
    initialized = await server.call_tool("initialize_repository", {})
    assert _tool_error_code(overview.value) == "NOT_INITIALIZED"
    assert initialized.structured_content["cognition_initialized"] is False

    with pytest.raises(ToolError) as unsynchronized:
        await server.call_tool("analysis_scope", {})
    assert _tool_error_code(unsynchronized.value) == "CACHE_REBUILD_REQUIRED"

    synced = await server.call_tool("sync_repository_facts", {"mode": "full"})
    scope = await server.call_tool(
        "analysis_scope",
        {
            "expected_graph_revision": synced.structured_content["graph_revision"],
            "expected_source_digest": synced.structured_content[
                "repository_source_digest"
            ],
        },
    )
    facts = await server.call_tool(
        "repository_facts",
        {
            "scope": "pkg.core",
            "expected_graph_revision": synced.structured_content["graph_revision"],
            "expected_source_digest": synced.structured_content[
                "repository_source_digest"
            ],
        },
    )
    assert scope.is_error is False
    assert scope.structured_content["totals"]["file_count"] == 2
    assert facts.is_error is False
    assert any(item["name"] == "greet" for item in facts.structured_content["entities"])

    with pytest.raises(ToolError) as m1b_query:
        await server.call_tool("cognitive_graph", {})
    assert _tool_error_code(m1b_query.value) == "NOT_INITIALIZED"


@pytest.mark.anyio
async def test_analyzer_query_routes_reject_uninitialized_bootstrap_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    main = _default_services()
    main.initialize_repository()
    main.synchronize_facts("full")
    analyzer = build_server("analyzer", _default_services("analyzer"))
    calls = {
        "cognitive_graph": {},
        "inspect_node": {"node_id": "behavior.missing"},
        "repository_facts": {"scope": "pkg"},
        "analysis_scope": {},
        "resolve_entity_context": {"path": "pkg/a.py"},
        "get_discussion_context": {"node_ids": ["behavior.missing"]},
        "search_cognitive_graph": {"query": "missing"},
        "history_event": {"event_id": _ulid("evt", 99)},
        "validate_graph": {},
        "cognitive_freshness": {},
        "pending_changes": {},
        "effective_query_freshness": {},
    }

    for tool_name, arguments in calls.items():
        with pytest.raises(ToolError) as raised:
            await analyzer.call_tool(tool_name, arguments)
        assert _tool_error_code(raised.value) == "NOT_INITIALIZED"


@pytest.mark.anyio
async def test_bootstrap_read_rechecks_uninitialized_state_after_two_main_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    first_main = _default_services()
    second_main = _default_services()
    first_main.initialize_repository()
    first_main.synchronize_facts("full")
    report = _report_payload(
        second_main,
        nodes=[
            {
                "id": "responsibility.bootstrap-race",
                "kind": "responsibility",
                "title": "Bootstrap race",
            }
        ],
        edges=[],
        mappings=[],
    )
    original_guard = first_main.run_m1a_bootstrap_read

    def initialize_between_preflight_and_query(operation):
        applied = _approve_and_apply(second_main, report)
        assert applied.graph_revision == 1
        return original_guard(operation)

    with patch.object(
        first_main,
        "run_m1a_bootstrap_read",
        side_effect=initialize_between_preflight_and_query,
    ), pytest.raises(ToolError) as raised:
        await build_server("main", first_main).call_tool("analysis_scope", {})

    assert _tool_error_code(raised.value) == "NOT_INITIALIZED"


@pytest.mark.anyio
async def test_bootstrap_read_rebuilds_deleted_replica_without_reinitialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    first_main = _default_services()
    first_main.initialize_repository()
    first_main.synchronize_facts("full")
    formal_before = {
        path.name: path.read_bytes()
        for path in (root / ".codecortex").glob("*.json")
    }
    shutil.rmtree(root / ".codecortex" / ".cache")

    restarted_main = _default_services()
    restarted_main.synchronize_facts("full")
    with patch.object(
        restarted_main.formal_store,
        "initialize",
        wraps=restarted_main.formal_store.initialize,
    ) as initialize:
        result = await build_server("main", restarted_main).call_tool(
            "analysis_scope", {}
        )

    assert result.is_error is False
    assert result.structured_content["graph_revision"] == 0
    initialize.assert_not_called()
    assert {
        path.name: path.read_bytes()
        for path in (root / ".codecortex").glob("*.json")
    } == formal_before


@pytest.mark.anyio
@pytest.mark.parametrize("cache_state", ["missing", "corrupt", "existing"])
async def test_analyzer_composition_never_mutates_cache(
    cache_state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)

    main = _default_services()
    _initialize_cognition(main)
    assert main.fact_sync is not None
    assert main.cognitive_replica is not None
    for database in (main.fact_sync.database, main.cognitive_replica):
        _normalize_sqlite_sidecars(database)
    if cache_state == "missing":
        cache = root / ".codecortex" / ".cache"
        shutil.rmtree(cache)
    elif cache_state == "corrupt":
        cache = root / ".codecortex" / ".cache"
        (cache / "facts.sqlite3").write_bytes(b"corrupt facts")
        (cache / "cognitive.sqlite3").write_bytes(b"corrupt graph")

    before = _cache_snapshot(root)
    analyzer = _default_services("analyzer")
    assert analyzer.cognitive_replica is not None
    assert analyzer.preflight_service is not None
    assert analyzer.preflight_service.recovery_service is None
    with pytest.raises(sqlite3.OperationalError, match="cannot be reset"):
        analyzer.cognitive_replica.reset()
    result_error: ToolError | None = None
    try:
        result = await build_server("analyzer", analyzer).call_tool(
            "analysis_scope", {}
        )
    except ToolError as error:
        result_error = error
    after = _cache_snapshot(root)

    assert after == before
    assert (root / ".codecortex" / ".cache").exists() is (cache_state != "missing")
    if cache_state == "existing":
        assert result_error is None
        assert result.is_error is False
    else:
        assert result_error is not None
        assert _tool_error_code(result_error) == "CACHE_REBUILD_REQUIRED"


@pytest.mark.anyio
async def test_analyzer_reads_existing_live_wal_sidecars_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    main = _default_services()
    _initialize_cognition(main)
    assert main.fact_sync is not None
    assert main.cognitive_replica is not None
    facts_connection = main.fact_sync.database.open_write()
    facts_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    facts_connection.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{main.fact_sync.database.path}{suffix}").unlink(missing_ok=True)
    replica_connection = main.cognitive_replica.open_write()
    replica_reader: sqlite3.Connection | None = None
    try:
        replica_connection.execute("CREATE TABLE wal_probe(value INTEGER)")
        replica_connection.execute("INSERT INTO wal_probe VALUES (1)")
        replica_connection.commit()
        replica_connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        replica_reader = main.cognitive_replica.open_read()
        replica_reader.execute("SELECT * FROM replica_metadata").fetchone()
        sidecars = tuple(
            Path(f"{main.cognitive_replica.path}{suffix}")
            for suffix in ("-wal", "-shm")
        )
        assert all(path.is_file() for path in sidecars)
        before = _cache_snapshot(root)

        result = await build_server("analyzer", _default_services("analyzer")).call_tool(
            "analysis_scope", {}
        )

        assert result.is_error is False
        assert _cache_snapshot(root) == before
    finally:
        if replica_reader is not None:
            replica_reader.close()
        replica_connection.close()


@pytest.mark.anyio
async def test_analyzer_rejects_wal_without_shm_without_creating_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    main = _default_services()
    _initialize_cognition(main)
    assert main.fact_sync is not None
    facts_wal = Path(f"{main.fact_sync.database.path}-wal")
    facts_shm = Path(f"{main.fact_sync.database.path}-shm")
    _normalize_sqlite_sidecars(main.fact_sync.database)
    facts_wal.write_bytes(b"orphaned-write-ahead-log")
    wal_before = facts_wal.read_bytes()

    with pytest.raises(ToolError) as raised:
        await build_server("analyzer", _default_services("analyzer")).call_tool(
            "repository_facts", {"scope": "pkg.core"}
        )

    assert _tool_error_code(raised.value) == "CACHE_REBUILD_REQUIRED"
    assert not facts_shm.exists()
    assert facts_wal.read_bytes() == wal_before


@pytest.mark.anyio
async def test_analyzer_rejects_incomplete_wal_sidecar_pair_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    main = _default_services()
    _initialize_cognition(main)
    assert main.fact_sync is not None
    facts_wal = Path(f"{main.fact_sync.database.path}-wal")
    facts_shm = Path(f"{main.fact_sync.database.path}-shm")
    _normalize_sqlite_sidecars(main.fact_sync.database)
    facts_shm.write_bytes(b"orphaned-shared-memory")
    before_shm = facts_shm.read_bytes()

    with pytest.raises(ToolError) as raised:
        await build_server("analyzer", _default_services("analyzer")).call_tool(
            "analysis_scope", {}
        )

    assert _tool_error_code(raised.value) == "CACHE_REBUILD_REQUIRED"
    assert facts_shm.read_bytes() == before_shm
    assert not facts_wal.exists()


@pytest.mark.parametrize(
    "cache_name",
    (
        "facts.sqlite3",
        "cognitive.sqlite3",
        "facts.sqlite3-wal",
        "cognitive.sqlite3-shm",
    ),
)
def test_analyzer_sqlite_paths_reject_database_and_sidecar_symlinks(
    cache_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)
    main = _default_services()
    _initialize_cognition(main)
    assert main.fact_sync is not None
    assert main.cognitive_replica is not None
    for database in (main.fact_sync.database, main.cognitive_replica):
        _normalize_sqlite_sidecars(database)

    target = root / ".codecortex" / ".cache" / cache_name
    outside = tmp_path / f"outside-{cache_name}"
    if cache_name.endswith(".sqlite3"):
        target.replace(outside)
    else:
        outside.write_bytes(b"outside-sidecar")
        peer_suffix = "-shm" if cache_name.endswith("-wal") else "-wal"
        target.with_name(target.name.rsplit("-", 1)[0] + peer_suffix).write_bytes(
            b"peer-sidecar"
        )
    target.symlink_to(outside)
    outside_before = outside.read_bytes()
    analyzer = _default_services("analyzer")
    assert analyzer.fact_sync is not None
    assert analyzer.cognitive_replica is not None
    database = (
        analyzer.fact_sync.database
        if cache_name.startswith("facts")
        else analyzer.cognitive_replica
    )

    with pytest.raises(sqlite3.OperationalError, match="unsafe"):
        database.open_read()

    assert outside.read_bytes() == outside_before


def _report_payload(
    services: ApplicationServices,
    *,
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    mappings: list[dict[str, object]],
) -> bytes:
    assert services.initialization_service is not None
    coordinate = services.initialization_service.begin_analysis()
    payload = {
        "schema_version": 1,
        "base_graph_revision": coordinate.graph_revision,
        "analyzed_source_digest": coordinate.source_digest,
        "analysis_scope": {"mode": "repository", "files": 2, "modules": 1},
        "coverage": {"analyzed_partitions": ["pkg"], "unexamined_partitions": []},
        "candidate_nodes": nodes,
        "candidate_edges": edges,
        "candidate_flows": [],
        "candidate_mappings": mappings,
        "evidence": [],
        "uncertainties": ["scoped regression report"],
        "unmapped_regions": [],
        "diagnostics": [],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _approve_and_apply(services: ApplicationServices, payload: bytes):
    proposal = services.create_cognitive_proposal_from_analysis(
        payload, "Initialize repository understanding"
    )
    return services.apply_cognitive_proposal(
        proposal.proposal_id,
        ApprovalRecord(
            proposal_id=proposal.proposal_id,
            patch_digest=proposal.patch_digest,
            approved_by="user",
            approved_at=APPROVED_AT,
            approval_summary="Approve current patch",
        ),
    )


def test_analysis_apply_with_entity_mapping_then_guarded_reads(
    production_services: ApplicationServices,
) -> None:
    services = production_services
    # Guarded reads already work before any apply: initialization rebuilt the
    # revision-0 replica through the production composition root.
    page = services.repository_facts("pkg.core", None, 10)
    entity = next(item for item in page.entities if item.name == "greet")
    evidence = {
        "id": _ulid("evid", 1),
        "kind": "code_entity",
        "entity_uid": entity.uid,
        "relative_path": entity.relative_path,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
        "observation": "greeting entry point",
    }
    payload = _report_payload(
        services,
        nodes=[
            {"id": "responsibility.greeting", "kind": "responsibility",
             "title": "Greeting"},
            {"id": "behavior.greet-user", "kind": "behavior",
             "title": "Greet user", "evidence": [evidence]},
            {"id": "capability.text-format", "kind": "capability",
             "title": "Text formatting"},
        ],
        edges=[
            {
                "id": _ulid("edge", 1),
                "type": "contains",
                "source_id": "responsibility.greeting",
                "target_id": "behavior.greet-user",
                "epistemic_status": "inferred",
                "evidence": [dict(evidence, id=_ulid("evid", 2))],
            },
            {
                "id": _ulid("edge", 2),
                "type": "uses",
                "source_id": "behavior.greet-user",
                "target_id": "capability.text-format",
                "epistemic_status": "inferred",
                "evidence": [dict(evidence, id=_ulid("evid", 3))],
            },
        ],
        mappings=[
            {
                "id": _ulid("map", 1),
                "subject_kind": "node",
                "subject_id": "behavior.greet-user",
                "entity_uid": entity.uid,
                "role": "primary",
                "resolution_status": "resolved",
                "evidence_note": "greeting entry point",
            }
        ],
    )

    result = _approve_and_apply(services, payload)

    assert result.graph_revision == 1
    assert result.cache_warnings == ()
    facts = services.repository_facts(
        "pkg.core", None, 10, expected_graph_revision=1
    )
    assert len(facts.entities) > 0
    hits = services.search_cognitive_graph("Greet user", expected_graph_revision=1)
    assert any(hit.node_id == "behavior.greet-user" for hit in hits.hits)


def test_contains_edge_without_evidence_applies_and_stays_readable(
    production_services: ApplicationServices,
) -> None:
    services = production_services
    payload = _report_payload(
        services,
        nodes=[
            {"id": "responsibility.greeting", "kind": "responsibility",
             "title": "Greeting"},
            {"id": "behavior.greet-user", "kind": "behavior",
             "title": "Greet user"},
        ],
        edges=[
            {
                "id": _ulid("edge", 1),
                "type": "contains",
                "source_id": "responsibility.greeting",
                "target_id": "behavior.greet-user",
                "epistemic_status": "inferred",
            }
        ],
        mappings=[],
    )

    result = _approve_and_apply(services, payload)

    assert result.graph_revision == 1
    assert result.cache_warnings == ()
    hits = services.search_cognitive_graph("Greet user", expected_graph_revision=1)
    assert any(hit.node_id == "behavior.greet-user" for hit in hits.hits)
