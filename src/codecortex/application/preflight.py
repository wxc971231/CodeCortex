"""Mandatory deterministic reconciliation before explicit CodeCortex work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codecortex.application.affected_scope import AffectedScopeCalculator
from codecortex.application.change_detection import ChangeDetector
from codecortex.application.fact_sync import FactSyncResult, FactSyncService
from codecortex.application.freshness import (
    FreshnessService,
    RepositoryFreshnessSummary,
)
from codecortex.application.ports import FormalStorePort, RepositoryLockPort
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.freshness import ChangeSet
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.freshness import FreshnessStore

if TYPE_CHECKING:
    from codecortex.application.recovery import RecoveryService


@dataclass(frozen=True)
class PreflightResult:
    """One consistent deterministic fact/cache view for an explicit operation."""

    fact_sync: FactSyncResult
    change_set: ChangeSet | None
    repository_status: RepositoryFreshnessSummary


class PreflightService:
    """Recover cache/formal readiness, then reconcile facts and one ChangeSet.

    This service deliberately has no Agent port and no graph-write operation.
    FactSync owns the expensive snapshot/retry loop; the final exclusive lock
    keeps formal baseline reading, scope calculation, and local freshness
    pointer replacement from mixing different formal graph revisions.
    """

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        fact_sync: FactSyncService,
        facts: FactsDatabase,
        freshness_store: FreshnessStore,
        repository_lock: RepositoryLockPort,
        recovery_service: RecoveryService | None = None,
        lock_timeout_seconds: float = 10,
        max_retries: int = 2,
    ) -> None:
        if type(lock_timeout_seconds) not in (int, float) or lock_timeout_seconds < 0:
            raise ValueError("Preflight lock timeout must be non-negative")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("Preflight retry count must be non-negative")
        self.formal_store = formal_store
        self.fact_sync = fact_sync
        self.facts = facts
        self.freshness_store = freshness_store
        self.repository_lock = repository_lock
        self.recovery_service = recovery_service
        self.lock_timeout_seconds = lock_timeout_seconds
        self.max_retries = max_retries

    def run(self) -> PreflightResult:
        """Return current facts plus the one baseline-to-current effective ChangeSet."""
        if self.recovery_service is not None and self.recovery_service.requires_recovery():
            recovered = self.recovery_service.ensure_cache()
            return PreflightResult(
                fact_sync=recovered.fact_sync,
                change_set=recovered.change_set,
                repository_status=recovered.repository_status,
            )
        self._recover_and_require_initialized()
        for attempt in range(self.max_retries + 1):
            synced = self.fact_sync.sync("auto")
            with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
                state = self.formal_store.load()
                self._require_initialized(state.manifest.cognition_initialized)
                metadata = self.facts.cache_metadata()
                live_source_digest = self.fact_sync.probe_source_digest()
                if (
                    synced.graph_revision != state.manifest.graph_revision
                    or metadata.graph_revision != state.manifest.graph_revision
                    or metadata.repository_source_digest
                    != synced.repository_source_digest
                    or live_source_digest != synced.repository_source_digest
                ):
                    if attempt == self.max_retries:
                        raise CodeCortexError(
                            ErrorCode.CACHE_REBUILD_REQUIRED,
                            "Formal state or managed source changed while Fact Preflight was reconciling facts",
                            retryable=True,
                            suggested_action="Retry the CodeCortex operation",
                        )
                    continue
                detected = ChangeDetector().detect(state, self.facts)
                change_set = self._stable_effective_change_set(detected)
                self.freshness_store.replace_effective(change_set)
                freshness = FreshnessService(change_set)
                return PreflightResult(
                    fact_sync=synced,
                    change_set=change_set,
                    repository_status=freshness.repository_status(),
                )
        raise AssertionError("Preflight retry loop unexpectedly exhausted")

    def _recover_and_require_initialized(self) -> None:
        """Resolve interrupted formal writes before any FactSync reads their revision."""
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            self.formal_store.recover()
            state = self.formal_store.load()
            self._require_initialized(state.manifest.cognition_initialized)

    @staticmethod
    def _require_initialized(cognition_initialized: bool) -> None:
        if not cognition_initialized:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "M1b Fact Preflight requires initialized repository cognition",
            )

    def _stable_effective_change_set(
        self, detected: ChangeSet | None
    ) -> ChangeSet | None:
        """Preserve identity when unchanged baseline/current inputs are rechecked."""
        if detected is None:
            return None
        try:
            existing = self.freshness_store.load_effective()
        except (CodeCortexError, OSError, TypeError, ValueError):
            # Freshness cache is disposable.  The new atomic replacement below
            # makes its current pointer authoritative again.
            self.freshness_store.freshness_path.unlink(missing_ok=True)
            existing = None
        reusable = (
            existing is not None
            and existing.baseline_source_digest == detected.baseline_source_digest
            and existing.current_source_digest == detected.current_source_digest
        )
        scope = AffectedScopeCalculator(
            self.formal_store.load(), self.facts
        ).calculate(detected)
        if reusable:
            assert existing is not None
            # Keep the original ChangeSet identity for idempotence, while
            # recomputing scope against the currently locked formal graph.
            return existing.with_affected_scope(scope)
        return detected.with_affected_scope(scope)
