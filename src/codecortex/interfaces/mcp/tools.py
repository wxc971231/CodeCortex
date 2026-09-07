"""Versioned DTO adapters for the static CodeCortex MCP tool sets."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from codecortex.application.baseline import BaselineApprovalRecord, DecisionRecord
from codecortex.application.freshness import FreshnessService
from codecortex.application.services import ApplicationServices, RepositoryOverview
from codecortex.domain.cognition import CognitiveGraph, ValidationResult
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import (
    ApprovalRecord,
    PatchOperation,
    PatchOperationKind,
    Proposal,
    json_value_to_mutable,
)
from codecortex.infrastructure.persistence.graph_replica import ContextRequest

SCHEMA_VERSION = 1


class _Dto(BaseModel):
    """Base protocol model: explicit schema version and no silent extra fields."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = Field(default=1, frozen=True)


class PatchOperationInput(_Dto):
    """Wire representation of one M0 graph patch operation."""

    kind: PatchOperationKind
    target_id: str = Field(min_length=1, max_length=256)
    value: dict[str, Any] | None = None
    expected_revision: int | Literal["absent"] | None = None

    def to_domain(self) -> PatchOperation:
        return PatchOperation(
            self.kind,
            self.target_id,
            self.value,
            expected_revision=self.expected_revision,
        )


class ProposalInput(_Dto):
    """Bounded wire input for a new M0 cognitive proposal."""

    operations: list[PatchOperationInput] = Field(min_length=1, max_length=100)
    affected_nodes: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=4_000)
    analyzed_source_digest: str | None = None
    source_preconditions: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    uncertainties: list[Any] = Field(default_factory=list, max_length=100)


class AnalysisProposalInput(_Dto):
    """One complete Analyzer report handed to Core without a loose patch path."""

    analysis_report: dict[str, Any]
    reason: str = Field(min_length=1, max_length=4_000)


class ProposalRevisionInput(_Dto):
    """Replacement current candidate for a previously discussed proposal."""

    operations: list[PatchOperationInput] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4_000)
    analyzed_source_digest: str | None = None
    source_preconditions: list[dict[str, Any]] | None = Field(default=None, max_length=500)
    affected_nodes: list[str] | None = Field(default=None, max_length=100)
    evidence: list[dict[str, Any]] | None = Field(default=None, max_length=500)
    uncertainties: list[Any] | None = Field(default=None, max_length=100)


class ApprovalRecordInput(_Dto):
    """The structured approval Main Codex derives from explicit user consent."""

    proposal_id: str = Field(min_length=1, max_length=256)
    patch_digest: str = Field(min_length=1, max_length=80)
    approved_by: str = Field(min_length=1, max_length=64)
    approved_at: str = Field(min_length=1, max_length=64)
    approval_summary: str = Field(min_length=1, max_length=4_000)

    def to_domain(self) -> ApprovalRecord:
        return ApprovalRecord(
            proposal_id=self.proposal_id,
            patch_digest=self.patch_digest,
            approved_by=self.approved_by,
            approved_at=self.approved_at,
            approval_summary=self.approval_summary,
        )


class RepositoryOverviewOutput(_Dto):
    repository_root: str
    graph_revision: int
    cognition_initialized: bool
    cognition_baseline_source_digest: str | None
    formal_files: dict[str, bool]


class CognitiveGraphOutput(_Dto):
    graph_revision: int
    nodes: list[dict[str, Any]]
    semantic_edges: list[dict[str, Any]]
    logical_flows: list[dict[str, Any]]
    implementation_mappings: list[dict[str, Any]]


class InspectNodeOutput(_Dto):
    node: dict[str, Any]
    relations: list[dict[str, Any]]
    approval_event_id: str | None
    graph_revision: int | None = None
    repository_source_digest: str | None = None
    flow: dict[str, Any] | None = None
    mappings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)
    continuation_hints: list[str] = Field(default_factory=list)


class HistoryEventOutput(_Dto):
    event: dict[str, Any]


class ValidationOutput(_Dto):
    valid: bool
    issues: list[dict[str, str]]


class ProposalOutput(_Dto):
    proposal: dict[str, Any]


class ApplyProposalOutput(_Dto):
    event_id: str
    graph_revision: int
    applied_proposal_id: str
    cache_warnings: list[str] = Field(default_factory=list)


