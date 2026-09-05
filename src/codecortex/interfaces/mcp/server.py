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
        "repository_facts",
        "analysis_scope",
        "resolve_entity_context",
        "get_discussion_context",
        "search_cognitive_graph",
        "cognitive_freshness",
        "pending_changes",
        "effective_query_freshness",
    }
)
MAIN_ONLY_TOOL_NAMES = frozenset(
    {
        "initialize_repository",
        "sync_repository_facts",
        "create_cognitive_proposal",
        "create_cognitive_proposal_from_analysis",
        "revise_cognitive_proposal",
        "cognitive_proposal",
        "apply_cognitive_proposal",
        "advance_cognition_baseline",
    }
)

LOGGER = logging.getLogger(__name__)


def _main_query[QueryResult](
    services: ApplicationServices,
    preflight: bool,
    operation: Callable[[], QueryResult],
) -> QueryResult:
    """Run the one Main fact gate before every M1a query entry."""
    if preflight:
        services.run_preflight()
    return operation()


def build_server(profile: Profile, services: ApplicationServices) -> MCPServer:
    """Build one fresh server with its static, profile-specific tool set."""
    if profile not in ("main", "analyzer"):
        raise ValueError(f"Unsupported MCP profile: {profile}")
    server = MCPServer("CodeCortex")
    _register_read_tools(server, services, preflight=(profile == "main"))
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


def _register_read_tools(
    server: MCPServer, services: ApplicationServices, *, preflight: bool
) -> None:
    @server.tool(name="repository_overview")
    def repository_overview() -> tools.RepositoryOverviewOutput:
        try:
            return tools.repository_overview(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="cognitive_graph")
    def cognitive_graph() -> tools.CognitiveGraphOutput:
        try:
            return _main_query(
                services, preflight, lambda: tools.cognitive_graph(services)
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="inspect_node")
    def inspect_node(node_id: str) -> tools.InspectNodeOutput:
        try:
            return _main_query(
                services, preflight, lambda: tools.inspect_node(services, node_id)
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="history_event")
    def history_event(event_id: str) -> tools.HistoryEventOutput:
        try:
            return tools.history_event(services, event_id)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="validate_graph")
    def validate_graph() -> tools.ValidationOutput:
        try:
            return tools.validate_graph(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="repository_facts")
    def repository_facts(
        scope: str,
        cursor: str | None = None,
        limit: int = 50,
        expected_graph_revision: int | None = None,
        expected_source_digest: str | None = None,
    ) -> tools.RepositoryFactsOutput:
        try:
            return _main_query(
                services,
                preflight,
                lambda: tools.repository_facts(
                    services,
                    scope,
                    cursor,
                    limit,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="analysis_scope")
    def analysis_scope(
        scope: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        expected_graph_revision: int | None = None,
        expected_source_digest: str | None = None,
    ) -> tools.AnalysisScopeOutput:
        try:
            return _main_query(
                services,
                preflight,
                lambda: tools.analysis_scope(
                    services,
                    scope,
                    cursor,
                    limit,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="resolve_entity_context")
    def resolve_entity_context(
        entity_uid: str | None = None,
        path: str | None = None,
        address: str | None = None,
        relation_types: list[str] | None = None,
        cursor: str | None = None,
        limit: int = 50,
        expected_graph_revision: int | None = None,
        expected_source_digest: str | None = None,
    ) -> tools.EntityContextOutput:
        try:
            return _main_query(
                services,
                preflight,
                lambda: tools.resolve_entity_context(
                    services,
                    entity_uid=entity_uid,
                    path=path,
                    address=address,
                    relation_types=relation_types or (),
                    cursor=cursor,
                    limit=limit,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="get_discussion_context")
    def get_discussion_context(
        node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        depth: int = 2,
        max_nodes: int = 40,
        max_entities: int = 80,
        max_evidence: int = 80,
        expected_graph_revision: int | None = None,
        expected_source_digest: str | None = None,
    ) -> tools.DiscussionContextOutput:
        try:
            return _main_query(
                services,
                preflight,
                lambda: tools.get_discussion_context(
                    services,
                    node_ids=node_ids or (),
                    entity_ids=entity_ids or (),
                    depth=depth,
                    max_nodes=max_nodes,
                    max_entities=max_entities,
                    max_evidence=max_evidence,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="search_cognitive_graph")
    def search_cognitive_graph(
        query: str,
        kinds: list[str] | None = None,
        limit: int = 20,
        expected_graph_revision: int | None = None,
        expected_source_digest: str | None = None,
    ) -> tools.SearchGraphOutput:
        try:
            return _main_query(
                services,
                preflight,
                lambda: tools.search_cognitive_graph(
                    services,
                    query,
                    kinds=kinds or (),
                    limit=limit,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="cognitive_freshness")
    def cognitive_freshness() -> tools.CognitiveFreshnessOutput:
        try:
            return tools.cognitive_freshness(services, preflight=preflight)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="pending_changes")
    def pending_changes(
        cursor: str | None = None,
        limit: int = 50,
    ) -> tools.PendingChangesOutput:
        try:
            return tools.pending_changes(
                services, cursor=cursor, limit=limit, preflight=preflight
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="effective_query_freshness")
    def effective_query_freshness(
        node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        max_unmapped_changes: int = 50,
    ) -> tools.EffectiveQueryFreshnessOutput:
        try:
            return tools.effective_query_freshness(
                services,
                node_ids=node_ids or (),
                entity_ids=entity_ids or (),
                max_unmapped_changes=max_unmapped_changes,
                preflight=preflight,
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error


def _register_main_tools(server: MCPServer, services: ApplicationServices) -> None:
    @server.tool(name="initialize_repository")
    def initialize_repository() -> tools.RepositoryOverviewOutput:
        try:
            return tools.initialize_repository(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="sync_repository_facts")
    def sync_repository_facts(
        mode: Literal["auto", "full"] = "auto",
    ) -> tools.SyncFactsOutput:
        try:
            return tools.sync_repository_facts(services, mode)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="create_cognitive_proposal")
    def create_cognitive_proposal(
        proposal: tools.ProposalInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.create_cognitive_proposal(services, proposal)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="create_cognitive_proposal_from_analysis")
    def create_cognitive_proposal_from_analysis(
        proposal: tools.AnalysisProposalInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.create_cognitive_proposal_from_analysis(services, proposal)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="revise_cognitive_proposal")
    def revise_cognitive_proposal(
        proposal_id: str,
        revision: tools.ProposalRevisionInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.revise_cognitive_proposal(services, proposal_id, revision)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="cognitive_proposal")
    def cognitive_proposal(proposal_id: str) -> tools.ProposalOutput:
        try:
            return tools.cognitive_proposal(services, proposal_id)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="apply_cognitive_proposal")
    def apply_cognitive_proposal(
        proposal_id: str,
        approval_record: tools.ApprovalRecordInput,
    ) -> tools.ApplyProposalOutput:
        try:
            return tools.apply_cognitive_proposal(services, proposal_id, approval_record)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="advance_cognition_baseline")
    def advance_cognition_baseline(
        change_set_id: str,
        reason: Literal["no_semantic_change", "user_accepted"],
        decision_record: tools.DecisionRecordInput,
        approval_record: tools.BaselineApprovalRecordInput | None = None,
    ) -> tools.BaselineAdvanceOutput:
        try:
            return tools.advance_cognition_baseline(
                services, change_set_id, reason, decision_record, approval_record
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error


def _configure_stderr_logging() -> None:
    """Keep diagnostics off STDIO protocol frames even in a fresh process."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
