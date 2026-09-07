"""Analysis-backed proposal creation and full-graph apply for M1a.

Design sections 13-15: an AnalysisReport becomes one aggregate pending
Proposal with stable-ID operations; approval changes nothing formal.  Apply
re-verifies approval, base graph revision, the full repository source digest,
and every fine-grained source precondition, then commits the event, graph,
entity refs, source baseline, views, and manifest in the M0 journaled
transaction.  Only after the formal commit succeeds does apply refresh the
disposable caches (cognitive replica and baseline entity snapshots); a cache
failure never rolls back formal truth and is reported as a rebuild warning.

Documented decisions:

- The repository lock is not re-entrant (each acquire opens a fresh file
  description), so Fact Sync runs immediately before the exclusive lock and
  the injected ``source_probe`` re-digests the managed source set from disk
  while the lock is held.  Both must agree with the report/proposal digest.
- ``cognition_initialized`` and the source baseline advance only when the
  proposal pins ``analyzed_source_digest`` (an analysis-grounded proposal).
  Digest-less manual proposals keep the M0 semantics: they advance the graph
  revision but leave the baseline untouched, and their per-file source
  preconditions are checked against the probe digests.
- Provenance is stamped only on records an operation actually creates or
  replaces (section 14: approval points to the latest event that changed the
  object).  ``remove_node`` requires the same patch to explicitly handle every
  referencing edge, flow, and mapping; Core performs no invisible cascade.
- ``set_logical_flow`` with a null value deletes the behavior's flow; it is
  the only flow-removal operation in the section 13 operation set.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from codecortex.application.ports import (
    FactSyncPort,
    FormalStorePort,
    PendingProposalStorePort,
    RepositoryLockPort,
    ViewRendererPort,
)
from codecortex.domain import graph as graph_model
from codecortex.domain.analysis import (
    AnalysisOperationKind,
    AnalysisReport,
)
from codecortex.domain.cognition import (
    DIGEST_PROFILE_VERSION,
    MANAGED_SOURCE_SET_VERSION,
    SCHEMA_VERSION,
    CognitiveGraph,
    FormalState,
    HistoryEventRef,
    SourceBaseline,
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
from codecortex.infrastructure.formal import view_manifest_for
from codecortex.infrastructure.persistence.entity_refs import recompute_entity_refs
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.infrastructure.views import render_legacy_views
from codecortex.telemetry import traced

SourceProbe = Callable[[], "ManagedSourceSnapshot"]


@dataclass(frozen=True)
class ManagedSourceSnapshot:
    """A fresh disk-backed digest view of the whole managed source set."""

    repository_source_digest: str
    file_digests: Mapping[str, str]


@dataclass(frozen=True)
class GraphApplyResult:
    """Outcome of one applied proposal, including cache-refresh warnings."""

    event_id: str
    graph_revision: int
    applied_proposal_id: str
    cache_warnings: tuple[str, ...] = ()


def operations_from_report(
    report: AnalysisReport,
    graph: CognitiveGraph | None = None,
    *,
    cognition_initialized: bool = False,
) -> tuple[PatchOperation, ...]:
    """Convert a report into primitives after checking target expectations.

    Legacy candidate-only reports remain an initialization-only compatibility
    form.  Once cognition exists, every change must be explicit so omission
    can never be interpreted as replacement or deletion.
    """
    if report.change_operations is not None:
        if graph is None:
            raise _invalid_patch(
                "Explicit analysis changes require the current formal graph"
            )
        return _explicit_operations_from_report(report, graph)
    if cognition_initialized:
        raise _invalid_patch(
            "An initialized graph requires explicit AnalysisReport change_operations"
        )
    operations = [
        PatchOperation("add_node", node.id, _node_value(node))
        for node in report.candidate_nodes
    ]
    operations.extend(
        PatchOperation("add_edge", edge.id, _edge_value(edge))
        for edge in report.candidate_edges
    )
    operations.extend(
        PatchOperation("set_logical_flow", flow.behavior_id, _flow_value(flow))
        for flow in report.candidate_flows
    )
    operations.extend(
        PatchOperation("add_mapping", mapping.id, _mapping_value(mapping))
        for mapping in report.candidate_mappings
    )
    return tuple(operations)


def _explicit_operations_from_report(
    report: AnalysisReport, graph: CognitiveGraph
) -> tuple[PatchOperation, ...]:
    candidates: dict[tuple[str, str], dict[str, object]] = {
        **{("node", node.id): _node_value(node) for node in report.candidate_nodes},
        **{("edge", edge.id): _edge_value(edge) for edge in report.candidate_edges},
        **{
            ("flow", flow.behavior_id): _flow_value(flow)
            for flow in report.candidate_flows
        },
        **{
            ("mapping", mapping.id): _mapping_value(mapping)
            for mapping in report.candidate_mappings
        },
    }
    current: dict[tuple[str, str], Mapping[str, object]] = {
        **{("node", str(item.get("id"))): item for item in graph.nodes},
        **{("edge", str(item.get("id"))): item for item in graph.semantic_edges},
        **{
            ("flow", str(item.get("behavior_id"))): item
            for item in graph.logical_flows
        },
        **{
            ("mapping", str(item.get("id"))): item
            for item in graph.implementation_mappings
        },
    }
    revision_keys = {
        "node": "node_revision",
        "edge": "edge_revision",
        "flow": "flow_revision",
        "mapping": "mapping_revision",
    }
    operations: list[PatchOperation] = []
    consumed: set[tuple[str, str]] = set()
    for requested in report.change_operations or ():
        object_kind = _analysis_operation_object_kind(requested.kind)
        target = (object_kind, requested.target_id)
        existing = current.get(target)
        if requested.before_revision is None:
            if existing is not None:
                raise _invalid_patch(
                    f"Expected {object_kind} to be absent before change: "
                    f"{requested.target_id}"
                )
        else:
            if existing is None:
                raise _invalid_patch(
                    f"Expected {object_kind} does not exist: {requested.target_id}"
                )
            actual_revision = existing.get(revision_keys[object_kind])
            if actual_revision != requested.before_revision:
                raise _invalid_patch(
                    f"Analysis before_revision does not match current {object_kind}: "
                    f"{requested.target_id}"
                )
        if requested.kind is AnalysisOperationKind.REMOVE_LOGICAL_FLOW:
            operations.append(
                PatchOperation(
                    "set_logical_flow",
                    requested.target_id,
                    None,
                    expected_revision=requested.before_revision,
                )
            )
        elif requested.kind.value.startswith("remove_"):
            operations.append(
                PatchOperation(
                    requested.kind.value,
                    requested.target_id,
                    None,
                    expected_revision=requested.before_revision,
                )
            )
        else:
            value = candidates.get(target)
            if value is None:
                raise _invalid_patch(
                    f"Analysis operation has no candidate value: {requested.target_id}"
                )
            consumed.add(target)
            operations.append(
                PatchOperation(
                    requested.kind.value,
                    requested.target_id,
                    value,
                    expected_revision=(
                        "absent"
                        if requested.before_revision is None
                        else requested.before_revision
                    ),
                )
            )
    unused = set(candidates) - consumed
    if unused:
        raise _invalid_patch(
            "Explicit analysis candidates must each have one change operation"
        )
    return tuple(operations)


def _analysis_operation_object_kind(kind: AnalysisOperationKind) -> str:
    if kind.value.endswith("_node"):
        return "node"
    if kind.value.endswith("_edge"):
        return "edge"
    if kind.value.endswith("_mapping"):
        return "mapping"
    return "flow"


def affected_nodes_from_report(
    report: AnalysisReport, graph: CognitiveGraph | None = None
) -> tuple[str, ...]:
    """Collect the sorted semantic-node scope one report touches."""
    affected = {node.id for node in report.candidate_nodes}
    affected.update(flow.behavior_id for flow in report.candidate_flows)
    for edge in report.candidate_edges:
        affected.update((edge.source_id, edge.target_id))
    affected.update(
        mapping.subject_id
        for mapping in report.candidate_mappings
        if mapping.subject_kind == graph_model.MappingSubjectKind.NODE
    )
    affected.update(
        mapping.subject_id.split("#step.", 1)[0]
        for mapping in report.candidate_mappings
        if mapping.subject_kind == graph_model.MappingSubjectKind.FLOW_STEP
    )
    if report.change_operations is not None:
        current_edges = (
            {}
            if graph is None
            else {str(item.get("id")): item for item in graph.semantic_edges}
        )
        current_mappings = (
            {}
            if graph is None
            else {
                str(item.get("id")): item
                for item in graph.implementation_mappings
            }
        )
        for operation in report.change_operations:
            object_kind = _analysis_operation_object_kind(operation.kind)
            if object_kind in {"node", "flow"}:
                affected.add(operation.target_id)
            elif object_kind == "edge":
                current_edge = current_edges.get(operation.target_id)
                if current_edge is not None:
                    affected.update(
                        str(endpoint)
                        for endpoint in (
                            current_edge.get("source_id"),
                            current_edge.get("target_id"),
                        )
                        if isinstance(endpoint, str)
                    )
            else:
                mapping = current_mappings.get(operation.target_id)
                if mapping is not None:
                    subject = mapping.get("subject_id")
                    if isinstance(subject, str):
                        affected.add(subject.split("#step.", 1)[0])
    return tuple(sorted(affected))


def apply_operations_to_graph(
    graph: CognitiveGraph,
    operations: tuple[PatchOperation, ...],
    event_id: str,
    new_revision: int,
) -> CognitiveGraph:
    """Apply validated patch operations in memory with provenance injection."""
    provenance = {"approval_event_id": event_id}
    nodes = {str(node.get("id")): dict(node) for node in graph.nodes}
    node_order = [str(node.get("id")) for node in graph.nodes]
    edges = {str(edge.get("id")): dict(edge) for edge in graph.semantic_edges}
    edge_order = [str(edge.get("id")) for edge in graph.semantic_edges]
    flows = {
        str(flow.get("behavior_id")): dict(flow) for flow in graph.logical_flows
    }
    flow_order = [str(flow.get("behavior_id")) for flow in graph.logical_flows]
    mappings = {
        str(mapping.get("id")): dict(mapping)
        for mapping in graph.implementation_mappings
    }
    mapping_order = [
        str(mapping.get("id")) for mapping in graph.implementation_mappings
    ]
    removed_nodes: list[str] = []
    for operation in operations:
        kind = PatchOperationKind(operation.kind)
        target = operation.target_id
        _verify_target_revision(
            operation,
            nodes=nodes,
            edges=edges,
            flows=flows,
            mappings=mappings,
        )
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
            removed_nodes.append(target)
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
        elif kind is PatchOperationKind.REMOVE_EDGE:
            if target not in edges:
                raise _invalid_patch(f"Edge does not exist: {target}")
            del edges[target]
            edge_order.remove(target)
        elif kind is PatchOperationKind.SET_LOGICAL_FLOW:
            if operation.value is None:
                if target not in flows:
                    raise _invalid_patch(f"Logical flow does not exist: {target}")
                del flows[target]
                flow_order.remove(target)
            else:
                flows[target] = _patched_flow(operation, provenance, new_revision)
                if target not in flow_order:
                    flow_order.append(target)
        elif kind is PatchOperationKind.ADD_MAPPING:
            if target in mappings:
                raise _invalid_patch(f"Mapping already exists: {target}")
            mappings[target] = _patched_record(
                operation, provenance, new_revision, revision_key="mapping_revision"
            )
            mapping_order.append(target)
        elif kind is PatchOperationKind.UPDATE_MAPPING:
            if target not in mappings:
                raise _invalid_patch(f"Mapping does not exist: {target}")
            mappings[target] = _patched_record(
                operation, provenance, new_revision, revision_key="mapping_revision"
            )
        else:
            if target not in mappings:
                raise _invalid_patch(f"Mapping does not exist: {target}")
            del mappings[target]
            mapping_order.remove(target)
    _require_explicit_node_removals(removed_nodes, edges, flows, mappings)
    return CognitiveGraph(
        graph.schema_version,
        new_revision,
        tuple(nodes[node_id] for node_id in node_order),
        tuple(edges[edge_id] for edge_id in edge_order),
        tuple(flows[behavior_id] for behavior_id in flow_order),
        tuple(mappings[mapping_id] for mapping_id in mapping_order),
    )


def _verify_target_revision(
    operation: PatchOperation,
    *,
    nodes: Mapping[str, Mapping[str, object]],
    edges: Mapping[str, Mapping[str, object]],
    flows: Mapping[str, Mapping[str, object]],
    mappings: Mapping[str, Mapping[str, object]],
) -> None:
    expected = operation.expected_revision
    if expected is None:
        return
    kind = PatchOperationKind(operation.kind)
    if kind.value.endswith("_node"):
        records, revision_key = nodes, "node_revision"
    elif kind.value.endswith("_edge"):
        records, revision_key = edges, "edge_revision"
    elif kind.value.endswith("_mapping"):
        records, revision_key = mappings, "mapping_revision"
    else:
        records, revision_key = flows, "flow_revision"
    current = records.get(operation.target_id)
    if expected == "absent":
        if current is not None:
            raise _invalid_patch(
                f"Target expected to be absent: {operation.target_id}"
            )
        return
    actual = None if current is None else current.get(revision_key)
    if actual != expected:
        raise _invalid_patch(
            f"Target expected revision {expected}, found {actual}: "
            f"{operation.target_id}"
        )


class ProposalService:
    """Create analysis-backed proposals and apply them as one transaction."""

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        repository_lock: RepositoryLockPort,
        pending_proposals: PendingProposalStorePort,
        view_renderer: ViewRendererPort,
        fact_sync: FactSyncPort,
        source_probe: SourceProbe,
        facts: FactsDatabase,
        replica: GraphReplica | None = None,
        lock_timeout_seconds: float = 10,
    ) -> None:
        self._formal_store = formal_store
        self._repository_lock = repository_lock
        self._pending = pending_proposals
        self._view_renderer = view_renderer
        self._fact_sync = fact_sync
        self._source_probe = source_probe
        self._facts = facts
        self._replica = replica
        self._lock_timeout_seconds = lock_timeout_seconds

    @traced("proposal.create")
    def create_proposal_from_analysis(
        self,
        report: AnalysisReport,
        reason: str,
        *,
        proposal_id: str | None = None,
        created_at: str | None = None,
    ) -> Proposal:
        """Persist one aggregate pending proposal for a validated report.

        Facts are re-synchronized and the report's graph revision and source
        digest are re-checked before anything is stored; the proposal then
        becomes the only temporary work state and formal state is untouched.
        """
        self._recover_interrupted_transaction()
        sync = self._fact_sync.sync("auto")
        with self._repository_lock.acquire("exclusive", self._lock_timeout_seconds):
            self._formal_store.recover()
            state = self._formal_store.load()
            if report.base_graph_revision != state.graph.graph_revision:
                raise CodeCortexError(
                    ErrorCode.GRAPH_REVISION_CONFLICT,
                    "Analysis report base graph revision no longer matches the "
                    "current formal graph",
                    details={
                        "base_graph_revision": report.base_graph_revision,
                        "current_graph_revision": state.graph.graph_revision,
                    },
                    suggested_action="Re-run the analysis against the current graph",
                )
            snapshot = self._source_probe()
            if snapshot.repository_source_digest != sync.repository_source_digest:
                raise _stale_proposal(
                    "Repository source changed during proposal creation"
                )
            if report.analyzed_source_digest != snapshot.repository_source_digest:
                raise _stale_proposal(
                    "Analysis report source digest no longer matches the repository"
                )
            operations = operations_from_report(
                report,
                state.graph,
                cognition_initialized=state.manifest.cognition_initialized,
            )
            preview_event_id = new_id(IdPrefix.EVENT)
            preview_revision = state.graph.graph_revision + 1
            preview_graph = apply_operations_to_graph(
                state.graph, operations, preview_event_id, preview_revision
            )
            preview_refs = recompute_entity_refs(
                preview_graph,
                preview_revision,
                facts=self._facts,
                previous=state.entity_refs,
            )
            preview_state = replace(
                state,
                graph=preview_graph,
                entity_refs=preview_refs,
            )
            violations = graph_model.validate_cognitive_graph(
                typed_graph_from_formal(preview_state)
            )
            if violations:
                raise CodeCortexError(
                    ErrorCode.ANALYSIS_REPORT_INVALID,
                    "Analysis report operations do not produce a valid cognitive graph",
                    details={
                        "issues": [
                            f"{violation.code} at {violation.location}: "
                            f"{violation.message}"
                            for violation in violations
                        ]
                    },
                    suggested_action="Correct the report operations and rerun analysis",
                )
            proposal = Proposal.create(
                proposal_id=proposal_id or new_id(IdPrefix.PROPOSAL),
                base_graph_revision=report.base_graph_revision,
                analyzed_source_digest=report.analyzed_source_digest,
                source_preconditions=_source_preconditions(snapshot),
                operations=operations,
                affected_nodes=affected_nodes_from_report(report, state.graph),
                reason=reason,
                evidence=tuple(
                    _evidence_value(evidence) for evidence in report.evidence
                ),
                uncertainties=tuple(report.uncertainties),
                created_at=(
                    _utc_now_rfc3339() if created_at is None else created_at
                ),
            )
            self._pending.create(proposal)
            return proposal

    @traced("proposal.apply", result=lambda value: {"graph_revision": value.graph_revision, "cache_warning_count": len(value.cache_warnings)})
    def apply_cognitive_proposal(
        self, proposal_id: str, approval: ApprovalRecord
    ) -> GraphApplyResult:
        """Commit one approved proposal as a single recoverable transaction.

        The apply follows design section 15: exclusive lock, recovery,
        approval/revision/source re-verification, in-memory application, full
        formal validation, journaled commit, then disposable-cache refresh.
        Any source change since proposal creation fails closed as
        ``PROPOSAL_STALE`` without writing a single byte.
        """
        self._recover_interrupted_transaction()
        self._fact_sync.sync("auto")
        with self._repository_lock.acquire("exclusive", self._lock_timeout_seconds):
            self._formal_store.recover()
            state = self._formal_store.load()
            if state.view_manifest is None:
                self._formal_store.verify_legacy_views(render_legacy_views(state.graph))
            proposal = self._pending.load(proposal_id)
            proposal.verify_approval(approval)
            proposal.verify_base_graph_revision(state.graph.graph_revision)
            snapshot = self._source_probe()
            verified = proposal.analyzed_source_digest is not None
            if verified and (
                snapshot.repository_source_digest
                != proposal.analyzed_source_digest
            ):
                raise _stale_proposal(
                    "Repository source digest changed since proposal creation"
                )
            _verify_source_preconditions(proposal, snapshot)
            event_id = new_id(IdPrefix.EVENT)
            new_revision = state.graph.graph_revision + 1
            graph = apply_operations_to_graph(
                state.graph, proposal.operations, event_id, new_revision
            )
            entity_refs = recompute_entity_refs(
                graph, new_revision, facts=self._facts, previous=state.entity_refs
            )
            if verified:
                baseline = SourceBaseline(
                    SCHEMA_VERSION,
                    DIGEST_PROFILE_VERSION,
                    MANAGED_SOURCE_SET_VERSION,
                    snapshot.repository_source_digest,
                    tuple(
                        {"relative_path": path, "content_digest": digest}
                        for path, digest in sorted(snapshot.file_digests.items())
                    ),
                )
                manifest = replace(
                    state.manifest,
                    graph_revision=new_revision,
                    cognition_initialized=True,
                    cognition_baseline=snapshot.repository_source_digest,
                )
            else:
                baseline = state.source_baseline
                manifest = replace(state.manifest, graph_revision=new_revision)
            applied = FormalState(
                manifest=manifest,
                graph=graph,
                entity_refs=entity_refs,
                source_baseline=baseline,
                history_events=(
                    *state.history_events,
                    HistoryEventRef(event_id, "cognitive_proposal_applied"),
                ),
            )
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
            views = self._view_renderer(applied.graph)
            applied = replace(
                applied,
                view_manifest=view_manifest_for(new_revision, views),
            )
            event = _applied_event(
                proposal, approval, event_id, state, new_revision, snapshot, verified
            )
            self._formal_store.commit(applied, event, views)
            self._pending.delete(proposal_id)
            warnings = self._refresh_caches(
                applied,
                baseline_digest=(
                    snapshot.repository_source_digest if verified else None
                ),
            )
            return GraphApplyResult(
                event_id=event_id,
                graph_revision=new_revision,
                applied_proposal_id=proposal.proposal_id,
                cache_warnings=warnings,
            )

    def _recover_interrupted_transaction(self) -> None:
        """Resolve any interrupted transaction before Fact Sync reads state.

        Fact Sync loads the formal revision, which fails closed on a crashed
        commit; recovery must therefore run first under its own exclusive
        lock (the lock is not re-entrant), and runs again inside the main
        critical section to close the race with a concurrent writer.
        """
        with self._repository_lock.acquire("exclusive", self._lock_timeout_seconds):
            self._formal_store.recover()

    @traced("proposal.cache_refresh", level="DEBUG", result=lambda value: {"cache_warning_count": len(value)})
    def _refresh_caches(
        self, applied: FormalState, *, baseline_digest: str | None
    ) -> tuple[str, ...]:
        """Refresh disposable caches after the formal commit succeeded.

        Formal truth is already durable; every failure here degrades to a
        rebuild-required warning instead of rolling back the commit.
        """
        warnings: list[str] = []
        if self._replica is not None:
            try:
                self._replica.rebuild(
                    typed_graph_from_formal(applied), applied.graph.graph_revision
                )
            except (CodeCortexError, OSError, sqlite3.Error, ValueError) as error:
                warnings.append(
                    f"{ErrorCode.CACHE_REBUILD_REQUIRED.value}: cognitive "
                    f"replica refresh failed after the formal commit: {error}"
                )
        try:
            if baseline_digest is not None:
                self._facts.replace_baseline_entity_snapshots(baseline_digest)
            self._facts.advance_graph_revision(applied.graph.graph_revision)
        except (CodeCortexError, OSError, sqlite3.Error, ValueError) as error:
            warnings.append(
                f"{ErrorCode.CACHE_REBUILD_REQUIRED.value}: fact cache refresh "
                f"failed after the formal commit: {error}"
            )
        return tuple(warnings)


def _source_preconditions(
    snapshot: ManagedSourceSnapshot,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {"relative_path": path, "content_digest": digest}
        for path, digest in sorted(snapshot.file_digests.items())
    )


def _verify_source_preconditions(
    proposal: Proposal, snapshot: ManagedSourceSnapshot
) -> None:
    """Re-verify every fine-grained precondition against current disk digests."""
    for entry in proposal.source_preconditions:
        if not isinstance(entry, Mapping) or set(entry) != {
            "relative_path",
            "content_digest",
        }:
            raise _stale_proposal("Source precondition has an unverifiable shape")
        relative = entry["relative_path"]
        expected = entry["content_digest"]
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise _stale_proposal("Source precondition is not verifiable")
        actual = snapshot.file_digests.get(relative)
        if actual is None:
            raise _stale_proposal(
                f"Managed source file no longer exists: {relative}"
            )
        if actual != expected:
            raise _stale_proposal(
                f"Managed source file changed since proposal creation: {relative}"
            )


def _patched_record(
    operation: PatchOperation,
    provenance: dict[str, str],
    new_revision: int,
    *,
    revision_key: str = "node_revision",
) -> dict[str, object]:
    value = json_value_to_mutable(operation.value)
    if not isinstance(value, dict):
        raise _invalid_patch(
            f"Patch value must be an object: {operation.target_id}"
        )
    record = dict(value)
    record["approval"] = dict(provenance)
    record[revision_key] = new_revision
    return record


def _patched_flow(
    operation: PatchOperation,
    provenance: dict[str, str],
    new_revision: int,
) -> dict[str, object]:
    record = _patched_record(
        operation, provenance, new_revision, revision_key="flow_revision"
    )
    steps = record.get("steps")
    if isinstance(steps, list):
        stamped = []
        for step in steps:
            if not isinstance(step, dict):
                raise _invalid_patch(
                    f"Flow steps must be objects: {operation.target_id}"
                )
            stamped_step = dict(step)
            stamped_step["approval"] = dict(provenance)
            stamped.append(stamped_step)
        record["steps"] = stamped
    return record


def _require_explicit_node_removals(
    removed_nodes: list[str],
    edges: dict[str, dict[str, object]],
    flows: dict[str, dict[str, object]],
    mappings: dict[str, dict[str, object]],
) -> None:
    """Reject invisible cascades: references must be handled by the patch."""
    for node_id in removed_nodes:
        offenders: list[str] = []
        for edge_id, edge in edges.items():
            if node_id in (edge.get("source_id"), edge.get("target_id")):
                offenders.append(f"edge {edge_id}")
        for behavior_id, flow in flows.items():
            if behavior_id == node_id:
                offenders.append(f"flow {behavior_id}")
                continue
            steps = flow.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict) and node_id in (
                        step.get("uses_capabilities") or ()
                    ):
                        offenders.append(f"flow step {step.get('id')}")
        for mapping_id, mapping in mappings.items():
            if mapping.get("subject_id") == node_id:
                offenders.append(f"mapping {mapping_id}")
        if offenders:
            raise _invalid_patch(
                f"remove_node {node_id} requires explicit handling of its "
                f"references: {', '.join(sorted(offenders))}"
            )


def _evidence_value(evidence: graph_model.Evidence) -> dict[str, object]:
    return {
        "id": evidence.id,
        "kind": evidence.kind,
        "entity_uid": evidence.entity_uid,
        "relative_path": evidence.relative_path,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "observation": evidence.observation,
    }


def _evidence_list(
    items: tuple[graph_model.Evidence, ...],
) -> list[dict[str, object]]:
    return [_evidence_value(evidence) for evidence in items]


def _node_value(node: graph_model.SemanticNode) -> dict[str, object]:
    return {
        "id": node.id,
        "kind": node.kind.value,
        "title": node.title,
        "aliases": list(node.aliases),
        "summary": node.summary,
        "epistemic_status": str(node.epistemic_status),
        "intent": node.intent,
        "observed": node.observed,
        "evidence": _evidence_list(node.evidence),
        "created_by": str(node.created_by),
        "last_modified_by": str(node.last_modified_by),
    }


def _edge_value(edge: graph_model.CognitiveEdge) -> dict[str, object]:
    return {
        "id": edge.id,
        "type": str(edge.type),
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "epistemic_status": str(edge.epistemic_status),
        "evidence": _evidence_list(edge.evidence),
    }


def _flow_value(flow: graph_model.LogicalFlow) -> dict[str, object]:
    return {
        "behavior_id": flow.behavior_id,
        "materialization_status": str(flow.materialization_status),
        "steps": [
            {
                "id": step.id,
                "order": step.order,
                "title": step.title,
                "summary": step.summary,
                "uses_capabilities": list(step.uses_capabilities),
                "evidence": _evidence_list(step.evidence),
            }
            for step in flow.steps
        ],
        "evidence": _evidence_list(flow.evidence),
    }


def _mapping_value(mapping: graph_model.ImplementationMapping) -> dict[str, object]:
    return {
        "id": mapping.id,
        "subject_kind": str(mapping.subject_kind),
        "subject_id": mapping.subject_id,
        "entity_uid": mapping.entity_uid,
        "role": str(mapping.role),
        "resolution_status": str(mapping.resolution_status),
        "evidence_note": mapping.evidence_note,
        "evidence": _evidence_list(mapping.evidence),
    }


def typed_graph_from_formal(state: FormalState) -> graph_model.CognitiveGraph:
    """Convert the validated formal persistence shape into the typed model."""
    graph = state.graph
    return graph_model.CognitiveGraph(
        graph_revision=graph.graph_revision,
        nodes=tuple(_typed_node(node) for node in graph.nodes),
        semantic_edges=tuple(
            _typed_edge(edge) for edge in graph.semantic_edges
        ),
        logical_flows=tuple(_typed_flow(flow) for flow in graph.logical_flows),
        implementation_mappings=tuple(
            _typed_mapping(mapping) for mapping in graph.implementation_mappings
        ),
        entity_uids=frozenset(
            str(entry.get("uid")) for entry in state.entity_refs.entities
        ),
    )


def _typed_approval(record: object) -> graph_model.Approval | None:
    if not isinstance(record, Mapping):
        return None
    event_id = record.get("approval_event_id")
    if not isinstance(event_id, str):
        return None
    approved_by = record.get("approved_by")
    approved_at = record.get("approved_at")
    return graph_model.Approval(
        approval_event_id=event_id,
        approved_by=approved_by if isinstance(approved_by, str) else "user",
        approved_at=approved_at if isinstance(approved_at, str) else None,
    )


def _typed_evidence(record: object) -> graph_model.Evidence:
    data = record if isinstance(record, Mapping) else {}
    return graph_model.Evidence(
        id=str(data.get("id")),
        kind=str(data.get("kind")),
        entity_uid=_optional_text(data.get("entity_uid")),
        relative_path=_optional_text(data.get("relative_path")),
        start_line=_optional_int(data.get("start_line")),
        end_line=_optional_int(data.get("end_line")),
        observation=_optional_text(data.get("observation")),
    )


def _typed_evidence_list(record: object) -> tuple[graph_model.Evidence, ...]:
    if not isinstance(record, (list, tuple)):
        return ()
    return tuple(_typed_evidence(item) for item in record)


def _typed_node(record: Mapping[str, object]) -> graph_model.SemanticNode:
    node_types: dict[str, type[graph_model.SemanticNode]] = {
        "responsibility": graph_model.Responsibility,
        "behavior": graph_model.Behavior,
        "capability": graph_model.Capability,
    }
    node_type = node_types[str(record.get("kind"))]
    aliases = record.get("aliases")
    return node_type(
        id=str(record.get("id")),
        title=str(record.get("title")),
        aliases=(
            tuple(str(alias) for alias in aliases)
            if isinstance(aliases, (list, tuple))
            else ()
        ),
        summary=_optional_text(record.get("summary")),
        epistemic_status=str(record.get("epistemic_status") or "inferred"),
        intent=_optional_text(record.get("intent")),
        observed=_optional_text(record.get("observed")),
        evidence=_typed_evidence_list(record.get("evidence")),
        created_by=str(record.get("created_by") or "analyzer"),
        last_modified_by=str(record.get("last_modified_by") or "analyzer"),
        node_revision=_optional_int(record.get("node_revision")) or 1,
        approval=_typed_approval(record.get("approval")),
    )


def _typed_edge(record: Mapping[str, object]) -> graph_model.CognitiveEdge:
    return graph_model.CognitiveEdge(
        id=str(record.get("id")),
        type=str(record.get("type")),
        source_id=str(record.get("source_id")),
        target_id=str(record.get("target_id")),
        epistemic_status=str(record.get("epistemic_status") or "inferred"),
        evidence=_typed_evidence_list(record.get("evidence")),
        edge_revision=_optional_int(record.get("edge_revision")) or 1,
        approval=_typed_approval(record.get("approval")),
    )


def _typed_flow(record: Mapping[str, object]) -> graph_model.LogicalFlow:
    steps = record.get("steps")
    typed_steps = []
    if isinstance(steps, (list, tuple)):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            capabilities = step.get("uses_capabilities")
            typed_steps.append(
                graph_model.FlowStep(
                    id=str(step.get("id")),
                    order=_optional_int(step.get("order")) or 0,
                    title=str(step.get("title")),
                    summary=_optional_text(step.get("summary")),
                    uses_capabilities=(
                        tuple(str(item) for item in capabilities)
                        if isinstance(capabilities, (list, tuple))
                        else ()
                    ),
                    evidence=_typed_evidence_list(step.get("evidence")),
                    approval=_typed_approval(step.get("approval")),
                )
            )
    return graph_model.LogicalFlow(
        behavior_id=str(record.get("behavior_id")),
        materialization_status=str(record.get("materialization_status")),
        flow_revision=_optional_int(record.get("flow_revision")) or 1,
        steps=tuple(typed_steps),
        evidence=_typed_evidence_list(record.get("evidence")),
        approval=_typed_approval(record.get("approval")),
    )


def _typed_mapping(
    record: Mapping[str, object],
) -> graph_model.ImplementationMapping:
    return graph_model.ImplementationMapping(
        id=str(record.get("id")),
        subject_kind=str(record.get("subject_kind")),
        subject_id=str(record.get("subject_id")),
        entity_uid=str(record.get("entity_uid")),
        role=str(record.get("role")),
        resolution_status=str(record.get("resolution_status")),
        evidence_note=_optional_text(record.get("evidence_note")),
        evidence=_typed_evidence_list(record.get("evidence")),
        mapping_revision=_optional_int(record.get("mapping_revision")) or 1,
        approval=_typed_approval(record.get("approval")),
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _changed_files(
    previous: SourceBaseline, snapshot: ManagedSourceSnapshot
) -> list[str]:
    old = {
        str(entry.get("relative_path")): str(entry.get("content_digest"))
        for entry in previous.files
    }
    new = dict(snapshot.file_digests)
    return sorted(
        path for path in set(old) | set(new) if old.get(path) != new.get(path)
    )


def _applied_event(
    proposal: Proposal,
    approval: ApprovalRecord,
    event_id: str,
    previous_state: FormalState,
    new_revision: int,
    snapshot: ManagedSourceSnapshot,
    verified: bool,
) -> dict[str, object]:
    """Build the self-contained immutable audit event for one M1a apply."""
    before_digest = previous_state.manifest.cognition_baseline
    after_digest = snapshot.repository_source_digest if verified else before_digest
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
            "before_source_digest": before_digest,
            "after_source_digest": after_digest,
            "changed_files": (
                _changed_files(previous_state.source_baseline, snapshot)
                if verified
                else []
            ),
            "changed_entities": [],
            "affected_nodes": list(proposal.affected_nodes),
            "scope_confidence": "complete" if verified else "partial",
            "unmapped_changes": [],
        },
        "applied_at": _utc_now_rfc3339(),
    }


def _proposal_snapshot(proposal: Proposal) -> dict[str, object]:
    """Serialize the complete applied proposal into the formal event.

    This mirrors the M0 audit shape in ``application.services``; later
    milestones may add fields but must not omit any of them.
    """
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


def _stale_proposal(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PROPOSAL_STALE,
        message,
        suggested_action="Revise or recreate the proposal",
    )


def _invalid_patch(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.ANALYSIS_REPORT_INVALID,
        message,
        suggested_action="Revise or recreate the proposal",
    )


def _utc_now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