class DecisionRecordInput(_Dto):
    decided_by: Literal["analyzer", "main_codex"]
    evidence_summary: str = Field(min_length=1, max_length=4_000)
    decided_at: str = Field(min_length=1, max_length=64)

    def to_domain(self) -> DecisionRecord:
        return DecisionRecord(self.decided_by, self.evidence_summary, self.decided_at)


class BaselineApprovalRecordInput(_Dto):
    change_set_id: str = Field(min_length=1, max_length=64)
    source_digest: str = Field(min_length=1, max_length=80)
    approved_by: Literal["user"]
    approved_at: str = Field(min_length=1, max_length=64)
    approval_summary: str = Field(min_length=1, max_length=4_000)

    def to_domain(self) -> BaselineApprovalRecord:
        return BaselineApprovalRecord(
            self.change_set_id, self.source_digest, self.approved_by,
            self.approved_at, self.approval_summary,
        )


class BaselineAdvanceOutput(_Dto):
    event_id: str
    graph_revision: int
    previous_source_digest: str
    current_source_digest: str
    cache_warnings: list[str] = Field(default_factory=list)


def repository_overview(services: ApplicationServices) -> RepositoryOverviewOutput:
    """Return a bounded summary without exposing internal service dataclasses."""
    overview: RepositoryOverview = services.repository_overview()
    return RepositoryOverviewOutput(
        repository_root=overview.repository_root,
        graph_revision=overview.graph_revision,
        cognition_initialized=overview.cognition_initialized,
        cognition_baseline_source_digest=overview.cognition_baseline_source_digest,
        formal_files=overview.formal_files,
    )


def cognitive_graph(services: ApplicationServices) -> CognitiveGraphOutput:
    """Return the canonical M0 graph as JSON-compatible DTO data."""
    return _graph_output(services.cognitive_graph())


def inspect_node(services: ApplicationServices, node_id: str) -> InspectNodeOutput:
    """Return M1a source-resolved inspection, retaining M0 compatibility."""
    if (
        services.query_service is not None
        and services.repository_overview().cognition_initialized
    ):
        inspection = services.inspect_node(node_id)
        approval_event_id = _approval_event_id(inspection.node)
        return InspectNodeOutput(
            graph_revision=inspection.coordinate.graph_revision,
            repository_source_digest=inspection.coordinate.repository_source_digest,
            node=dict(inspection.node),
            relations=[dict(item) for item in inspection.relations],
            approval_event_id=approval_event_id,
            flow=None if inspection.flow is None else dict(inspection.flow),
            mappings=[
                {
                    **dict(mapping.mapping),
                    "resolution_status": mapping.resolution_status,
                    "current_location": (
                        None
                        if mapping.current_location is None
                        else asdict(mapping.current_location)
                    ),
                    "last_known_location": asdict(mapping.last_known_location),
                }
                for mapping in inspection.mappings
            ],
            evidence=[dict(item) for item in inspection.evidence],
            truncated=inspection.truncated,
            truncation_reasons=list(inspection.truncation_reasons),
            continuation_hints=list(inspection.continuation_hints),
        )
    graph = services.cognitive_graph()
    node = next((item for item in graph.nodes if item.get("id") == node_id), None)
    if node is None:
        raise CodeCortexError(
            ErrorCode.ANALYSIS_REPORT_INVALID,
            f"Cognitive graph node does not exist: {node_id}",
            suggested_action="Use a node ID returned by cognitive_graph",
        )
    relations = [
        dict(edge)
        for edge in graph.semantic_edges
        if node_id in _edge_endpoints(edge)
    ]
    approval_event_id = _approval_event_id(node)
    return InspectNodeOutput(
        node=dict(node), relations=relations, approval_event_id=approval_event_id
    )


def _approval_event_id(node: Mapping[str, object]) -> str | None:
    approval = node.get("approval")
    return (
        approval.get("approval_event_id")
        if isinstance(approval, Mapping)
        and isinstance(approval.get("approval_event_id"), str)
        else None
    )


def history_event(services: ApplicationServices, event_id: str) -> HistoryEventOutput:
    """Return one validated immutable History event."""
    return HistoryEventOutput(event=services.history_event(event_id))


def validate_graph(services: ApplicationServices) -> ValidationOutput:
    """Expose the normal Core validation result as a versioned DTO."""
    result: ValidationResult = services.validate_graph()
    return ValidationOutput(
        valid=result.valid,
        issues=[
            {
                "code": issue.code.value,
                "location": issue.location,
                "message": issue.message,
            }
            for issue in result.issues
        ],
    )


