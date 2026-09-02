"""Application use cases coordinating formal state and repository locking."""

from dataclasses import dataclass
from datetime import UTC, datetime

from codecortex.application.ports import (
    FormalStorePort,
    PendingProposalStorePort,
    RepositoryContextPort,
    RepositoryLockPort,
)
from codecortex.domain.cognition import (
    FormalState,
    Manifest,
    SourceBaseline,
    ValidationResult,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, new_id
from codecortex.domain.proposals import PatchOperation, Proposal


@dataclass(frozen=True)
class RepositoryOverview:
    """Bounded adapter-facing summary of one formal repository."""

    repository_root: str
    graph_revision: int
    cognition_initialized: bool
    cognition_baseline_source_digest: str | None
    formal_files: dict[str, bool]


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
