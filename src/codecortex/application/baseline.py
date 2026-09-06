"""Audited advancement of cognition baseline without changing the graph."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.ports import FormalStorePort, RepositoryLockPort
from codecortex.application.preflight import PreflightResult, PreflightService
from codecortex.domain.cognition import (
    DIGEST_PROFILE_VERSION,
    MANAGED_SOURCE_SET_VERSION,
    SCHEMA_VERSION,
    FormalState,
    HistoryEventRef,
    SourceBaseline,
    validate_formal_state,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.facts import DigestProfile
from codecortex.domain.freshness import ChangeSet
from codecortex.domain.ids import IdPrefix, new_id, validate_id
from codecortex.infrastructure.persistence.entity_refs import recompute_entity_refs
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.python.digest import repository_digest_from_file_digests

BaselineAdvanceReason = Literal["no_semantic_change", "user_accepted"]


@dataclass(frozen=True)
class DecisionRecord:
    """The bounded Main/Analyzer conclusion that no graph patch is needed."""

    decided_by: Literal["analyzer", "main_codex"]
    evidence_summary: str
    decided_at: str

    def __post_init__(self) -> None:
        if self.decided_by not in ("analyzer", "main_codex"):
            raise ValueError("Decision actor must be analyzer or main_codex")
        if not isinstance(self.evidence_summary, str) or not self.evidence_summary.strip():
            raise ValueError("Decision evidence summary must be non-empty")
        if len(self.evidence_summary) > 4_000:
            raise ValueError("Decision evidence summary exceeds 4000 characters")
        _require_utc_timestamp(self.decided_at, "Decision timestamp")


@dataclass(frozen=True)
class BaselineApprovalRecord:
    """Explicit user acceptance bound to exactly one current ChangeSet digest."""

    change_set_id: str
    source_digest: str
    approved_by: Literal["user"]
    approved_at: str
    approval_summary: str

    def __post_init__(self) -> None:
        validate_id(self.change_set_id, IdPrefix.CHANGE_SET)
        _require_digest(self.source_digest, "Approval source digest")
        if self.approved_by != "user":
            raise ValueError("Baseline approval must be approved by user")
        _require_utc_timestamp(self.approved_at, "Approval timestamp")
        if not isinstance(self.approval_summary, str) or not self.approval_summary.strip():
            raise ValueError("Approval summary must be non-empty")
        if len(self.approval_summary) > 4_000:
            raise ValueError("Approval summary exceeds 4000 characters")


@dataclass(frozen=True)
class BaselineAdvanceResult:
    """The new accepted source coordinate and immutable audit event."""

    event_id: str
    graph_revision: int
    previous_source_digest: str
    current_source_digest: str
    event: dict[str, object]
    cache_warnings: tuple[str, ...] = ()


class BaselineAdvanceService:
    """Advance one effective ChangeSet after a documented semantic decision."""

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        fact_sync: FactSyncService,
        facts: FactsDatabase,
        freshness_store: FreshnessStore,
        repository_lock: RepositoryLockPort,
        lock_timeout_seconds: float = 10,
    ) -> None:
        self.formal_store = formal_store
        self.fact_sync = fact_sync
        self.facts = facts
        self.freshness_store = freshness_store
        self.repository_lock = repository_lock
        self.lock_timeout_seconds = lock_timeout_seconds
        self._preflight = PreflightService(
            formal_store=formal_store,
            fact_sync=fact_sync,
            facts=facts,
            freshness_store=freshness_store,
            repository_lock=repository_lock,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    def preflight(self) -> PreflightResult:
        """Expose the same deterministic readiness gate used by M1b tools."""
        return self._preflight.run()

    @staticmethod
    def require_approval(
        reason: str, approval: BaselineApprovalRecord | None
    ) -> None:
        if reason == "user_accepted" and approval is None:
            raise CodeCortexError(
                ErrorCode.APPROVAL_REQUIRED,
                "user_accepted baseline advance requires explicit user approval",
            )
        if reason not in ("no_semantic_change", "user_accepted"):
            raise CodeCortexError(
                ErrorCode.APPROVAL_MISMATCH,
                "Baseline advance reason is unsupported",
            )
        if reason == "no_semantic_change" and approval is not None:
            raise CodeCortexError(
                ErrorCode.APPROVAL_MISMATCH,
                "no_semantic_change baseline advance must not carry approval",
            )

    @staticmethod
    def verify_approval(
        approval: BaselineApprovalRecord,
        change_set_id: str,
        source_digest: str,
    ) -> None:
        if (
            approval.change_set_id != change_set_id
            or approval.source_digest != source_digest
        ):
            raise CodeCortexError(
                ErrorCode.APPROVAL_MISMATCH,
                "Baseline approval does not match the current ChangeSet digest",
            )

    def advance(
        self,
        change_set_id: str,
        reason: BaselineAdvanceReason,
        decision_record: DecisionRecord,
        approval_record: BaselineApprovalRecord | None,
    ) -> BaselineAdvanceResult:
        """Commit a verified current ChangeSet as the new cognition baseline."""
        validate_id(change_set_id, IdPrefix.CHANGE_SET)
        self.require_approval(reason, approval_record)
        initial = self.preflight()
        change_set = _required_matching_change_set(initial.change_set, change_set_id)
        if reason == "user_accepted":
            assert approval_record is not None
            self.verify_approval(
                approval_record, change_set.change_set_id, change_set.current_source_digest
            )

        # A second synchronization immediately precedes the formal lock. It
        # detects source edits made after the semantic decision and makes the
        # supplied ChangeSet stale instead of silently advancing it.
        self.fact_sync.sync("auto")
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            self.formal_store.recover()
            state = self.formal_store.load()
            current = self._verified_current_change_set(state, change_set_id)
            if current.current_source_digest != change_set.current_source_digest:
                raise _stale_change_set("Current source digest changed before baseline advance")
            if reason == "user_accepted":
                assert approval_record is not None
                self.verify_approval(
                    approval_record, current.change_set_id, current.current_source_digest
                )
            event_id = new_id(IdPrefix.EVENT)
            advanced = _advanced_state(state, self.facts, current, event_id)
            validation = validate_formal_state(advanced)
            if not validation.valid:
                raise CodeCortexError(
                    ErrorCode.FORMAL_STATE_CORRUPT,
                    "Baseline advance would produce invalid formal state",
                    details={"issues": [item.message for item in validation.issues]},
                )
            if self.fact_sync.probe_source_digest() != current.current_source_digest:
                raise _stale_change_set(
                    "Managed source changed after Fact Sync and before baseline commit"
                )
            event = _baseline_event(event_id, advanced, current, reason, decision_record, approval_record)
            self.formal_store.commit_baseline_advance(advanced, event)

        warnings = self._refresh_caches(current.current_source_digest)
        return BaselineAdvanceResult(
            event_id=event_id,
            graph_revision=advanced.manifest.graph_revision,
            previous_source_digest=current.baseline_source_digest,
            current_source_digest=current.current_source_digest,
            event=event,
            cache_warnings=warnings,
        )

    def _refresh_caches(self, baseline_source_digest: str) -> tuple[str, ...]:
        """Refresh disposable baseline caches after formal truth is durable."""
        try:
            self.facts.replace_baseline_entity_snapshots(baseline_source_digest)
        except (CodeCortexError, OSError, sqlite3.Error, ValueError) as error:
            # Preserve the old effective ChangeSet as another recovery signal.
            return (
                (
                    f"{ErrorCode.CACHE_REBUILD_REQUIRED.value}: baseline entity "
                    f"snapshot refresh failed after the formal commit: {error}"
                ),
            )
        try:
            self.freshness_store.replace_effective(None)
        except (CodeCortexError, OSError, ValueError) as error:
            return (
                (
                    f"{ErrorCode.CACHE_REBUILD_REQUIRED.value}: effective freshness "
                    f"reset failed after the formal commit: {error}"
                ),
            )
        return ()

    def _verified_current_change_set(
        self, state: FormalState, change_set_id: str
    ) -> ChangeSet:
        metadata = self.facts.cache_metadata()
        if metadata.graph_revision != state.manifest.graph_revision:
            raise _stale_change_set("Fact cache graph revision changed before baseline advance")
        current = self.freshness_store.load_effective()
        current = _required_matching_change_set(current, change_set_id)
        if current.baseline_source_digest != state.manifest.cognition_baseline:
            raise _stale_change_set("ChangeSet no longer starts at the formal baseline")
        if current.current_source_digest != metadata.repository_source_digest:
            raise _stale_change_set("ChangeSet no longer matches current source facts")
        return current


def _advanced_state(
    state: FormalState, facts: FactsDatabase, change_set: ChangeSet, event_id: str
) -> FormalState:
    source_files = facts.source_file_digests()
    aggregate = repository_digest_from_file_digests(
        source_files, DigestProfile(DIGEST_PROFILE_VERSION)
    )
    if aggregate != change_set.current_source_digest:
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            "Baseline file records do not match the verified current source digest",
        )
    baseline = SourceBaseline(
        SCHEMA_VERSION,
        DIGEST_PROFILE_VERSION,
        MANAGED_SOURCE_SET_VERSION,
        change_set.current_source_digest,
        tuple(
            {"relative_path": path, "content_digest": digest}
            for path, digest in sorted(source_files.items())
        ),
    )
    refs = recompute_entity_refs(
        state.graph,
        state.graph.graph_revision,
        facts=facts,
        previous=state.entity_refs,
    )
    return replace(
        state,
        manifest=replace(state.manifest, cognition_baseline=change_set.current_source_digest),
        entity_refs=refs,
        source_baseline=baseline,
        history_events=(
            *state.history_events,
            HistoryEventRef(event_id, "cognition_baseline_advanced"),
        ),
    )


def _baseline_event(
    event_id: str,
    state: FormalState,
    change_set: ChangeSet,
    reason: BaselineAdvanceReason,
    decision: DecisionRecord,
    approval: BaselineApprovalRecord | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "cognition_baseline_advanced",
        "graph_revision": state.manifest.graph_revision,
        "change_set_id": change_set.change_set_id,
        "before_source_digest": change_set.baseline_source_digest,
        "after_source_digest": change_set.current_source_digest,
        "reason": reason,
        "decision_record": {
            "decided_by": decision.decided_by,
            "evidence_summary": decision.evidence_summary,
            "decided_at": decision.decided_at,
        },
        "approval": (
            None
            if approval is None
            else {
                "change_set_id": approval.change_set_id,
                "source_digest": approval.source_digest,
                "approved_by": approval.approved_by,
                "approved_at": approval.approved_at,
                "approval_summary": approval.approval_summary,
            }
        ),
        "change_set_summary": _change_set_summary(change_set),
        "applied_at": decision.decided_at,
    }


def _change_set_summary(change_set: ChangeSet) -> dict[str, object]:
    return {
        "before_source_digest": change_set.baseline_source_digest,
        "after_source_digest": change_set.current_source_digest,
        "changed_files": {
            "added": list(change_set.changed_files.added),
            "modified": list(change_set.changed_files.modified),
            "deleted": list(change_set.changed_files.deleted),
            "renamed": [list(item) for item in change_set.changed_files.renamed],
        },
        "changed_entities": {
            "added": list(change_set.changed_entities.added),
            "modified": list(change_set.changed_entities.modified),
            "missing": list(change_set.changed_entities.missing),
            "moved": [list(item) for item in change_set.changed_entities.moved],
        },
        "affected_nodes": list(change_set.affected_nodes),
        "scope_confidence": change_set.scope_confidence,
        "unmapped_changes": [dict(item) for item in change_set.unmapped_changes],
    }


def _required_matching_change_set(
    change_set: ChangeSet | None, expected_id: str
) -> ChangeSet:
    if change_set is None or change_set.change_set_id != expected_id:
        raise _stale_change_set("The requested ChangeSet is no longer effective")
    return change_set


def _stale_change_set(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PROPOSAL_STALE,
        message,
        suggested_action="Run Fact Preflight again and repeat the semantic decision",
    )


def _require_digest(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name} must be a SHA-256 digest")


def _require_utc_timestamp(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp") from error
