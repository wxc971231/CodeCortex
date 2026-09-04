"""Application use cases coordinating formal state and repository locking."""

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from codecortex.application.fact_sync import FactSyncResult
from codecortex.application.freshness import (
    FreshnessService,
    RepositoryFreshnessSummary,
)
from codecortex.application.ports import (
    FactSyncPort,
    FormalStorePort,
    PendingProposalStorePort,
    RecoveryResult,
    RepositoryContextPort,
    RepositoryLockPort,
    ViewRendererPort,
)
from codecortex.application.proposals import typed_graph_from_formal
from codecortex.application.query import (
    AnalysisScopeResult,
    EntityContextResult,
    NodeInspection,
    QueryService,
    RepositoryFactsPage,
    SearchPage,
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
from codecortex.domain.freshness import ChangeSet
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
from codecortex.infrastructure.persistence.graph_replica import (
    ContextRequest,
    DiscussionContext,
    GraphReplica,
)

if TYPE_CHECKING:
    from codecortex.application.initialize import InitializationService
    from codecortex.application.preflight import PreflightResult, PreflightService
    from codecortex.application.proposals import ProposalService


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
    cache_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreshnessSnapshot:
    """One verified repository freshness view for an MCP caller.

    Main obtains this by running deterministic Fact Preflight.  Analyzer may
    only read the already prepared cache/formal coordinate, so its sandbox
    never receives a cache-writing synchronization path.
    """

    baseline_source_digest: str
    current_source_digest: str
    change_set: ChangeSet | None
    repository_status: RepositoryFreshnessSummary


@dataclass
class ApplicationServices:
    """Coordinate use cases while keeping policy out of CLI and MCP adapters."""

    repository: RepositoryContextPort
    formal_store: FormalStorePort
    repository_lock: RepositoryLockPort
    lock_timeout_seconds: float = 10
    pending_proposals: PendingProposalStorePort | None = None
    view_renderer: ViewRendererPort | None = None
    fact_sync: FactSyncPort | None = None
    query_service: QueryService | None = None
    initialization_service: InitializationService | None = None
    m1a_proposal_service: ProposalService | None = None
    preflight_service: PreflightService | None = None
    cognitive_replica: GraphReplica | None = None
    cognitive_graph_max_objects: int = 500

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
            self._refresh_cognitive_replica(state)
            return self._overview(state)

    def _refresh_cognitive_replica(self, state: FormalState) -> None:
        """(Re)build the disposable cognitive replica from the formal state.

        Initialization is a Main-only write path, so this is where a fresh or
        outdated replica becomes readable; Analyzer reads never write caches.
        """
        if self.cognitive_replica is not None:
            self.cognitive_replica.rebuild(
                typed_graph_from_formal(state), state.graph.graph_revision
            )

    def recover_formal_state(self) -> RecoveryResult:
        """Recover an interrupted transaction before a Main entry serves state.

        Recovery may replace or remove files, so it is deliberately an explicit
        Main-process preparation step instead of an implicit action on every
        read. Analyzer processes remain read-only: they either see a state
        prepared by Main or fail closed on an interrupted transaction.
        """
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            return self.formal_store.recover()

    def validate_graph(self) -> ValidationResult:
        """Load and validate the complete formal state under a shared lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            self.formal_store.load()
        return ValidationResult(valid=True, issues=())

    def synchronize_facts(self, mode: str = "auto") -> FactSyncResult:
        """Refresh only disposable code facts; formal baseline and graph stay untouched."""
        if mode not in ("auto", "full"):
            raise ValueError("Fact Sync mode must be 'auto' or 'full'")
        if self.fact_sync is None:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "Fact synchronization is not configured",
            )
        return self.fact_sync.sync(cast(Literal["auto", "full"], mode))

    def run_preflight(self) -> PreflightResult:
        """Run the single mandatory M1b readiness gate for an explicit operation."""
        if self.preflight_service is None:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "Fact Preflight is not configured",
            )
        return self.preflight_service.run()

    def freshness_snapshot(self, *, preflight: bool) -> FreshnessSnapshot:
        """Return freshness after Main preparation or from Analyzer's read view.

        A read-only Analyzer is intentionally unable to repair stale cache
        state.  It must fail closed and ask Main to run the deterministic gate.
        """
        if preflight:
            result = self.run_preflight()
            change_set = result.change_set
            return FreshnessSnapshot(
                baseline_source_digest=(
                    result.fact_sync.repository_source_digest
                    if change_set is None
                    else change_set.baseline_source_digest
                ),
                current_source_digest=result.fact_sync.repository_source_digest,
                change_set=change_set,
                repository_status=result.repository_status,
            )
        return self._prepared_freshness_snapshot()

    def _prepared_freshness_snapshot(self) -> FreshnessSnapshot:
        """Read a Main-prepared freshness coordinate without modifying cache."""
        preflight_service = self.preflight_service
        if preflight_service is None:
            raise CodeCortexError(
                ErrorCode.CACHE_REBUILD_REQUIRED,
                "Read-only freshness is unavailable until Main runs Fact Preflight",
                suggested_action="Ask Main CodeCortex to run cognitive_freshness first",
            )
        try:
            with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
                state = self.formal_store.load()
                if not state.manifest.cognition_initialized:
                    raise CodeCortexError(
                        ErrorCode.NOT_INITIALIZED,
                        "M1b freshness requires initialized repository cognition",
                    )
                baseline = state.manifest.cognition_baseline
                if baseline is None:
                    raise CodeCortexError(
                        ErrorCode.FORMAL_STATE_CORRUPT,
                        "Initialized cognition has no formal source baseline",
                    )
                metadata = preflight_service.facts.cache_metadata()
                if metadata.graph_revision != state.manifest.graph_revision:
                    raise _freshness_cache_error("Fact cache graph revision is stale")
                if (
                    metadata.digest_profile_version
                    != state.manifest.digest_profile_version
                    or metadata.managed_source_set_version
                    != state.manifest.managed_source_set_version
                ):
                    raise _freshness_cache_error(
                        "Fact cache digest profile does not match formal source baseline"
                    )
                change_set = preflight_service.freshness_store.load_effective()
                current = metadata.repository_source_digest
                if current == baseline:
                    if change_set is not None:
                        raise _freshness_cache_error(
                            "Fresh cache has an unexpected effective ChangeSet"
                        )
                elif (
                    change_set is None
                    or change_set.baseline_source_digest != baseline
                    or change_set.current_source_digest != current
                ):
                    raise _freshness_cache_error(
                        "Fact cache and effective ChangeSet do not describe one source coordinate"
                    )
                summary = FreshnessService(change_set).repository_status()
                return FreshnessSnapshot(
                    baseline_source_digest=baseline,
                    current_source_digest=current,
                    change_set=change_set,
                    repository_status=summary,
                )
        except CodeCortexError:
            raise
        except (OSError, ValueError) as error:
            raise _freshness_cache_error("Prepared freshness cache is unreadable") from error

    def repository_facts(
        self,
        scope: str,
        cursor: str | None = None,
        limit: int = 50,
        *,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> RepositoryFactsPage:
        """Return one guarded, cursor-paginated page of module entities."""
        return self._query().repository_facts(
            scope,
            cursor,
            limit,
            expected_source_digest=expected_source_digest,
            expected_graph_revision=expected_graph_revision,
        )

    def analysis_scope(
        self,
        scope: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        *,
        diagnostics_limit: int = 50,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> AnalysisScopeResult:
        """Return guarded package/module partitions, totals, and diagnostics."""
        return self._query().analysis_scope(
            scope,
            cursor,
            limit,
            diagnostics_limit=diagnostics_limit,
            expected_source_digest=expected_source_digest,
            expected_graph_revision=expected_graph_revision,
        )

    def resolve_entity_context(
        self,
        *,
        entity_uid: str | None = None,
        path: str | None = None,
        address: str | None = None,
        relation_types: tuple[str, ...] = (),
        cursor: str | None = None,
        limit: int = 50,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> EntityContextResult:
        """Resolve exactly one anchor to entities, mappings, and relations."""
        return self._query().resolve_entity_context(
            entity_uid=entity_uid,
            path=path,
            address=address,
            relation_types=relation_types,
            cursor=cursor,
            limit=limit,
            expected_source_digest=expected_source_digest,
            expected_graph_revision=expected_graph_revision,
        )

    def get_discussion_context(self, request: ContextRequest) -> DiscussionContext:
        """Return one guarded, bounded discussion-context neighborhood."""
        return self._query().get_discussion_context(request)

    def search_cognitive_graph(
        self,
        query: str,
        kinds: tuple[str, ...] = (),
        limit: int = 20,
        *,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> SearchPage:
        """Return guarded, deterministic, bounded cognitive search hits."""
        return self._query().search_cognitive_graph(
            query,
            kinds,
            limit,
            expected_source_digest=expected_source_digest,
            expected_graph_revision=expected_graph_revision,
        )

    def inspect_node(
        self,
        node_id: str,
        *,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> NodeInspection:
        """Return one M1a node inspection with current-source projections."""
        return self._query().inspect_node(
            node_id,
            expected_source_digest=expected_source_digest,
            expected_graph_revision=expected_graph_revision,
        )

    def _query(self) -> QueryService:
        if self.query_service is None:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "Bounded query services are not configured",
            )
        return self.query_service

    def repository_overview(self) -> RepositoryOverview:
        """Return the current formal-state summary under a shared lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            state = self.formal_store.load()
            return self._overview(state)

    def cognitive_graph(self) -> CognitiveGraph:
        """Load the complete formal graph under a shared repository lock.

        M1a keeps this M0 entry for compatibility diagnostics only: responses
        are capped at ``cognitive_graph_max_objects`` and larger graphs must
        be read through the bounded query APIs instead (detailed design
        section 17).
        """
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            graph = self.formal_store.load().graph
        total = (
            len(graph.nodes)
            + len(graph.semantic_edges)
            + len(graph.logical_flows)
            + len(graph.implementation_mappings)
        )
        if total > self.cognitive_graph_max_objects:
            raise CodeCortexError(
                ErrorCode.CONTEXT_LIMIT_EXCEEDED,
                "cognitive_graph() is compatibility-only and capped at "
                f"{self.cognitive_graph_max_objects} objects; this graph has "
                f"{total}. Use search_cognitive_graph, get_discussion_context, "
                "or the cursor-based repository_facts interface instead",
                suggested_action=(
                    "Use search_cognitive_graph, get_discussion_context, or "
                    "cursor-based repository_facts"
                ),
            )
        return graph

    def history_event(self, event_id: str) -> dict[str, object]:
        """Load one immutable History event under a shared repository lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            return self.formal_store.read_history_event(event_id)

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

    def create_cognitive_proposal_from_analysis(
        self, payload: bytes, reason: str
    ) -> Proposal:
        """Create the one aggregate Proposal produced by an Analyzer report.

        This is deliberately separate from the generic M0 Proposal endpoint:
        only this path accepts Analyzer output, and it always validates report
        bounds/freshness before the M1a proposal service rechecks source state.
        """
        if self.initialization_service is None:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "Analysis-backed initialization is not configured",
            )
        return self.initialization_service.create_aggregate_proposal(payload, reason)

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
        if self.m1a_proposal_service is not None:
            m1a_result = self.m1a_proposal_service.apply_cognitive_proposal(
                proposal_id, approval
            )
            return ApplyResult(
                event_id=m1a_result.event_id,
                graph_revision=m1a_result.graph_revision,
                applied_proposal_id=m1a_result.applied_proposal_id,
                cache_warnings=m1a_result.cache_warnings,
            )
        store = self._pending_store()
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            self.formal_store.recover()
            state = self.formal_store.load()
            proposal = store.load(proposal_id)
            proposal.verify_approval(approval)
            proposal.verify_base_graph_revision(state.graph.graph_revision)
            _verify_source_preconditions(self.repository.root, proposal)
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
            views = self._view_renderer()(applied.graph)
            if state.view_manifest is not None:
                applied = replace(
                    applied,
                    view_manifest=view_manifest_for(new_revision, views),
                )
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

    def _view_renderer(self) -> ViewRendererPort:
        if self.view_renderer is None:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "View rendering is not configured",
            )
        return self.view_renderer

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


_CONTENT_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _stale_proposal(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PROPOSAL_STALE,
        message,
        suggested_action="Revise or recreate the proposal",
    )


def _verify_source_preconditions(root: Path, proposal: Proposal) -> None:
    """Re-verify every source precondition against the current repository.

    M0 can recompute plain SHA-256 file digests but has no Digest Profile
    to recompute a comparable repository digest, so a proposal pinning
    ``analyzed_source_digest`` is unverifiable and fails closed as stale.
    """
    if proposal.analyzed_source_digest is not None:
        raise _stale_proposal(
            "Proposal analyzed source digest cannot be re-verified in M0"
        )
    for entry in proposal.source_preconditions:
        _verify_source_precondition(root, entry)


def _verify_source_precondition(root: Path, entry: Mapping[str, object]) -> None:
    if not isinstance(entry, Mapping) or set(entry) != {
        "relative_path",
        "content_digest",
    }:
        raise _stale_proposal("Source precondition has an unverifiable shape")
    relative = entry["relative_path"]
    expected = entry["content_digest"]
    if not isinstance(relative, str) or not relative:
        raise _stale_proposal("Source precondition path is not verifiable")
    if not isinstance(expected, str) or (
        _CONTENT_DIGEST_PATTERN.fullmatch(expected) is None
    ):
        raise _stale_proposal("Source precondition digest is not verifiable")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise _stale_proposal(
            f"Source precondition path escapes the repository: {relative}"
        )
    resolved = (root / candidate).resolve()
    if os.path.commonpath((str(root), str(resolved))) != str(root):
        raise _stale_proposal(
            f"Source precondition path escapes the repository: {relative}"
        )
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise _stale_proposal(
            f"Source precondition file is unreadable: {relative}"
        ) from error
    actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual != expected:
        raise _stale_proposal(
            f"Source precondition file changed since proposal creation: {relative}"
        )


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
    value = json_value_to_mutable(operation.value)
    if not isinstance(value, dict):
        raise _invalid_patch(
            f"Patch value must be an object: {operation.target_id}"
        )
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


def _freshness_cache_error(message: str) -> CodeCortexError:
    """Return the single fail-closed error for an Analyzer cache mismatch."""
    return CodeCortexError(
        ErrorCode.CACHE_REBUILD_REQUIRED,
        message,
        retryable=True,
        suggested_action="Ask Main CodeCortex to run Fact Preflight and retry",
    )
