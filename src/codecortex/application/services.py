"""Application use cases coordinating formal state and repository locking."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

from codecortex.application.ports import (
    FormalStorePort,
    PendingProposalStorePort,
    RepositoryContextPort,
    RepositoryLockPort,
)
from codecortex.domain.cognition import (
    CognitiveGraph,
    FormalState,
    HistoryEventRef,
    Manifest,
    SourceBaseline,
    ValidationResult,
    validate_formal_state,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, new_id
from codecortex.domain.proposals import (
    ApprovalRecord,
    PatchOperation,
    PatchOperationKind,
    Proposal,
    ProposalRevision,
    ProposalStatus,
    json_value_to_mutable,
)
from codecortex.infrastructure.views import render_views


@dataclass(frozen=True)
class RepositoryOverview:
    """Bounded adapter-facing summary of one formal repository."""

    repository_root: str
    graph_revision: int
    cognition_initialized: bool
    cognition_baseline_source_digest: str | None
    formal_files: dict[str, bool]


@dataclass(frozen=True)
class ApplyResult:
    """Stable adapter-facing outcome of one applied cognitive proposal."""

    event_id: str
    graph_revision: int
    applied_proposal_id: str


@dataclass
class ApplicationServices:
    """Coordinate use cases while keeping policy out of CLI and MCP adapters."""

    repository: RepositoryContextPort
    formal_store: FormalStorePort
    repository_lock: RepositoryLockPort
    lock_timeout_seconds: float = 10
    pending_proposals: PendingProposalStorePort | None = None

    def initialize_repository(self) -> RepositoryOverview:
        """Idempotently establish the revision-zero technical skeleton."""
        manifest = Manifest(
            schema_version=1,
            graph_revision=0,
            cognition_initialized=False,
            cognition_baseline=None,
        )
        baseline = SourceBaseline.empty()
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            state = self.formal_store.initialize(
                FormalState.empty(manifest=manifest, source_baseline=baseline)
            )
            return self._overview(state)

    def validate_graph(self) -> ValidationResult:
        """Load and validate the complete formal state under a shared lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            self.formal_store.load()
        return ValidationResult(valid=True, issues=())

    def repository_overview(self) -> RepositoryOverview:
        """Return the current formal-state summary under a shared lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            state = self.formal_store.load()
            return self._overview(state)

    def create_cognitive_proposal(
        self,
        *,
        operations: tuple[PatchOperation, ...],
        affected_nodes: tuple[str, ...],
        reason: str,
        analyzed_source_digest: str | None = None,
        source_preconditions: tuple[dict[str, object], ...] = (),
        evidence: tuple[dict[str, object], ...] = (),
        uncertainties: tuple[object, ...] = (),
        proposal_id: str | None = None,
        created_at: str | None = None,
    ) -> Proposal:
        """Create and persist a proposal bound to the current graph revision."""
        store = self._pending_store()
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            state = self.formal_store.load()
            proposal = Proposal.create(
                proposal_id=proposal_id or new_id(IdPrefix.PROPOSAL),
                base_graph_revision=state.graph.graph_revision,
                analyzed_source_digest=analyzed_source_digest,
                source_preconditions=source_preconditions,
                operations=operations,
                affected_nodes=affected_nodes,
                reason=reason,
                evidence=evidence,
                uncertainties=uncertainties,
                created_at=(
                    _utc_now_rfc3339() if created_at is None else created_at
                ),
            )
            store.create(proposal)
            return proposal

    def revise_cognitive_proposal(
        self,
        proposal_id: str,
        *,
        operations: tuple[PatchOperation, ...],
        reason: str,
        revised_at: str | None = None,
        analyzed_source_digest: str | None = None,
        source_preconditions: tuple[dict[str, object], ...] | None = None,
        affected_nodes: tuple[str, ...] | None = None,
        evidence: tuple[dict[str, object], ...] | None = None,
        uncertainties: tuple[object, ...] | None = None,
    ) -> Proposal:
        """Atomically replace current proposal content and append its revision record."""
        store = self._pending_store()
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            state = self.formal_store.load()
            current = store.load(proposal_id)
            current.verify_base_graph_revision(state.graph.graph_revision)
            revised = current.revise(
                operations,
                reason=reason,
                revised_at=(
                    _utc_now_rfc3339() if revised_at is None else revised_at
                ),
                analyzed_source_digest=analyzed_source_digest,
                source_preconditions=source_preconditions,
                affected_nodes=affected_nodes,
                evidence=evidence,
                uncertainties=uncertainties,
            )
            store.replace(revised)
            return revised

    def cognitive_proposal(self, proposal_id: str) -> Proposal:
        """Read the current pending proposal snapshot under the repository lock."""
        store = self._pending_store()
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            self.formal_store.load()
            return store.load(proposal_id)

    def apply_cognitive_proposal(
        self, proposal_id: str, approval: ApprovalRecord
    ) -> ApplyResult:
        """Commit one approved proposal as a single recoverable formal transaction.

        Under the exclusive repository lock, any interrupted prior
        transaction is recovered, preconditions are reloaded and verified,
        the patch is applied and validated in memory, and the resulting
        event, graph, entity refs, unchanged source baseline, and views
        are committed with the manifest as the last commit marker. Only a
        successful commit consumes the pending proposal; the formal event
        already carries its complete snapshot.
        """
        store = self._pending_store()
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            self.formal_store.recover()
            state = self.formal_store.load()
            proposal = store.load(proposal_id)
            proposal.verify_approval(approval)
            proposal.verify_base_graph_revision(state.graph.graph_revision)
            event_id = new_id(IdPrefix.EVENT)
            new_revision = state.graph.graph_revision + 1
            applied = _applied_formal_state(state, proposal, event_id, new_revision)
            result = validate_formal_state(applied)
            if not result.valid:
                raise CodeCortexError(
                    ErrorCode.ANALYSIS_REPORT_INVALID,
                    "Applied proposal does not produce a valid formal graph",
                    details={
                        "proposal_id": proposal_id,
                        "issues": [
                            f"{issue.code.value} at {issue.location}: "
                            f"{issue.message}"
                            for issue in result.issues
                        ],
                    },
                    suggested_action="Revise or recreate the proposal",
                )
            views = render_views(applied.graph)
            event = _history_event(proposal, approval, event_id, new_revision)
            self.formal_store.commit(applied, event, views)
            store.delete(proposal_id)
            return ApplyResult(
                event_id=event_id,
                graph_revision=new_revision,
                applied_proposal_id=proposal.proposal_id,
            )

    def _pending_store(self) -> PendingProposalStorePort:
        if self.pending_proposals is None:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "Pending proposal storage is not configured",
            )
        return self.pending_proposals

    def _overview(self, state: FormalState) -> RepositoryOverview:
        return RepositoryOverview(
            repository_root=self.repository.root.as_posix(),
            graph_revision=state.manifest.graph_revision,
            cognition_initialized=state.manifest.cognition_initialized,
            cognition_baseline_source_digest=state.manifest.cognition_baseline,
            formal_files=self.formal_store.formal_file_presence(),
        )


def _utc_now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _applied_formal_state(
    state: FormalState,
    proposal: Proposal,
    event_id: str,
    new_revision: int,
) -> FormalState:
    """Build the complete new formal snapshot for one approved proposal."""
    return FormalState(
        manifest=replace(state.manifest, graph_revision=new_revision),
        graph=_apply_patch_operations(
            state.graph, proposal, event_id, new_revision
        ),
        entity_refs=replace(state.entity_refs, graph_revision=new_revision),
        source_baseline=state.source_baseline,
        history_events=(
            *state.history_events,
            HistoryEventRef(event_id, "cognitive_proposal_applied"),
        ),
    )


def _apply_patch_operations(
    graph: CognitiveGraph,
    proposal: Proposal,
    event_id: str,
    new_revision: int,
) -> CognitiveGraph:
    """Apply node and edge operations, injecting event provenance."""
    provenance = {"approval_event_id": event_id}
    nodes = {str(node.get("id")): dict(node) for node in graph.nodes}
    node_order = [str(node.get("id")) for node in graph.nodes]
    edges = {str(edge.get("id")): dict(edge) for edge in graph.semantic_edges}
    edge_order = [str(edge.get("id")) for edge in graph.semantic_edges]
    for operation in proposal.operations:
        kind = PatchOperationKind(operation.kind)
        target = operation.target_id
        if kind is PatchOperationKind.ADD_NODE:
            if target in nodes:
                raise _invalid_patch(f"Node already exists: {target}")
            nodes[target] = _patched_record(operation, provenance, new_revision)
            node_order.append(target)
        elif kind is PatchOperationKind.UPDATE_NODE:
            if target not in nodes:
                raise _invalid_patch(f"Node does not exist: {target}")
            nodes[target] = _patched_record(operation, provenance, new_revision)
        elif kind is PatchOperationKind.REMOVE_NODE:
            if target not in nodes:
                raise _invalid_patch(f"Node does not exist: {target}")
            del nodes[target]
            node_order.remove(target)
        elif kind is PatchOperationKind.ADD_EDGE:
            if target in edges:
                raise _invalid_patch(f"Edge already exists: {target}")
            edges[target] = _patched_record(
                operation, provenance, new_revision, revision_key="edge_revision"
            )
            edge_order.append(target)
        elif kind is PatchOperationKind.UPDATE_EDGE:
            if target not in edges:
                raise _invalid_patch(f"Edge does not exist: {target}")
            edges[target] = _patched_record(
                operation, provenance, new_revision, revision_key="edge_revision"
            )
        else:
            if target not in edges:
                raise _invalid_patch(f"Edge does not exist: {target}")
            del edges[target]
            edge_order.remove(target)
    for node_id in proposal.affected_nodes:
        node = nodes.get(node_id)
        if node is not None:
            node["approval"] = dict(provenance)
            node["node_revision"] = new_revision
    return CognitiveGraph(
        graph.schema_version,
        new_revision,
        tuple(nodes[node_id] for node_id in node_order),
        tuple(edges[edge_id] for edge_id in edge_order),
        graph.logical_flows,
        graph.implementation_mappings,
    )


def _patched_record(
    operation: PatchOperation,
    provenance: dict[str, str],
    new_revision: int,
    *,
    revision_key: str = "node_revision",
) -> dict[str, object]:
    value = cast(dict[str, object], json_value_to_mutable(operation.value))
    record = dict(value)
    record["approval"] = dict(provenance)
    record[revision_key] = new_revision
    return record


def _history_event(
    proposal: Proposal,
    approval: ApprovalRecord,
    event_id: str,
    new_revision: int,
) -> dict[str, object]:
    """Build the self-contained immutable audit event for one apply."""
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "cognitive_proposal_applied",
        "proposal_id": proposal.proposal_id,
        "base_graph_revision": proposal.base_graph_revision,
        "graph_revision": new_revision,
        "reason": proposal.reason,
        "patch_digest": proposal.patch_digest,
        "affected_nodes": list(proposal.affected_nodes),
        "proposal_snapshot": _proposal_snapshot(proposal),
        "approval": {
            "proposal_id": approval.proposal_id,
            "patch_digest": approval.patch_digest,
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
            "approval_summary": approval.approval_summary,
        },
        "change_set_summary": {
            "before_source_digest": proposal.analyzed_source_digest,
            "after_source_digest": proposal.analyzed_source_digest,
            "changed_files": [],
            "changed_entities": [],
            "affected_nodes": list(proposal.affected_nodes),
            "scope_confidence": "complete",
            "unmapped_changes": [],
        },
        "applied_at": _utc_now_rfc3339(),
    }


def _proposal_snapshot(proposal: Proposal) -> dict[str, object]:
    """Serialize the complete applied proposal into the formal event."""
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "status": ProposalStatus.APPLIED.value,
        "base_graph_revision": proposal.base_graph_revision,
        "analyzed_source_digest": proposal.analyzed_source_digest,
        "source_preconditions": [
            json_value_to_mutable(item) for item in proposal.source_preconditions
        ],
        "operations": [
            operation.to_canonical_value() for operation in proposal.operations
        ],
        "affected_nodes": list(proposal.affected_nodes),
        "reason": proposal.reason,
        "evidence": [json_value_to_mutable(item) for item in proposal.evidence],
        "uncertainties": [
            json_value_to_mutable(item) for item in proposal.uncertainties
        ],
        "revision_log": [
            _revision_snapshot(revision) for revision in proposal.revision_log
        ],
        "patch_digest": proposal.patch_digest,
        "created_at": proposal.created_at,
    }


def _revision_snapshot(revision: ProposalRevision) -> dict[str, object]:
    return {
        "revision_number": revision.revision_number,
        "reason": revision.reason,
        "previous_reason": revision.previous_reason,
        "previous_patch_digest": revision.previous_patch_digest,
        "previous_operations": [
            operation.to_canonical_value()
            for operation in revision.previous_operations
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
            json_value_to_mutable(item) for item in revision.previous_uncertainties
        ],
        "revised_at": revision.revised_at,
    }


def _invalid_patch(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.ANALYSIS_REPORT_INVALID,
        message,
        suggested_action="Revise or recreate the proposal",
    )