def initialize_repository(services: ApplicationServices) -> RepositoryOverviewOutput:
    """Idempotently create the M0 revision-zero formal state."""
    overview = services.initialize_repository()
    return RepositoryOverviewOutput(
        repository_root=overview.repository_root,
        graph_revision=overview.graph_revision,
        cognition_initialized=overview.cognition_initialized,
        cognition_baseline_source_digest=overview.cognition_baseline_source_digest,
        formal_files=overview.formal_files,
    )


def create_cognitive_proposal(
    services: ApplicationServices, proposal: ProposalInput
) -> ProposalOutput:
    """Validate and persist one pending proposal through the application service."""
    created = services.create_cognitive_proposal(
        operations=tuple(operation.to_domain() for operation in proposal.operations),
        affected_nodes=tuple(proposal.affected_nodes),
        reason=proposal.reason,
        analyzed_source_digest=proposal.analyzed_source_digest,
        source_preconditions=tuple(dict(item) for item in proposal.source_preconditions),
        evidence=tuple(dict(item) for item in proposal.evidence),
        uncertainties=tuple(proposal.uncertainties),
    )
    return ProposalOutput(proposal=_proposal_payload(created))


def create_cognitive_proposal_from_analysis(
    services: ApplicationServices, proposal: AnalysisProposalInput
) -> ProposalOutput:
    """Strictly validate one Analyzer report and create one aggregate Proposal."""
    payload = json.dumps(
        proposal.analysis_report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    created = services.create_cognitive_proposal_from_analysis(
        payload, proposal.reason
    )
    return ProposalOutput(proposal=_proposal_payload(created))


def revise_cognitive_proposal(
    services: ApplicationServices,
    proposal_id: str,
    revision: ProposalRevisionInput,
) -> ProposalOutput:
    """Replace the current candidate while preserving its immutable revision log."""
    revised = services.revise_cognitive_proposal(
        proposal_id,
        operations=tuple(operation.to_domain() for operation in revision.operations),
        reason=revision.reason,
        analyzed_source_digest=revision.analyzed_source_digest,
        source_preconditions=(
            None
            if revision.source_preconditions is None
            else tuple(dict(item) for item in revision.source_preconditions)
        ),
        affected_nodes=(
            None if revision.affected_nodes is None else tuple(revision.affected_nodes)
        ),
        evidence=(
            None if revision.evidence is None else tuple(dict(item) for item in revision.evidence)
        ),
        uncertainties=(
            None if revision.uncertainties is None else tuple(revision.uncertainties)
        ),
    )
    return ProposalOutput(proposal=_proposal_payload(revised))


def cognitive_proposal(services: ApplicationServices, proposal_id: str) -> ProposalOutput:
    """Return the bounded current snapshot of a pending proposal."""
    return ProposalOutput(proposal=_proposal_payload(services.cognitive_proposal(proposal_id)))


def apply_cognitive_proposal(
    services: ApplicationServices,
    proposal_id: str,
    approval_record: ApprovalRecordInput,
) -> ApplyProposalOutput:
    """Atomically apply the explicitly approved current proposal snapshot."""
    result = services.apply_cognitive_proposal(proposal_id, approval_record.to_domain())
    return ApplyProposalOutput(
        event_id=result.event_id,
        graph_revision=result.graph_revision,
        applied_proposal_id=result.applied_proposal_id,
        cache_warnings=list(result.cache_warnings),
    )


def advance_cognition_baseline(
    services: ApplicationServices,
    change_set_id: str,
    reason: Literal["no_semantic_change", "user_accepted"],
    decision_record: DecisionRecordInput,
    approval_record: BaselineApprovalRecordInput | None = None,
) -> BaselineAdvanceOutput:
    result = services.advance_cognition_baseline(
        change_set_id,
        reason,
        decision_record.to_domain(),
        None if approval_record is None else approval_record.to_domain(),
    )
    return BaselineAdvanceOutput(
        event_id=result.event_id,
        graph_revision=result.graph_revision,
        previous_source_digest=result.previous_source_digest,
        current_source_digest=result.current_source_digest,
        cache_warnings=list(result.cache_warnings),
    )


class RepositoryFactsOutput(_Dto):
    """One guarded, cursor-paginated page of module entities."""

    repository_source_digest: str
    graph_revision: int
    entities: list[dict[str, Any]]
    cursor: str | None
    truncated: bool


class AnalysisScopeOutput(_Dto):
    """Guarded package/module partitions with totals and diagnostics."""

    repository_source_digest: str
    graph_revision: int
    scope: str | None
    totals: dict[str, int]
    node_kind_counts: dict[str, int]
    partitions: list[dict[str, Any]]
    cursor: str | None
    truncated: bool
    diagnostics: list[dict[str, Any]]
    diagnostics_truncated: bool


class EntityContextOutput(_Dto):
    """Guarded factual and cognitive context around one entity anchor."""

    repository_source_digest: str
    graph_revision: int
    anchor_kind: str
    anchor_value: str
    entities: list[dict[str, Any]]
    cursor: str | None
    truncated: bool
    entity_refs: list[dict[str, Any]]
    mappings: list[dict[str, Any]]
    mappings_truncated: bool
    relations: list[dict[str, Any]]
    relations_truncated: bool


class DiscussionContextOutput(_Dto):
    """One guarded, bounded discussion-context neighborhood."""

    graph_revision: int
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    flows: list[dict[str, Any]]
    mappings: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    truncated: bool
    cursor: str | None
    truncation_reasons: list[str] = Field(default_factory=list)
    continuation_hints: list[str] = Field(default_factory=list)


class SearchGraphOutput(_Dto):
    """One guarded page of weighted cognitive search hits."""

    repository_source_digest: str
    graph_revision: int
    hits: list[dict[str, Any]]
    cursor: str | None
    truncated: bool


class SyncFactsOutput(_Dto):
    """The committed cache-generation outcome of one Fact Sync run."""

    repository_source_digest: str
    graph_revision: int
    index_generation: int
    parsed_files: int
    added_files: int
    changed_files: int
    deleted_files: int
    retry_count: int
    rebuilt: bool
    diagnostics: list[dict[str, Any]]


class CognitiveFreshnessOutput(_Dto):
    """Bounded repository-level cognition freshness and ChangeSet summary."""

    repository_status: Literal["fresh", "pending", "unresolved"]
    baseline_source_digest: str
    current_source_digest: str
    change_set_id: str | None
    file_diff_completeness: Literal["complete", "partial"] | None
    entity_diff_completeness: Literal["complete", "partial"] | None
    scope_confidence: Literal["complete", "partial", "unknown"] | None
    affected_nodes: list[str]
    affected_flows: list[str]
    affected_entities: list[str]
    diagnostics: list[str]
    reason_codes: list[str]
    truncated: bool


class PendingChangesOutput(_Dto):
    """One bounded page over the single effective baseline-to-current ChangeSet."""

    repository_status: Literal["fresh", "pending", "unresolved"]
    baseline_source_digest: str
    current_source_digest: str
    change_set_id: str | None
    file_diff_completeness: Literal["complete", "partial"] | None
    entity_diff_completeness: Literal["complete", "partial"] | None
    scope_confidence: Literal["complete", "partial", "unknown"] | None
    changed_files: list[dict[str, Any]]
    changed_entities: list[dict[str, Any]]
    affected_nodes: list[str]
    affected_flows: list[str]
    affected_entities: list[str]
    unmapped_changes: list[dict[str, Any]]
    diagnostics: list[str]
    reason_codes: list[str]
    cursor: str | None
    truncated: bool


class EffectiveQueryFreshnessOutput(_Dto):
    """Bounded, auditable route decision for a supplied graph/entity scope."""

    status: Literal[
        "current",
        "unaffected_current",
        "affected_source_first",
        "unknown_source_first",
    ]
    repository_status: Literal["fresh", "pending", "unresolved"]
    baseline_source_digest: str
    current_source_digest: str
    change_set_id: str | None
    scope_confidence: Literal["complete", "partial", "unknown"] | None
    matched_affected_nodes: list[str]
    matched_affected_entities: list[str]
    unmapped_changes: list[dict[str, Any]]
    unmapped_changes_truncated: bool
    reason_codes: list[str]


def repository_facts(
    services: ApplicationServices,
    scope: str,
    cursor: str | None = None,
    limit: int = 50,
    expected_graph_revision: int | None = None,
    expected_source_digest: str | None = None,
) -> RepositoryFactsOutput:
    """Return one guarded page of module entities with cursor metadata."""
    page = services.repository_facts(
        scope,
        cursor,
        limit,
        expected_source_digest=expected_source_digest,
        expected_graph_revision=expected_graph_revision,
    )
    return RepositoryFactsOutput(
        repository_source_digest=page.coordinate.repository_source_digest,
        graph_revision=page.coordinate.graph_revision,
        entities=[asdict(entity) for entity in page.entities],
        cursor=page.cursor,
        truncated=page.truncated,
    )


def analysis_scope(
    services: ApplicationServices,
    scope: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected_graph_revision: int | None = None,
    expected_source_digest: str | None = None,
) -> AnalysisScopeOutput:
    """Return guarded partitions, totals, and diagnostics for delegation."""
    result = services.analysis_scope(
        scope,
        cursor,
        limit,
        diagnostics_limit=min(limit, services.query_max_evidence),
        expected_source_digest=expected_source_digest,
        expected_graph_revision=expected_graph_revision,
    )
    return AnalysisScopeOutput(
        repository_source_digest=result.coordinate.repository_source_digest,
        graph_revision=result.coordinate.graph_revision,
        scope=result.scope,
        totals=asdict(result.totals),
        node_kind_counts=dict(result.node_kind_counts),
        partitions=[asdict(partition) for partition in result.partitions],
        cursor=result.cursor,
        truncated=result.truncated,
        diagnostics=[asdict(diagnostic) for diagnostic in result.diagnostics],
        diagnostics_truncated=result.diagnostics_truncated,
    )


def resolve_entity_context(
    services: ApplicationServices,
    entity_uid: str | None = None,
    path: str | None = None,
    address: str | None = None,
    relation_types: Sequence[str] = (),
    cursor: str | None = None,
    limit: int = 50,
    expected_graph_revision: int | None = None,
    expected_source_digest: str | None = None,
) -> EntityContextOutput:
    """Resolve exactly one anchor to entities, mappings, and relations."""
    result = services.resolve_entity_context(
        entity_uid=entity_uid,
        path=path,
        address=address,
        relation_types=tuple(relation_types),
        cursor=cursor,
        limit=limit,
        expected_source_digest=expected_source_digest,
        expected_graph_revision=expected_graph_revision,
    )
    return EntityContextOutput(
        repository_source_digest=result.coordinate.repository_source_digest,
        graph_revision=result.coordinate.graph_revision,
        anchor_kind=result.anchor_kind,
        anchor_value=result.anchor_value,
        entities=[asdict(entity) for entity in result.entities],
        cursor=result.cursor,
        truncated=result.truncated,
        entity_refs=[asdict(ref) for ref in result.entity_refs],
        mappings=[asdict(mapping) for mapping in result.mappings],
        mappings_truncated=result.mappings_truncated,
        relations=[asdict(relation) for relation in result.relations],
        relations_truncated=result.relations_truncated,
    )


def get_discussion_context(
    services: ApplicationServices,
    node_ids: Sequence[str] = (),
    entity_ids: Sequence[str] = (),
    depth: int = 2,
    max_nodes: int = 40,
    max_entities: int = 80,
    max_evidence: int = 80,
    expected_graph_revision: int | None = None,
    expected_source_digest: str | None = None,
    include_flows: bool = True,
) -> DiscussionContextOutput:
    """Return one guarded, bounded discussion-context neighborhood."""
    result = services.get_discussion_context(
        ContextRequest(
            node_ids=tuple(node_ids),
            entity_uids=tuple(entity_ids),
            depth=depth,
            max_nodes=max_nodes,
            max_entities=max_entities,
            max_evidence=max_evidence,
            expected_graph_revision=expected_graph_revision,
            expected_source_digest=expected_source_digest,
            include_flows=include_flows,
        )
    )
    return DiscussionContextOutput(
        graph_revision=result.graph_revision,
        nodes=[asdict(node) for node in result.nodes],
        edges=[asdict(edge) for edge in result.edges],
        flows=[asdict(flow) for flow in result.flows],
        mappings=[asdict(mapping) for mapping in result.mappings],
        entities=[asdict(entity) for entity in result.entities],
        evidence=[asdict(evidence) for evidence in result.evidence],
        truncated=result.truncated,
        cursor=result.continuation,
        truncation_reasons=list(result.truncation_reasons),
        continuation_hints=list(result.continuation_hints),
    )


def search_cognitive_graph(
    services: ApplicationServices,
    query: str,
    kinds: Sequence[str] = (),
    limit: int = 20,
    expected_graph_revision: int | None = None,
    expected_source_digest: str | None = None,
) -> SearchGraphOutput:
    """Return guarded, deterministic, bounded cognitive search hits."""
    page = services.search_cognitive_graph(
        query,
        tuple(kinds),
        limit,
        expected_source_digest=expected_source_digest,
        expected_graph_revision=expected_graph_revision,
    )
    return SearchGraphOutput(
        repository_source_digest=page.coordinate.repository_source_digest,
        graph_revision=page.coordinate.graph_revision,
        hits=[asdict(hit) for hit in page.hits],
        cursor=None,
        truncated=page.truncated,
    )


def sync_repository_facts(
    services: ApplicationServices,
    mode: Literal["auto", "full"] = "auto",
) -> SyncFactsOutput:
    """Refresh only disposable code facts; formal state stays untouched."""
    result = services.synchronize_facts(mode)
    return SyncFactsOutput(
        repository_source_digest=result.repository_source_digest,
        graph_revision=result.graph_revision,
        index_generation=result.index_generation,
        parsed_files=result.parsed_files,
        added_files=result.added_files,
        changed_files=result.changed_files,
        deleted_files=result.deleted_files,
        retry_count=result.retry_count,
        rebuilt=result.rebuilt,
        diagnostics=[asdict(diagnostic) for diagnostic in result.diagnostics],
    )


_MAX_FRESHNESS_PAGE_SIZE = 100
_DEFAULT_FRESHNESS_PAGE_SIZE = 50
_MAX_QUERY_SCOPE_IDS = 100


def cognitive_freshness(
    services: ApplicationServices, *, preflight: bool
) -> CognitiveFreshnessOutput:
    """Return a bounded repository status after Main prep or Analyzer readback."""
    snapshot = services.freshness_snapshot(preflight=preflight)
    change_set = snapshot.change_set
    if change_set is None:
        return CognitiveFreshnessOutput(
            repository_status=snapshot.repository_status.status,
            baseline_source_digest=snapshot.baseline_source_digest,
            current_source_digest=snapshot.current_source_digest,
            change_set_id=None,
            file_diff_completeness=None,
            entity_diff_completeness=None,
            scope_confidence=None,
            affected_nodes=[],
            affected_flows=[],
            affected_entities=[],
            diagnostics=[],
            reason_codes=list(snapshot.repository_status.reason_codes),
            truncated=False,
        )
    affected_nodes, nodes_truncated = _bounded_items(
        change_set.affected_nodes, 0, _DEFAULT_FRESHNESS_PAGE_SIZE
    )
    affected_flows, flows_truncated = _bounded_items(
        change_set.affected_flows, 0, _DEFAULT_FRESHNESS_PAGE_SIZE
    )
    affected_entities, entities_truncated = _bounded_items(
        change_set.affected_entities, 0, _DEFAULT_FRESHNESS_PAGE_SIZE
    )
    diagnostics, diagnostics_truncated = _bounded_items(
        change_set.diagnostics, 0, _DEFAULT_FRESHNESS_PAGE_SIZE
    )
    return CognitiveFreshnessOutput(
        repository_status=snapshot.repository_status.status,
        baseline_source_digest=snapshot.baseline_source_digest,
        current_source_digest=snapshot.current_source_digest,
        change_set_id=change_set.change_set_id,
        file_diff_completeness=change_set.file_diff_completeness,
        entity_diff_completeness=change_set.entity_diff_completeness,
        scope_confidence=change_set.scope_confidence,
        affected_nodes=list(affected_nodes),
        affected_flows=list(affected_flows),
        affected_entities=list(affected_entities),
        diagnostics=list(diagnostics),
        reason_codes=list(snapshot.repository_status.reason_codes),
        truncated=(
            nodes_truncated
            or flows_truncated
            or entities_truncated
            or diagnostics_truncated
            or len(change_set.unmapped_changes) > _DEFAULT_FRESHNESS_PAGE_SIZE
        ),
    )


def pending_changes(
    services: ApplicationServices,
    cursor: str | None = None,
    limit: int = _DEFAULT_FRESHNESS_PAGE_SIZE,
    *,
    preflight: bool,
) -> PendingChangesOutput:
    """Return bounded details of the one effective ChangeSet, if any."""
    offset, page_size = _page_window(cursor, limit)
    snapshot = services.freshness_snapshot(preflight=preflight)
    change_set = snapshot.change_set
    if change_set is None:
        return PendingChangesOutput(
            repository_status=snapshot.repository_status.status,
            baseline_source_digest=snapshot.baseline_source_digest,
            current_source_digest=snapshot.current_source_digest,
            change_set_id=None,
            file_diff_completeness=None,
            entity_diff_completeness=None,
            scope_confidence=None,
            changed_files=[],
            changed_entities=[],
            affected_nodes=[],
            affected_flows=[],
            affected_entities=[],
            unmapped_changes=[],
            diagnostics=[],
            reason_codes=list(snapshot.repository_status.reason_codes),
            cursor=None,
            truncated=False,
        )
    collections: tuple[Sequence[Any], ...] = (
        _file_changes(change_set),
        _entity_changes(change_set),
        change_set.affected_nodes,
        change_set.affected_flows,
        change_set.affected_entities,
        change_set.unmapped_changes,
        change_set.diagnostics,
    )
    pages = [_bounded_items(values, offset, page_size) for values in collections]
    truncated = any(is_truncated for _, is_truncated in pages)
    next_cursor = str(offset + page_size) if truncated else None
    changed_files, changed_entities, nodes, flows, entities, unmapped, diagnostics = (
        page for page, _ in pages
    )
    return PendingChangesOutput(
        repository_status=snapshot.repository_status.status,
        baseline_source_digest=snapshot.baseline_source_digest,
        current_source_digest=snapshot.current_source_digest,
        change_set_id=change_set.change_set_id,
        file_diff_completeness=change_set.file_diff_completeness,
        entity_diff_completeness=change_set.entity_diff_completeness,
        scope_confidence=change_set.scope_confidence,
        changed_files=[dict(item) for item in changed_files],
        changed_entities=[dict(item) for item in changed_entities],
        affected_nodes=list(nodes),
        affected_flows=list(flows),
        affected_entities=list(entities),
        unmapped_changes=[dict(item) for item in unmapped],
        diagnostics=list(diagnostics),
        reason_codes=list(snapshot.repository_status.reason_codes),
        cursor=next_cursor,
        truncated=truncated,
    )


def effective_query_freshness(
    services: ApplicationServices,
    node_ids: Sequence[str] = (),
    entity_ids: Sequence[str] = (),
    max_unmapped_changes: int = _DEFAULT_FRESHNESS_PAGE_SIZE,
    *,
    preflight: bool,
) -> EffectiveQueryFreshnessOutput:
    """Return Core's local source-first decision without unbounded evidence."""
    nodes = _bounded_identifiers(node_ids, "node_ids")
    entities = _bounded_identifiers(entity_ids, "entity_ids")
    detail_limit = _bounded_limit(max_unmapped_changes, "max_unmapped_changes")
    snapshot = services.freshness_snapshot(preflight=preflight)
    result = FreshnessService(snapshot.change_set).for_query(nodes, entities)
    unmapped, unmapped_truncated = _bounded_items(
        result.unmapped_changes, 0, detail_limit
    )
    return EffectiveQueryFreshnessOutput(
        status=result.status,
        repository_status=result.repository_status,
        baseline_source_digest=snapshot.baseline_source_digest,
        current_source_digest=snapshot.current_source_digest,
        change_set_id=result.change_set_id,
        scope_confidence=result.scope_confidence,
        matched_affected_nodes=list(result.matched_affected_nodes),
        matched_affected_entities=list(result.matched_affected_entities),
        unmapped_changes=[dict(item) for item in unmapped],
        unmapped_changes_truncated=unmapped_truncated,
        reason_codes=list(result.reason_codes),
    )


def _file_changes(change_set: Any) -> tuple[dict[str, object], ...]:
    records = [
        {"kind": kind, "relative_path": path}
        for kind, paths in (
            ("added", change_set.changed_files.added),
            ("modified", change_set.changed_files.modified),
            ("deleted", change_set.changed_files.deleted),
        )
        for path in paths
    ]
    records.extend(
        {"kind": "renamed", "old_relative_path": old, "relative_path": new}
        for old, new in change_set.changed_files.renamed
    )
    return tuple(sorted(records, key=lambda item: (str(item.get("relative_path")), str(item["kind"]))))


def _entity_changes(change_set: Any) -> tuple[dict[str, object], ...]:
    records = [
        {"kind": kind, "entity_uid": uid}
        for kind, entities in (
            ("added", change_set.changed_entities.added),
            ("modified", change_set.changed_entities.modified),
            ("missing", change_set.changed_entities.missing),
        )
        for uid in entities
    ]
    records.extend(
        {
            "kind": "moved",
            "entity_uid": uid,
            "old_relative_path": old,
            "relative_path": new,
        }
        for uid, old, new in change_set.changed_entities.moved
    )
    return tuple(sorted(records, key=lambda item: (str(item["entity_uid"]), str(item["kind"]))))


def _page_window(cursor: str | None, limit: int) -> tuple[int, int]:
    if cursor is None:
        offset = 0
    elif (
        isinstance(cursor, str)
        and len(cursor) <= 10
        and cursor.isascii()
        and cursor.isdecimal()
    ):
        offset = int(cursor)
    else:
        raise ValueError("cursor must be a non-negative decimal offset")
    return offset, _bounded_limit(limit, "limit")


def _bounded_limit(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_FRESHNESS_PAGE_SIZE:
        raise ValueError(f"{name} must be an integer from 1 to {_MAX_FRESHNESS_PAGE_SIZE}")
    return value


def _bounded_identifiers(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be a list of identifiers")
    result = tuple(values)
    if len(result) > _MAX_QUERY_SCOPE_IDS:
        raise ValueError(f"{name} may contain at most {_MAX_QUERY_SCOPE_IDS} identifiers")
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain non-empty identifiers")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _bounded_items(
    values: Sequence[Any], offset: int, limit: int
) -> tuple[tuple[Any, ...], bool]:
    page = tuple(values[offset : offset + limit])
    return page, offset + limit < len(values)


def as_tool_error(error: CodeCortexError) -> ToolError:
    """Preserve the stable Core error contract inside an anticipated MCP error."""
    return ToolError(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "error": error.to_dict()},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def invalid_argument(error: ValueError | TypeError) -> CodeCortexError:
    """Translate request-validation failures into the stable error contract."""
    return CodeCortexError(
        ErrorCode.INVALID_ARGUMENT,
        str(error),
        suggested_action="Fix the request arguments and retry",
    )


def _graph_output(graph: CognitiveGraph) -> CognitiveGraphOutput:
    return CognitiveGraphOutput(
        graph_revision=graph.graph_revision,
        nodes=[dict(item) for item in graph.nodes],
        semantic_edges=[dict(item) for item in graph.semantic_edges],
        logical_flows=[dict(item) for item in graph.logical_flows],
        implementation_mappings=[dict(item) for item in graph.implementation_mappings],
    )


def _edge_endpoints(edge: Mapping[str, object]) -> set[str]:
    """Support the M0 edge shapes while avoiding recursive heuristic matching."""
    return {
        value
        for key in (
            "source_id",
            "target_id",
            "source",
            "target",
            "from_node_id",
            "to_node_id",
        )
        if isinstance((value := edge.get(key)), str)
    }


def _proposal_payload(proposal: Proposal) -> dict[str, Any]:
    """Serialize an immutable domain Proposal without leaking its Python types."""
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "status": proposal.status.value,
        "base_graph_revision": proposal.base_graph_revision,
        "analyzed_source_digest": proposal.analyzed_source_digest,
        "source_preconditions": [
            json_value_to_mutable(item) for item in proposal.source_preconditions
        ],
        "operations": [item.to_canonical_value() for item in proposal.operations],
        "affected_nodes": list(proposal.affected_nodes),
        "reason": proposal.reason,
        "evidence": [json_value_to_mutable(item) for item in proposal.evidence],
        "uncertainties": [json_value_to_mutable(item) for item in proposal.uncertainties],
        "revision_log": [
            {
                "revision_number": revision.revision_number,
                "reason": revision.reason,
                "previous_reason": revision.previous_reason,
                "previous_patch_digest": revision.previous_patch_digest,
                "previous_operations": [
                    item.to_canonical_value() for item in revision.previous_operations
                ],
                "previous_analyzed_source_digest": revision.previous_analyzed_source_digest,
                "previous_source_preconditions": [
                    json_value_to_mutable(item)
                    for item in revision.previous_source_preconditions
                ],
                "previous_affected_nodes": list(revision.previous_affected_nodes),
                "previous_evidence": [
                    json_value_to_mutable(item) for item in revision.previous_evidence
                ],
                "previous_uncertainties": [
                    json_value_to_mutable(item)
                    for item in revision.previous_uncertainties
                ],
                "revised_at": revision.revised_at,
            }
            for revision in proposal.revision_log
        ],
        "patch_digest": proposal.patch_digest,
        "created_at": proposal.created_at,
    }
