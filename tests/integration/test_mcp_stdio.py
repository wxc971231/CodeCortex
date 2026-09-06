"""Real STDIO protocol coverage for the CodeCortex MCP server."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from codecortex.application.services import ApplicationServices
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import find_repository
from codecortex.infrastructure.views import render_views


def _initialize_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    repository = find_repository(root)
    ApplicationServices(
        repository=repository,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(repository.root),
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
    ).initialize_repository()


@pytest.mark.anyio
async def test_stdio_server_handles_sequential_requests_and_logs_to_stderr(
    tmp_path: Path,
) -> None:
    """Protocol frames remain clean when the real server emits diagnostics."""
    _initialize_repository(tmp_path)
    source_root = Path(__file__).parents[2] / "src"
    environment = {**os.environ, "PYTHONPATH": str(source_root)}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codecortex", "mcp", "--profile", "analyzer"],
        cwd=tmp_path,
        env=environment,
    )
    stderr_path = tmp_path / "mcp-server.stderr"
    with stderr_path.open("w", encoding="utf-8") as server_stderr:
        async with stdio_client(parameters, errlog=server_stderr) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                overview = await session.call_tool("repository_overview", {})
                validation = await session.call_tool("validate_graph", {})
                graph = await session.call_tool("cognitive_graph", {})
                missing_node = await session.call_tool(
                    "inspect_node", {"node_id": "capability.missing"}
                )

    assert overview.is_error is False
    assert overview.structured_content["schema_version"] == 1
    for result in (validation, graph, missing_node):
        assert result.is_error is True
        error_text = result.content[0].text
        error_payload = json.loads(error_text[error_text.index("{") :])
        assert error_payload["schema_version"] == 1
        assert error_payload["error"]["code"] == "NOT_INITIALIZED"
    assert "CodeCortex MCP server started" in stderr_path.read_text(encoding="utf-8")
