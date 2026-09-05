"""M1b freshness MCP tools over a real formal state and disposable cache."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.preflight import PreflightService
from codecortex.application.services import ApplicationServices
from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    Manifest,
    SourceBaseline,
)
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.repository import Repository
from codecortex.interfaces.mcp.server import build_server

M1B_READ_TOOLS = {
    "cognitive_freshness",
    "pending_changes",
    "effective_query_freshness",
}


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _services(tmp_path: Path) -> tuple[ApplicationServices, Path]:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    repository = Repository(root)
    _write(root, "app.py", "def baseline() -> int:\n    return 1\n")
    lock = RepositoryLock(root)
    formal_store = FormalStore(repository)
    seed_sync = FactSyncService(repository, repository_lock=lock)
    seed = seed_sync.sync()
    baseline = SourceBaseline(
        1,
        1,
        1,
        seed.repository_source_digest,
        tuple(
            {"relative_path": path, "content_digest": digest}
            for path, digest in seed_sync.database.source_file_digests().items()
        ),
    )
    formal_store.initialize(
        FormalState(
            manifest=Manifest(1, 0, True, seed.repository_source_digest),
            graph=CognitiveGraph.empty(),
            entity_refs=EntityRefs.empty(),
            source_baseline=baseline,
        )
    )
    sync = FactSyncService(repository, formal_store=formal_store, repository_lock=lock)
    sync.database.replace_baseline_entity_snapshots(seed.repository_source_digest)
    freshness_store = FreshnessStore(root / ".codecortex" / ".cache")
    preflight = PreflightService(
        formal_store=formal_store,
        fact_sync=sync,
        facts=sync.database,
        freshness_store=freshness_store,
        repository_lock=lock,
    )
    return (
        ApplicationServices(
            repository=repository,
            formal_store=formal_store,
            repository_lock=lock,
            fact_sync=sync,
            preflight_service=preflight,
        ),
        root,
    )


async def _tool_names(server: object) -> set[str]:
    tools = await server.list_tools()  # type: ignore[attr-defined]
    return {tool.name for tool in tools}


def _payload(result: object) -> dict[str, object]:
    return result.structured_content  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_freshness_tools_are_readable_by_both_profiles(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)

    assert M1B_READ_TOOLS <= await _tool_names(build_server("main", services))
    assert M1B_READ_TOOLS <= await _tool_names(build_server("analyzer", services))
    assert "advance_cognition_baseline" in await _tool_names(build_server("main", services))
    assert "advance_cognition_baseline" not in await _tool_names(
        build_server("analyzer", services)
    )
    assert "sync_repository_facts" not in await _tool_names(
        build_server("analyzer", services)
    )


@pytest.mark.anyio
async def test_main_preflights_and_analyzer_reads_prepared_freshness(
    tmp_path: Path,
) -> None:
    services, root = _services(tmp_path)
    _write(root, "app.py", "def baseline() -> int:\n    return 2\n")

    main = build_server("main", services)
    result = _payload(await main.call_tool("cognitive_freshness", {}))

    assert result["repository_status"] in {"pending", "unresolved"}
    assert result["baseline_source_digest"] != result["current_source_digest"]
    assert result["change_set_id"] is not None

    analyzer = build_server("analyzer", services)
    with patch.object(services, "run_preflight", wraps=services.run_preflight) as run:
        analyzer_result = _payload(await analyzer.call_tool("cognitive_freshness", {}))

    run.assert_not_called()
    assert analyzer_result["change_set_id"] == result["change_set_id"]
    assert analyzer_result["current_source_digest"] == result["current_source_digest"]


@pytest.mark.anyio
async def test_every_main_m1a_query_route_runs_preflight_but_analyzer_never_does(
    tmp_path: Path,
) -> None:
    services, _ = _services(tmp_path)
    calls = {
        "cognitive_graph": {},
        "inspect_node": {"node_id": "behavior.missing"},
        "repository_facts": {"scope": "app"},
        "analysis_scope": {},
        "resolve_entity_context": {"path": "app.py"},
        "get_discussion_context": {"node_ids": ["behavior.missing"]},
        "search_cognitive_graph": {"query": "missing"},
    }

    main = build_server("main", services)
    with patch.object(services, "run_preflight", wraps=services.run_preflight) as run:
        for name, arguments in calls.items():
            try:
                await main.call_tool(name, arguments)
            except ToolError:
                pass
            assert run.call_count == 1
            run.reset_mock()

    analyzer = build_server("analyzer", services)
    with patch.object(services, "run_preflight", wraps=services.run_preflight) as run:
        for name, arguments in calls.items():
            try:
                await analyzer.call_tool(name, arguments)
            except ToolError:
                pass
        run.assert_not_called()


@pytest.mark.anyio
async def test_pending_changes_is_bounded_and_query_inputs_are_bounded(
    tmp_path: Path,
) -> None:
    services, root = _services(tmp_path)
    _write(
        root,
        "app.py",
        "\n".join(
            ["def baseline() -> int:", "    return 2", ""]
            + [f"def added_{index}() -> int:\n    return {index}\n" for index in range(30)]
        ),
    )
    main = build_server("main", services)

    pending = _payload(
        await main.call_tool("pending_changes", {"cursor": None, "limit": 20})
    )

    assert len(pending["changed_entities"]) <= 20
    assert pending["truncated"] is True
    assert pending["cursor"] is not None

    query = _payload(
        await main.call_tool(
            "effective_query_freshness", {"node_ids": ["behavior.unrelated"]}
        )
    )
    assert query["status"] == "unknown_source_first"
    assert query["reason_codes"]

    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await main.call_tool(
            "effective_query_freshness",
            {"node_ids": [f"behavior.{index}" for index in range(101)]},
        )
