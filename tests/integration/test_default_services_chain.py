"""End-to-end regression over the production CLI composition root.

These tests exist because every pre-review apply test wired replica providers
by hand while the production root did not, which let the read plane break
unnoticed.  They compose through ``_default_services`` exactly as the CLI/MCP
entrypoints do and assert that guarded reads work before and after an
analysis-backed apply, with zero cache warnings.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codecortex.application.services import ApplicationServices
from codecortex.domain.proposals import ApprovalRecord
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


def _ulid(prefix: str, index: int) -> str:
    suffix = format(index, "X")
    return f"{prefix}_01J{'0' * (23 - len(suffix))}{suffix}"


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
    assert _tool_error_code(unsynchronized.value) in {
        "NOT_INITIALIZED",
        "CACHE_REBUILD_REQUIRED",
    }

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
@pytest.mark.parametrize("cache_state", ["missing", "corrupt", "existing"])
async def test_analyzer_composition_never_mutates_cache(
    cache_state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_test_repository(root)
    monkeypatch.chdir(root)

    main = _default_services()
    main.initialize_repository()
    if cache_state == "missing":
        cache = root / ".codecortex" / ".cache"
        for path in cache.glob("*"):
            path.unlink()
        cache.rmdir()
    elif cache_state == "corrupt":
        cache = root / ".codecortex" / ".cache"
        assert main.cognitive_replica is not None
        connection = main.cognitive_replica.open_write()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        for suffix in ("-wal", "-shm"):
            (cache / f"cognitive.sqlite3{suffix}").unlink(missing_ok=True)
        (cache / "facts.sqlite3").write_bytes(b"corrupt facts")
        (cache / "cognitive.sqlite3").write_bytes(b"corrupt graph")
    else:
        main.synchronize_facts("full")
        assert main.fact_sync is not None
        assert main.cognitive_replica is not None
        for database in (main.fact_sync.database, main.cognitive_replica):
            connection = database.open_write()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            for suffix in ("-wal", "-shm"):
                Path(f"{database.path}{suffix}").unlink(missing_ok=True)

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
