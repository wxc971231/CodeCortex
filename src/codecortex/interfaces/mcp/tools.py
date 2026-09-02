"""Versioned DTO adapters for the static CodeCortex MCP tool sets."""

import json
from collections.abc import Mapping
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

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

    def to_domain(self) -> PatchOperation:
        return PatchOperation(self.kind, self.target_id, self.value)


class ProposalInput(_Dto):
    """Bounded wire input for a new M0 cognitive proposal."""

    operations: list[PatchOperationInput] = Field(min_length=1, max_length=100)
    affected_nodes: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=4_000)
    analyzed_source_digest: str | None = None
    source_preconditions: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    uncertainties: list[Any] = Field(default_factory=list, max_length=100)


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
    """Return one node, its directly attached edges, and approval provenance."""
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
    approval = node.get("approval")
    approval_event_id = (
        approval.get("approval_event_id")
        if isinstance(approval, Mapping)
        and isinstance(approval.get("approval_event_id"), str)
        else None
    )
    return InspectNodeOutput(
        node=dict(node), relations=relations, approval_event_id=approval_event_id
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
    )


def as_tool_error(error: CodeCortexError) -> ToolError:
    """Preserve the stable Core error contract inside an anticipated MCP error."""
    return ToolError(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "error": error.to_dict()},
            ensure_ascii=False,
            sort_keys=True,
        )
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
