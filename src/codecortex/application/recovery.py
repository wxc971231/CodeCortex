"""Deterministic reconstruction of disposable M1b caches from formal state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

from codecortex.application.affected_scope import AffectedScopeCalculator
from codecortex.application.change_detection import ChangeDetector
from codecortex.application.fact_sync import FactSyncResult, FactSyncService
from codecortex.application.freshness import (
    FreshnessService,
    RepositoryFreshnessSummary,
)
from codecortex.application.ports import FormalStorePort, RepositoryLockPort
from codecortex.application.proposals import typed_graph_from_formal
from codecortex.domain.cognition import FormalState
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.freshness import ChangeSet
from codecortex.infrastructure.persistence.facts_db import CodeEntity, FactsDatabase
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.infrastructure.python.parser import EntityIdentityHint, EntityKind
from codecortex.telemetry import traced


@dataclass(frozen=True)
class CacheRecoveryResult:
    """One reconstructed cache coordinate, with no formal state mutation."""

    fact_sync: FactSyncResult
    change_set: ChangeSet | None
    repository_status: RepositoryFreshnessSummary
    facts: FactsDatabase
    rebuilt_facts: bool
    rebuilt_replica: bool
    agent_calls: int = 0


class RecoveryService:
    """Rebuild local facts, replica, and effective ChangeSet from Git state.

    This is intentionally a deterministic Core-only repair.  ``FormalStore``
    validates every Git-carried JSON/History/View invariant before any cache
    write; cache content never becomes a fallback source of formal truth.
    """

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        fact_sync: FactSyncService,
        facts: FactsDatabase,
        freshness_store: FreshnessStore,
        repository_lock: RepositoryLockPort,
        cognitive_replica: GraphReplica | None = None,
        lock_timeout_seconds: float = 10,
    ) -> None:
        if type(lock_timeout_seconds) not in (int, float) or lock_timeout_seconds < 0:
            raise ValueError("Recovery lock timeout must be non-negative")
        self.formal_store = formal_store
        self.fact_sync = fact_sync
        self.facts = facts
        self.freshness_store = freshness_store
        self.repository_lock = repository_lock
        self.cognitive_replica = cognitive_replica
        self.lock_timeout_seconds = lock_timeout_seconds

    @traced("recovery.requires_recovery", level="DEBUG")
    def requires_recovery(self) -> bool:
        """Return whether local facts/replica/pointer cannot serve formal state."""
        try:
            with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
                state = self.formal_store.load()
                self._require_initialized(state)
                if not self.facts.path.exists() or not self.facts.integrity_ok():
                    return True
                metadata = self.facts.cache_metadata()
                if (
                    metadata.cache_schema_version != FactsDatabase.schema_version
                    or metadata.parser_version != self.fact_sync.parser_version
                    or metadata.digest_profile_version
                    != state.manifest.digest_profile_version
                    or metadata.managed_source_set_version
                    != state.manifest.managed_source_set_version
                    or metadata.graph_revision != state.manifest.graph_revision
                ):
                    return True
                baseline_digest = state.manifest.cognition_baseline
                if any(
                    snapshot.baseline_source_digest != baseline_digest
                    for snapshot in self.facts.baseline_entity_snapshots()
                ):
                    return True
                if self.cognitive_replica is not None:
                    replica = self.cognitive_replica.metadata()
                    if replica.graph_revision != state.manifest.graph_revision:
                        return True
                effective = self.freshness_store.load_effective()
                return bool(
                    effective is not None
                    and effective.baseline_source_digest != baseline_digest
                )
        except (
            CodeCortexError,
            OSError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
        ):
            return True

    @traced("recovery.ensure_cache", level="DEBUG")
    def ensure_cache(self) -> CacheRecoveryResult:
        """Run the fixed eight-step recovery sequence once, without Agents."""
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            self.formal_store.recover()
            formal = self.formal_store.load()
            self._require_initialized(formal)
            hints = _formal_identity_hints(formal)

        # Full sync parses all current source outside the formal-state lock and
        # restores only identities that formal entity_refs can prove.
        synced = self.fact_sync.sync("full", identity_hints=hints)
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            formal = self.formal_store.load()
            self._require_initialized(formal)
            metadata = self.facts.cache_metadata()
            if (
                metadata.graph_revision != formal.manifest.graph_revision
                or metadata.repository_source_digest != synced.repository_source_digest
            ):
                raise CodeCortexError(
                    ErrorCode.CACHE_REBUILD_REQUIRED,
                    "Formal state changed while cache recovery was rebuilding facts",
                    retryable=True,
                    suggested_action="Retry the CodeCortex operation",
                )
            rebuilt_replica = self._rebuild_replica(formal)
            self._seed_baseline_entity_snapshots(formal, synced.repository_source_digest)
            detected = ChangeDetector().detect(formal, self.facts)
            change_set = (
                None
                if detected is None
                else detected.with_affected_scope(
                    AffectedScopeCalculator(formal, self.facts).calculate(detected)
                )
            )
            self._replace_effective(change_set)
            if self.fact_sync.probe_source_digest() != synced.repository_source_digest:
                raise CodeCortexError(
                    ErrorCode.CACHE_REBUILD_REQUIRED,
                    "Managed source changed while cache recovery was finalizing",
                    retryable=True,
                    suggested_action="Retry the CodeCortex operation",
                )
            return CacheRecoveryResult(
                fact_sync=synced,
                change_set=change_set,
                repository_status=FreshnessService(change_set).repository_status(),
                facts=self.facts,
                rebuilt_facts=True,
                rebuilt_replica=rebuilt_replica,
            )

    @traced("recovery.ensure_bootstrap_cache", level="DEBUG")
    def ensure_bootstrap_cache(self) -> None:
        """Repair only the revision-zero query replica after explicit Fact Sync.

        M1a initialization is allowed to read facts before cognition is
        initialized, but it must not silently perform the required full Fact
        Sync.  This path therefore proves that the fact cache already matches
        the live source/formal coordinate and reconstructs only the disposable
        cognitive replica.
        """
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            self.formal_store.recover()
            formal = self.formal_store.load()
            if formal.manifest.cognition_initialized:
                raise CodeCortexError(
                    ErrorCode.NOT_INITIALIZED,
                    "M1a bootstrap reads are unavailable after cognition initialization",
                )
            try:
                if not self.facts.path.is_file() or not self.facts.integrity_ok():
                    raise _bootstrap_cache_error(
                        "M1a bootstrap facts are missing or unreadable"
                    )
                metadata = self.facts.cache_metadata()
                if (
                    metadata.cache_schema_version != FactsDatabase.schema_version
                    or metadata.parser_version != self.fact_sync.parser_version
                    or metadata.digest_profile_version
                    != formal.manifest.digest_profile_version
                    or metadata.managed_source_set_version
                    != formal.manifest.managed_source_set_version
                    or metadata.graph_revision != formal.manifest.graph_revision
                    or self.fact_sync.probe_source_digest()
                    != metadata.repository_source_digest
                ):
                    raise _bootstrap_cache_error(
                        "M1a bootstrap facts do not match the live formal/source coordinate"
                    )
            except CodeCortexError:
                raise
            except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
                raise _bootstrap_cache_error(
                    "M1a bootstrap facts are missing or unreadable"
                ) from error

            replica_current = False
            if self.cognitive_replica is not None:
                try:
                    replica_current = (
                        self.cognitive_replica.metadata().graph_revision
                        == formal.manifest.graph_revision
                    )
                except (
                    CodeCortexError,
                    OSError,
                    sqlite3.DatabaseError,
                    TypeError,
                    ValueError,
                ):
                    replica_current = False
            if not replica_current:
                self._rebuild_replica(formal)

            # Recheck both authorities before releasing the lock; rebuilding a
            # replica for a source or formal coordinate that moved is unsafe.
            current = self.formal_store.load()
            current_metadata = self.facts.cache_metadata()
            if current.manifest.cognition_initialized:
                raise CodeCortexError(
                    ErrorCode.NOT_INITIALIZED,
                    "M1a bootstrap reads are unavailable after cognition initialization",
                )
            if (
                current.manifest.graph_revision != formal.manifest.graph_revision
                or current_metadata.graph_revision != current.manifest.graph_revision
                or self.fact_sync.probe_source_digest()
                != current_metadata.repository_source_digest
            ):
                raise _bootstrap_cache_error(
                    "M1a bootstrap coordinate changed while rebuilding its replica"
                )

    @staticmethod
    def _require_initialized(formal: FormalState) -> None:
        if not formal.manifest.cognition_initialized:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "M1b cache recovery requires initialized repository cognition",
            )

    @traced("recovery.rebuild_replica", level="DEBUG")
    def _rebuild_replica(self, formal: FormalState) -> bool:
        if self.cognitive_replica is None:
            return False
        graph = typed_graph_from_formal(formal)
        try:
            self.cognitive_replica.rebuild(graph, formal.manifest.graph_revision)
        except sqlite3.DatabaseError as error:
            if not _is_corrupt_sqlite(error):
                raise
            # The replica is never formal truth.  Reset exactly this local
            # cache file, then rebuild from the already-validated formal graph.
            self.cognitive_replica.reset()
            self.cognitive_replica.rebuild(graph, formal.manifest.graph_revision)
        return True

    def _seed_baseline_entity_snapshots(
        self, formal: FormalState, current_digest: str
    ) -> None:
        baseline_digest = formal.manifest.cognition_baseline
        if baseline_digest is None:
            raise CodeCortexError(
                ErrorCode.FORMAL_STATE_CORRUPT,
                "Initialized cognition has no source baseline for cache recovery",
            )
        if current_digest == baseline_digest:
            # Current source exactly is baseline source, so every rebuilt
            # current entity is a sound baseline comparison snapshot.
            self.facts.replace_baseline_entity_snapshots(baseline_digest)
            return
        resolvable: list[CodeEntity] = []
        for reference in formal.entity_refs.entities:
            entity = _resolve_formal_reference(self.facts, reference)
            if entity is not None:
                resolvable.append(entity)
        self.facts.seed_partial_baseline_entity_snapshots(
            baseline_digest, tuple(sorted(resolvable, key=lambda entity: entity.uid))
        )

    def _replace_effective(self, change_set: ChangeSet | None) -> None:
        try:
            self.freshness_store.replace_effective(change_set)
        except (OSError, ValueError, TypeError):
            # The pointer is cache-only and validated by FreshnessStore; do not
            # attempt to interpret a corrupt payload before replacing it.
            self.freshness_store.freshness_path.unlink(missing_ok=True)
            self.freshness_store.replace_effective(change_set)


def _formal_identity_hints(formal: FormalState) -> tuple[EntityIdentityHint, ...]:
    hints: list[EntityIdentityHint] = []
    for entry in formal.entity_refs.entities:
        try:
            kind = cast(EntityKind, entry["kind"])
            hint = EntityIdentityHint(
                uid=str(entry["uid"]),
                address=str(entry["last_known_address"]),
                kind=kind,
                relative_path=str(entry["relative_path"]),
                signature=(
                    None if entry.get("signature") is None else str(entry["signature"])
                ),
                fingerprint=str(entry["fingerprint"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CodeCortexError(
                ErrorCode.FORMAL_STATE_CORRUPT,
                "Formal entity references cannot seed cache recovery",
            ) from error
        hints.append(hint)
    return tuple(sorted(hints, key=lambda hint: (hint.relative_path, hint.uid)))


def _resolve_formal_reference(
    facts: FactsDatabase, reference: dict[str, object]
) -> CodeEntity | None:
    try:
        uid = str(reference["uid"])
        direct = facts.entity_by_uid(uid)
        if direct is not None:
            return direct
        resolved = facts.resolve_entity_reference(
            last_known_address=str(reference["last_known_address"]),
            kind=str(reference["kind"]),
            signature=(
                None if reference.get("signature") is None else str(reference["signature"])
            ),
            fingerprint=str(reference["fingerprint"]),
        )
        return resolved.entity if resolved.status == "resolved" else None
    except (KeyError, TypeError, ValueError) as error:
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            "Formal entity reference cannot be resolved during cache recovery",
        ) from error


def _is_corrupt_sqlite(error: sqlite3.DatabaseError) -> bool:
    message = str(error).lower()
    return "file is not a database" in message or "database disk image is malformed" in message


def _bootstrap_cache_error(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.CACHE_REBUILD_REQUIRED,
        message,
        retryable=True,
        suggested_action=(
            "Run sync_repository_facts with mode=full from Main, then retry "
            "the M1a bootstrap read"
        ),
    )
