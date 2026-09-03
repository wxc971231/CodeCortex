"""End-to-end M1a MCP tools over real repositories, caches, and servers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.query import (
    CacheCoordinate,
    CurrentSourceLocation,
    NodeInspection,
    QueryService,
    ResolvedMapping,
)
from codecortex.application.replica_providers import (
    formal_entity_ref_provider,
    formal_history_event_provider,
)
from codecortex.application.services import ApplicationServices
from codecortex.domain.proposals import ApprovalRecord, PatchOperation
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views
from codecortex.interfaces.mcp.server import build_server
from tests.conftest import build_discussion_graph

M1A_READ_TOOLS = {
    "inspect_node",
    "repository_facts",
    "analysis_scope",
    "resolve_entity_context",
    "get_discussion_context",
    "search_cognitive_graph",
}
APPROVED_AT = "2026-09-03T02:00:00Z"


def _compose(root: Path, *, max_graph_objects: int = 500) -> ApplicationServices:
    repository = Repository(root)
    formal_store = FormalStore(repository)
    lock = RepositoryLock(root)
    cache_directory = root / ".codecortex" / ".cache"
    return ApplicationServices(
        repository=repository,
        formal_store=formal_store,
        repository_lock=lock,
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
        fact_sync=FactSyncService(
            repository, formal_store=formal_store, repository_lock=lock
        ),
        query_service=QueryService(
            formal_store=formal_store,
            facts=FactsDatabase(cache_directory / "facts.sqlite3"),
            replica=GraphReplica(
                cache_directory / "cognitive.sqlite3",
                entity_refs=formal_entity_ref_provider(formal_store),
                history_events=formal_history_event_provider(formal_store),
            ),
            repository_lock=lock,
        ),
        cognitive_graph_max_objects=max_graph_objects,
    )


def _add_node(node_id: str) -> PatchOperation:
    return PatchOperation(
        "add_node", node_id, {"id": node_id, "kind": "behavior", "title": node_id}
    )


def _apply_node(app: ApplicationServices, node_id: str) -> None:
    proposal = app.create_cognitive_proposal(
        operations=(_add_node(node_id),),
        affected_nodes=(node_id,),
        reason=f"Add {node_id}",
    )
    app.apply_cognitive_proposal(
        proposal.proposal_id,
        ApprovalRecord(
            proposal_id=proposal.proposal_id,
            patch_digest=proposal.patch_digest,
            approved_by="user",
            approved_at=APPROVED_AT,
            approval_summary="Approve current patch",
        ),
    )


def _write_sources(root: Path) -> None:
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (package / "core.py").write_text(
        '"""Core module."""\n\n\n'
        "def greet(name: str) -> str:\n"
        '    return f"hello {name}"\n\n\n'
        "class Greeter:\n"
        "    def run(self) -> str:\n"
        '        return greet("world")\n',
        encoding="utf-8",
    )
    (package / "user.py").write_text(
        '"""User module."""\n\n'
        "from pkg.core import Greeter\n\n\n"
        "def use() -> str:\n"
        "    return Greeter().run()\n",
        encoding="utf-8",
    )


@pytest.fixture
def m1a_repo(
    tmp_path: Path, replica_entity_refs, replica_history_events
) -> ApplicationServices:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _write_sources(root)
    app = _compose(root)
    app.initialize_repository()
    _apply_node(app, "behavior.answer-question")
    app.synchronize_facts("auto")
    replica = GraphReplica.create_new(
        root / ".codecortex" / ".cache" / "cognitive.sqlite3",
        entity_refs=replica_entity_refs,
        history_events=replica_history_events,
    )
    replica.rebuild(build_discussion_graph(graph_revision=1), 1)
    return app


class _McpProfiles:
    def __init__(self, services: ApplicationServices) -> None:
        self._services = services

    async def names(self, profile: str) -> set[str]:
        server = build_server(profile, self._services)
        return {tool.name for tool in await server.list_tools()}


@pytest.fixture
def mcp_profiles(m1a_repo: ApplicationServices) -> _McpProfiles:
    return _McpProfiles(m1a_repo)


def _tool_error_payload(excinfo: pytest.ExceptionInfo[ToolError]) -> dict:
    message = str(excinfo.value)
    return json.loads(message[message.index("{") :])


@pytest.mark.anyio
async def test_m1a_read_tools_exist_in_both_profiles(mcp_profiles) -> None:
    assert M1A_READ_TOOLS <= await mcp_profiles.names("main")
    assert M1A_READ_TOOLS <= await mcp_profiles.names("analyzer")
    assert "sync_repository_facts" not in await mcp_profiles.names("analyzer")
    assert "sync_repository_facts" in await mcp_profiles.names("main")


@pytest.mark.anyio
async def test_analyzer_cannot_call_sync_tool(m1a_repo) -> None:
    analyzer = build_server("analyzer", m1a_repo)
    with pytest.raises(ToolError, match="Unknown tool"):
        await analyzer.call_tool("sync_repository_facts", {"mode": "auto"})


@pytest.mark.anyio
async def test_inspect_node_tool_uses_m1a_query_projection(m1a_repo) -> None:
    inspection = NodeInspection(
        coordinate=CacheCoordinate("sha256:" + "a" * 64, 1),
        node={"id": "behavior.answer-question", "kind": "behavior"},
        relations=(),
        flow=None,
        mappings=(
            ResolvedMapping(
                mapping={"id": "map_01", "entity_uid": "ent_01"},
                resolution_status="resolved",
                current_location=CurrentSourceLocation(
                    relative_path="pkg/new.py",
                    address="pkg.new:answer",
                    start_line=10,
                    end_line=12,
                    signature="() -> str",
                ),
                last_known_location=CurrentSourceLocation(
                    relative_path="pkg/old.py",
                    address="pkg.old:answer",
                    start_line=None,
                    end_line=None,
                    signature="() -> str",
                ),
            ),
        ),
        evidence=(),
    )
    overview = SimpleNamespace(cognition_initialized=True)
    with (
        patch.object(m1a_repo, "repository_overview", return_value=overview),
        patch.object(m1a_repo, "inspect_node", return_value=inspection),
    ):
        server = build_server("analyzer", m1a_repo)
        result = await server.call_tool(
            "inspect_node", {"node_id": "behavior.answer-question"}
        )

    assert result.is_error is False
    payload = result.structured_content
    assert payload["graph_revision"] == 1
    assert payload["repository_source_digest"].startswith("sha256:")
    assert payload["node"]["id"] == "behavior.answer-question"
    assert "mappings" in payload
    assert "flow" in payload
    assert "evidence" in payload
    assert payload["mappings"][0]["current_location"]["relative_path"] == "pkg/new.py"
    assert payload["mappings"][0]["last_known_location"]["relative_path"] == "pkg/old.py"


@pytest.mark.anyio
async def test_repository_facts_tool_pages_entities(m1a_repo) -> None:
    server = build_server("analyzer", m1a_repo)

    first = await server.call_tool(
        "repository_facts", {"scope": "pkg.core", "limit": 2}
    )

    assert first.is_error is False
    payload = first.structured_content
    assert payload["schema_version"] == 1
    assert payload["graph_revision"] == 1
    assert payload["repository_source_digest"].startswith("sha256:")
    assert len(payload["entities"]) == 2
    assert payload["truncated"] is True
    assert payload["cursor"] is not None

    second = await server.call_tool(
        "repository_facts",
        {"scope": "pkg.core", "limit": 2, "cursor": payload["cursor"]},
    )

    assert second.is_error is False
    first_names = {entity["name"] for entity in payload["entities"]}
    second_names = {entity["name"] for entity in second.structured_content["entities"]}
    assert first_names.isdisjoint(second_names)
    assert first_names | second_names == {"greet", "Greeter", "pkg.core", "run"}
    assert second.structured_content["truncated"] is False
    assert second.structured_content["cursor"] is None


@pytest.mark.anyio
async def test_analysis_scope_tool_reports_partitions(m1a_repo) -> None:
    server = build_server("analyzer", m1a_repo)

    result = await server.call_tool("analysis_scope", {})

    assert result.is_error is False
    payload = result.structured_content
    assert payload["schema_version"] == 1
    modules = {partition["module_name"] for partition in payload["partitions"]}
    assert {"pkg", "pkg.core", "pkg.user"} <= modules
    assert payload["totals"]["file_count"] == 3
    assert payload["totals"]["entity_count"] >= 4
    assert payload["node_kind_counts"] == {
        "behavior": 1,
        "capability": 2,
        "responsibility": 1,
    }
    assert payload["truncated"] is False
    assert "cursor" in payload
    assert "diagnostics" in payload


@pytest.mark.anyio
async def test_search_cognitive_graph_tool_returns_bounded_hits(m1a_repo) -> None:
    server = build_server("main", m1a_repo)

    result = await server.call_tool(
        "search_cognitive_graph",
        {"query": "resume", "kinds": ["behavior"], "limit": 10},
    )

    assert result.is_error is False
    payload = result.structured_content
    assert [hit["node_id"] for hit in payload["hits"]] == [
        "behavior.checkpoint-resume"
    ]
    assert payload["truncated"] is False
    assert payload["graph_revision"] == 1


@pytest.mark.anyio
async def test_discussion_context_tool_bounds_and_truncates(m1a_repo) -> None:
    server = build_server("analyzer", m1a_repo)

    result = await server.call_tool(
        "get_discussion_context",
        {
            "node_ids": ["behavior.checkpoint-resume"],
            "depth": 2,
            "max_nodes": 3,
            "max_entities": 4,
            "max_evidence": 2,
        },
    )

    assert result.is_error is False
    payload = result.structured_content
    assert payload["schema_version"] == 1
    assert len(payload["nodes"]) <= 3
    assert payload["truncated"] is True
    assert "cursor" in payload
    assert payload["graph_revision"] == 1

    full = await server.call_tool(
        "get_discussion_context",
        {
            "node_ids": ["behavior.checkpoint-resume"],
            "depth": 1,
            "max_nodes": 40,
            "max_entities": 80,
            "max_evidence": 80,
        },
    )

    assert full.is_error is False
    assert full.structured_content["truncated"] is False
    assert full.structured_content["flows"]
    assert full.structured_content["mappings"]
    assert full.structured_content["evidence"]


@pytest.mark.anyio
async def test_resolve_entity_context_tool_by_path_and_double_anchor(
    m1a_repo,
) -> None:
    server = build_server("main", m1a_repo)

    resolved = await server.call_tool(
        "resolve_entity_context", {"path": "pkg/user.py", "limit": 10}
    )

    assert resolved.is_error is False
    payload = resolved.structured_content
    assert payload["anchor_kind"] == "path"
    assert any(entity["name"] == "use" for entity in payload["entities"])
    assert payload["relations"]
    assert payload["schema_version"] == 1

    with pytest.raises(ToolError) as double_anchor:
        await server.call_tool(
            "resolve_entity_context",
            {"entity_uid": "ent_x", "path": "pkg/user.py"},
        )

    payload = _tool_error_payload(double_anchor)
    assert payload["schema_version"] == 1
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert payload["error"]["retryable"] is False


@pytest.mark.anyio
async def test_resolve_entity_context_tool_by_uid_with_mappings(m1a_repo) -> None:
    server = build_server("analyzer", m1a_repo)

    result = await server.call_tool(
        "resolve_entity_context",
        {"entity_uid": "ent_01J0000000000000000000000A"},
    )

    assert result.is_error is False
    payload = result.structured_content
    assert payload["anchor_kind"] == "entity_uid"
    assert payload["entities"] == []
    assert [mapping["subject_id"] for mapping in payload["mappings"]] == [
        "behavior.checkpoint-resume"
    ]
    assert payload["entity_refs"][0]["relative_path"] == (
        "src/codecortex/infrastructure/formal.py"
    )


@pytest.mark.anyio
async def test_read_tools_fail_closed_when_caches_lag_formal_revision(
    m1a_repo,
) -> None:
    _apply_node(m1a_repo, "behavior.second-revision")
    server = build_server("analyzer", m1a_repo)

    with pytest.raises(ToolError) as search_error:
        await server.call_tool(
            "search_cognitive_graph", {"query": "resume", "limit": 10}
        )
    with pytest.raises(ToolError) as facts_error:
        await server.call_tool("repository_facts", {"scope": "pkg.core"})

    for excinfo in (search_error, facts_error):
        payload = _tool_error_payload(excinfo)
        assert payload["schema_version"] == 1
        assert payload["error"]["code"] == "CACHE_REBUILD_REQUIRED"
        assert payload["error"]["retryable"] is True


@pytest.mark.anyio
async def test_caller_revision_pin_detects_graph_drift(m1a_repo) -> None:
    server = build_server("main", m1a_repo)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool(
            "get_discussion_context",
            {
                "node_ids": ["behavior.checkpoint-resume"],
                "expected_graph_revision": 9,
            },
        )

    assert _tool_error_payload(excinfo)["error"]["code"] == "CACHE_REBUILD_REQUIRED"


@pytest.mark.anyio
async def test_sync_repository_facts_main_only(m1a_repo) -> None:
    server = build_server("main", m1a_repo)

    result = await server.call_tool("sync_repository_facts", {"mode": "auto"})

    assert result.is_error is False
    payload = result.structured_content
    assert payload["schema_version"] == 1
    assert payload["graph_revision"] == 1
    assert payload["rebuilt"] is False
    assert payload["parsed_files"] == 0


@pytest.fixture
def zero_cap_repo(tmp_path: Path) -> ApplicationServices:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    app = _compose(root, max_graph_objects=0)
    app.initialize_repository()
    _apply_node(app, "behavior.answer-question")
    return app


@pytest.mark.anyio
async def test_cognitive_graph_compatibility_cap(
    zero_cap_repo: ApplicationServices,
) -> None:
    server = build_server("analyzer", zero_cap_repo)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("cognitive_graph", {})

    payload = _tool_error_payload(excinfo)
    assert payload["schema_version"] == 1
    assert payload["error"]["code"] == "CONTEXT_LIMIT_EXCEEDED"
    assert "search_cognitive_graph" in payload["error"]["message"]
    assert "get_discussion_context" in payload["error"]["message"]
