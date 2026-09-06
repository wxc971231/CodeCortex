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
    *,
    allow_m1a_bootstrap: bool = False,
) -> QueryResult:
    """Run the profile-safe gate, including guarded M1a bootstrap fact reads.

    The M1a initialization sequence needs ``repository_facts`` and
    ``analysis_scope`` while formal cognition is still uninitialized. Those
    two tools may continue in either profile only under an atomic formal-state
    guard;
    their QueryService CacheGuard still requires a synchronized fact cache and
    revision-matched cognitive replica. Every initialized/M1b query retains
    the mandatory preflight path.
    """
    if preflight:
        try:
            services.run_preflight()
        except CodeCortexError as error:
            if not allow_m1a_bootstrap or error.code is not ErrorCode.NOT_INITIALIZED:
                raise
            return services.run_m1a_bootstrap_read(operation)
        return operation()
    return _analyzer_guarded_read(
        services,
        preflight,
        operation,
        allow_m1a_bootstrap=allow_m1a_bootstrap,
    )


def _analyzer_guarded_read[QueryResult](
    services: ApplicationServices,
    main_profile: bool,
    operation: Callable[[], QueryResult],
    *,
    allow_m1a_bootstrap: bool = False,
) -> QueryResult:
    """Apply Analyzer's atomic cognition/bootstrap read boundary."""
    return (
        operation()
        if main_profile
        else services.run_analyzer_query_read(
            operation, allow_m1a_bootstrap=allow_m1a_bootstrap
        )
    )


def _configured_query_value(
    services: ApplicationServices, attribute: str, fallback: int
) -> int:
    configured = getattr(services, attribute, fallback)
    if type(configured) is not int or configured < 1:
        return fallback
    return configured


def _configured_query_default(
    services: ApplicationServices, attribute: str, fallback: int
) -> int:
    return min(_configured_query_value(services, attribute, fallback), fallback)


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
    async def repository_overview() -> tools.RepositoryOverviewOutput:
        try:
            return tools.repository_overview(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="cognitive_graph")
    async def cognitive_graph() -> tools.CognitiveGraphOutput:
        try:
            return _main_query(
                services, preflight, lambda: tools.cognitive_graph(services)
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="inspect_node")
    async def inspect_node(node_id: str) -> tools.InspectNodeOutput:
        try:
            return _main_query(
                services, preflight, lambda: tools.inspect_node(services, node_id)
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="history_event")
    async def history_event(event_id: str) -> tools.HistoryEventOutput:
        try:
            return _analyzer_guarded_read(
                services, preflight, lambda: tools.history_event(services, event_id)
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="validate_graph")
    async def validate_graph() -> tools.ValidationOutput:
        try:
            return _analyzer_guarded_read(
                services, preflight, lambda: tools.validate_graph(services)
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="repository_facts")
    async def repository_facts(
        scope: str,
        cursor: str | None = None,
        limit: int | None = None,
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
                    _configured_query_default(
                        services, "query_max_entities", 50
                    )
                    if limit is None
                    else limit,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
                allow_m1a_bootstrap=True,
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="analysis_scope")
    async def analysis_scope(
        scope: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
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
                    _configured_query_default(
                        services, "query_max_entities", 50
                    )
                    if limit is None
                    else limit,
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
                allow_m1a_bootstrap=True,
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="resolve_entity_context")
    async def resolve_entity_context(
        entity_uid: str | None = None,
        path: str | None = None,
        address: str | None = None,
        relation_types: list[str] | None = None,
        cursor: str | None = None,
        limit: int | None = None,
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
                    limit=(
                        _configured_query_default(
                            services, "query_max_entities", 50
                        )
                        if limit is None
                        else limit
                    ),
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="get_discussion_context")
    async def get_discussion_context(
        node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        depth: int | None = None,
        max_nodes: int | None = None,
        max_entities: int | None = None,
        max_evidence: int | None = None,
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
                    depth=(
                        _configured_query_value(
                            services, "query_default_depth", 2
                        )
                        if depth is None
                        else depth
                    ),
                    max_nodes=(
                        _configured_query_default(
                            services, "query_max_nodes", 40
                        )
                        if max_nodes is None
                        else max_nodes
                    ),
                    max_entities=(
                        _configured_query_default(
                            services, "query_max_entities", 80
                        )
                        if max_entities is None
                        else max_entities
                    ),
                    max_evidence=(
                        _configured_query_default(
                            services, "query_max_evidence", 80
                        )
                        if max_evidence is None
                        else max_evidence
                    ),
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="search_cognitive_graph")
    async def search_cognitive_graph(
        query: str,
        kinds: list[str] | None = None,
        limit: int | None = None,
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
                    limit=(
                        _configured_query_default(
                            services, "query_max_nodes", 20
                        )
                        if limit is None
                        else limit
                    ),
                    expected_graph_revision=expected_graph_revision,
                    expected_source_digest=expected_source_digest,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="cognitive_freshness")
    async def cognitive_freshness() -> tools.CognitiveFreshnessOutput:
        try:
            return _analyzer_guarded_read(
                services,
                preflight,
                lambda: tools.cognitive_freshness(services, preflight=preflight),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="pending_changes")
    async def pending_changes(
        cursor: str | None = None,
        limit: int = 50,
    ) -> tools.PendingChangesOutput:
        try:
            return _analyzer_guarded_read(
                services,
                preflight,
                lambda: tools.pending_changes(
                    services, cursor=cursor, limit=limit, preflight=preflight
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="effective_query_freshness")
    async def effective_query_freshness(
        node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        max_unmapped_changes: int = 50,
    ) -> tools.EffectiveQueryFreshnessOutput:
        try:
            return _analyzer_guarded_read(
                services,
                preflight,
                lambda: tools.effective_query_freshness(
                    services,
                    node_ids=node_ids or (),
                    entity_ids=entity_ids or (),
                    max_unmapped_changes=max_unmapped_changes,
                    preflight=preflight,
                ),
            )
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except (TypeError, ValueError) as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error


def _register_main_tools(server: MCPServer, services: ApplicationServices) -> None:
    @server.tool(name="initialize_repository")
    async def initialize_repository() -> tools.RepositoryOverviewOutput:
        try:
            return tools.initialize_repository(services)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="sync_repository_facts")
    async def sync_repository_facts(
        mode: Literal["auto", "full"] = "auto",
    ) -> tools.SyncFactsOutput:
        try:
            return tools.sync_repository_facts(services, mode)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="create_cognitive_proposal")
    async def create_cognitive_proposal(
        proposal: tools.ProposalInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.create_cognitive_proposal(services, proposal)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="create_cognitive_proposal_from_analysis")
    async def create_cognitive_proposal_from_analysis(
        proposal: tools.AnalysisProposalInput,
    ) -> tools.ProposalOutput:
        try:
            return tools.create_cognitive_proposal_from_analysis(services, proposal)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="revise_cognitive_proposal")
    async def revise_cognitive_proposal(
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
    async def cognitive_proposal(proposal_id: str) -> tools.ProposalOutput:
        try:
            return tools.cognitive_proposal(services, proposal_id)
        except CodeCortexError as error:
            raise tools.as_tool_error(error) from error
        except ValueError as error:
            raise tools.as_tool_error(tools.invalid_argument(error)) from error

    @server.tool(name="apply_cognitive_proposal")
    async def apply_cognitive_proposal(
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
    async def advance_cognition_baseline(
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
