"""Static Main and Analyzer CodeCortex MCP servers over STDIO."""

import logging
import sys
from collections.abc import Callable
from typing import Literal, cast

from mcp.server import MCPServer

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.interfaces.mcp import tools

Profile = Literal["main", "analyzer"]
READ_TOOL_NAMES = frozenset(
    {
        "repository_overview",
        "cognitive_graph",
        "inspect_node",
        "history_event",
        "validate_graph",
    }
)
MAIN_ONLY_TOOL_NAMES = frozenset(
    {
        "initialize_repository",
        "create_cognitive_proposal",
        "revise_cognitive_proposal",
        "cognitive_proposal",
        "apply_cognitive_proposal",
    }
)

LOGGER = logging.getLogger(__name__)


def build_server(profile: Profile, services: ApplicationServices) -> MCPServer:
    """Build one fresh server with its static, profile-specific tool set."""
    if profile not in ("main", "analyzer"):
        raise ValueError(f"Unsupported MCP profile: {profile}")
    server = MCPServer("CodeCortex")
    _register_read_tools(server, services)
    if profile == "main":
        _register_main_tools(server, services)
    return server


def run_stdio(
    profile: str,
    services_factory: Callable[[], ApplicationServices],
) -> int:
    """Compose CWD-scoped services and serve one MCP STDIO session."""
    if profile not in ("main", "analyzer"):
        raise ValueError(f"Unsupported MCP profile: {profile}")
    checked_profile = cast(Profile, profile)
    _configure_stderr_logging()
    services = services_factory()
    _recover_main_formal_state(checked_profile, services)
    server = build_server(checked_profile, services)
    LOGGER.info("CodeCortex MCP server started with %s profile", checked_profile)
    server.run(transport="stdio")
    return 0


def _recover_main_formal_state(profile: Profile, services: ApplicationServices) -> None:
    """Recover once for Main without granting Analyzer a write path."""
    if profile != "main":
        return
    try:
        services.recover_formal_state()
    except CodeCortexError as error:
        # An uninitialized repository has no transaction to recover. Init remains
        # an ordinary Main MCP tool call; any other recovery failure is unsafe.
        if error.code is not ErrorCode.NOT_INITIALIZED:
            raise


def _register_read_tools(server: MCPServer, services: ApplicationServices) -> None:
    @server.tool(name="repository_overview")
    def repository_overview() -> tools.RepositoryOverviewOutput:
        try:
            return tools.repository_overview(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="cognitive_graph")
    def cognitive_graph() -> tools.CognitiveGraphOutput:
        try:
            return tools.cognitive_graph(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="inspect_node")
    def inspect_node(node_id: str) -> tools.InspectNodeOutput:
        try:
            return tools.inspect_node(services, node_id)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="history_event")
    def history_event(event_id: str) -> tools.HistoryEventOutput:
        try:
            return tools.history_event(services, event_id)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="validate_graph")
    def validate_graph() -> tools.ValidationOutput:
        try:
            return tools.validate_graph(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error


def _register_main_tools(server: MCPServer, services: ApplicationServices) -> None:
    @server.tool(name="initialize_repository")
    def initialize_repository() -> tools.RepositoryOverviewOutput:
        try:
            return tools.initialize_repository(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="create_cognitive_proposal")
    def create_cognitive_proposal(
        proposal: tools.ProposalInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.create_cognitive_proposal(services, proposal)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="revise_cognitive_proposal")
    def revise_cognitive_proposal(
        proposal_id: str,
        revision: tools.ProposalRevisionInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.revise_cognitive_proposal(services, proposal_id, revision)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="cognitive_proposal")
    def cognitive_proposal(proposal_id: str) -> tools.ProposalOutput:
        try:
            return tools.cognitive_proposal(services, proposal_id)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error

    @server.tool(name="apply_cognitive_proposal")
    def apply_cognitive_proposal(
        proposal_id: str,
        approval_record: tools.ApprovalRecordInput,
    ) -> tools.ApplyProposalOutput:
        try:
            return tools.apply_cognitive_proposal(services, proposal_id, approval_record)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error


def _configure_stderr_logging() -> None:
    """Keep diagnostics off STDIO protocol frames even in a fresh process."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
