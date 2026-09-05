"""The Analyzer dispatch freshness chain for M1a initialization.

Design sections 10-11: Main records the source digest and graph revision
before dispatching the Analyzer, then re-synchronizes facts and re-reads the
formal revision before consuming the returned report.  Any source or graph
change in between rejects the whole report as stale; M1a deliberately does
not rebase an old report onto a changed snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codecortex.application.ports import (
    FactSyncPort,
    FormalStorePort,
    RepositoryLockPort,
)
from codecortex.domain.analysis import (
    DEFAULT_ANALYSIS_LIMITS,
    AnalysisLimits,
    AnalysisReport,
    validate_analysis_report,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import Proposal

_MAX_COORDINATE_RETRIES = 2


class AnalysisProposalPort(Protocol):
    """The narrow write boundary used after a report has been validated."""

    def create_proposal_from_analysis(
        self, report: AnalysisReport, reason: str
    ) -> Proposal:
        """Persist exactly one aggregate pending Proposal from ``report``."""


@dataclass(frozen=True, slots=True)
class AnalysisCoordinate:
    """The pinned source digest and graph revision handed to the Analyzer."""

    graph_revision: int
    source_digest: str


class InitializationService:
    """Coordinate the freshness checks around one Analyzer dispatch."""

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        fact_sync: FactSyncPort,
        repository_lock: RepositoryLockPort,
        proposal_service: AnalysisProposalPort | None = None,
        lock_timeout_seconds: float = 10,
        limits: AnalysisLimits = DEFAULT_ANALYSIS_LIMITS,
    ) -> None:
        self._formal_store = formal_store
        self._fact_sync = fact_sync
        self._repository_lock = repository_lock
        self._proposal_service = proposal_service
        self._lock_timeout_seconds = lock_timeout_seconds
        self._limits = limits

    def begin_analysis(self) -> AnalysisCoordinate:
        """Re-synchronize facts and pin the coordinate the Analyzer must use."""
        return self._current_coordinate()

    def consume_report(self, payload: bytes) -> AnalysisReport:
        """Re-verify freshness, then strictly validate one Analyzer report."""
        coordinate = self._current_coordinate()
        return validate_analysis_report(
            payload,
            coordinate.graph_revision,
            coordinate.source_digest,
            limits=self._limits,
        )

    def create_aggregate_proposal(self, payload: bytes, reason: str) -> Proposal:
        """Validate one Analyzer payload then persist one aggregate Proposal.

        Codex owns Analyzer dispatch and the user discussion.  Core owns this
        narrow hand-off so an Analyzer payload cannot bypass freshness or the
        strict report schema on its way to formal state.  The proposal service
        performs one final Fact Sync/source probe before it writes the pending
        proposal, so source changes between validation and persistence fail
        closed.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Proposal reason must be non-empty")
        if self._proposal_service is None:
            raise RuntimeError("Analysis-backed proposal service is not configured")
        report = self.consume_report(payload)
        return self._proposal_service.create_proposal_from_analysis(
            report, reason.strip()
        )

    def _current_coordinate(self) -> AnalysisCoordinate:
        # Fact Sync takes the exclusive lock internally and returns the digest
        # of the snapshot it actually committed.  The formal revision is then
        # read under a shared lock; the Main process is the only writer.
        for _attempt in range(_MAX_COORDINATE_RETRIES + 1):
            result = self._fact_sync.sync("auto")
            with self._repository_lock.acquire("shared", self._lock_timeout_seconds):
                revision = self._formal_store.load().graph.graph_revision
                live_source_digest = self._fact_sync.probe_source_digest()
                if (
                    revision == result.graph_revision
                    and live_source_digest == result.repository_source_digest
                ):
                    return AnalysisCoordinate(
                        graph_revision=revision,
                        source_digest=result.repository_source_digest,
                    )
        raise CodeCortexError(
            ErrorCode.PROPOSAL_STALE,
            "Managed source or formal graph kept changing during analysis preparation",
            retryable=True,
            suggested_action="Retry analysis against a stable repository snapshot",
        )
