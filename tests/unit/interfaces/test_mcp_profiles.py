"""MCP profile registration and adapter-boundary tests."""

import json
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codecortex.application.services import RepositoryOverview
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.interfaces.mcp.server import _recover_main_formal_state, build_server

ANALYZER_TOOLS = {
    "repository_overview",
    "cognitive_graph",
    "inspect_node",
    "history_event",
    "validate_graph",
    "repository_facts",
    "analysis_scope",
    "resolve_entity_context",
    "get_discussion_context",
    "search_cognitive_graph",
    "cognitive_freshness",
    "pending_changes",
    "effective_query_freshness",
}
MAIN_TOOLS = ANALYZER_TOOLS | {
    "initialize_repository",
    "sync_repository_facts",
    "create_cognitive_proposal",
    "create_cognitive_proposal_from_analysis",
    "revise_cognitive_proposal",
    "cognitive_proposal",
    "apply_cognitive_proposal",
}


@pytest.fixture
def services() -> MagicMock:
    """An application-service double: MCP tests never touch a user repository."""
    return MagicMock()


async def _listed_tool_names(server: object) -> set[str]:
    tools = await server.list_tools()  # type: ignore[attr-defined]
    return {tool.name for tool in tools}


@pytest.mark.anyio
async def test_profiles_register_exact_static_tool_allowlists(
    services: MagicMock,
) -> None:
    """Analyzer must not receive a write tool merely hidden at call time."""
    assert await _listed_tool_names(build_server("analyzer", services)) == ANALYZER_TOOLS
    assert await _listed_tool_names(build_server("main", services)) == MAIN_TOOLS


@pytest.mark.anyio
async def test_analyzer_write_tool_is_not_registered(services: MagicMock) -> None:
    """The SDK itself rejects writes for Analyzer because no handler exists."""
    server = build_server("analyzer", services)
    with pytest.raises(ToolError, match="Unknown tool"):
        await server.call_tool(
            "apply_cognitive_proposal",
            {"proposal_id": "prop_01ARZ3NDEKTSV4RRFFQ69G5FAV", "approval_record": {}},
        )
    with pytest.raises(ToolError, match="Unknown tool"):
        await server.call_tool(
            "create_cognitive_proposal_from_analysis",
            {"proposal": {"analysis_report": {}, "reason": "forbidden"}},
        )


@pytest.mark.anyio
async def test_read_tool_returns_versioned_dto(services: MagicMock) -> None:
    """Public MCP responses are DTOs rather than application dataclasses."""
    services.repository_overview.return_value = RepositoryOverview(
        repository_root="/repo",
        graph_revision=0,
        cognition_initialized=False,
        cognition_baseline_source_digest=None,
        formal_files={"manifest.json": True},
    )

    result = await build_server("analyzer", services).call_tool(
        "repository_overview", {}
    )

    assert result.structured_content == {
        "schema_version": 1,
        "repository_root": "/repo",
        "graph_revision": 0,
        "cognition_initialized": False,
        "cognition_baseline_source_digest": None,
        "formal_files": {"manifest.json": True},
    }


@pytest.mark.anyio
async def test_domain_error_is_a_stable_structured_tool_error(
    services: MagicMock,
) -> None:
    """Core errors retain their code and suggested action at the MCP boundary."""
    services.repository_overview.side_effect = CodeCortexError(
        ErrorCode.NOT_INITIALIZED,
        "CodeCortex formal state is not initialized",
        suggested_action="Run initialize_repository first",
    )

    with pytest.raises(ToolError) as excinfo:
        await build_server("analyzer", services).call_tool("repository_overview", {})

    message = str(excinfo.value)
    payload = json.loads(message[message.index("{") :])
    assert payload["schema_version"] == 1
    assert payload["error"]["code"] == "NOT_INITIALIZED"
    assert payload["error"]["suggested_action"] == "Run initialize_repository first"


def test_main_process_recovers_before_registering_tools(services: MagicMock) -> None:
    """A restarted Main server repairs a prior interrupted transaction once."""
    _recover_main_formal_state("main", services)

    services.recover_formal_state.assert_called_once_with()


def test_analyzer_process_never_attempts_recovery_writes(services: MagicMock) -> None:
    """Analyzer keeps its read-only contract even when Main can recover."""
    _recover_main_formal_state("analyzer", services)

    services.recover_formal_state.assert_not_called()


def test_main_process_allows_uninitialized_repository(services: MagicMock) -> None:
    """Main must still start so its initialize tool can create the skeleton."""
    services.recover_formal_state.side_effect = CodeCortexError(
        ErrorCode.NOT_INITIALIZED, "not initialized"
    )

    _recover_main_formal_state("main", services)

    services.recover_formal_state.assert_called_once_with()


def test_main_process_refuses_unsafe_recovery_failure(services: MagicMock) -> None:
    """A corrupt journal is never hidden behind an apparently healthy server."""
    services.recover_formal_state.side_effect = CodeCortexError(
        ErrorCode.FORMAL_STATE_CORRUPT, "unprovable transaction"
    )

    with pytest.raises(CodeCortexError, match="unprovable transaction"):
        _recover_main_formal_state("main", services)
