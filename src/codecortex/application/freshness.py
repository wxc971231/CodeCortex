"""Transparent repository and query-local cognition freshness decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from codecortex.domain.freshness import ChangeSet, ScopeConfidence

RepositoryCognitionStatus = Literal["fresh", "pending", "unresolved"]
EffectiveQueryFreshness = Literal[
    "current",
    "unaffected_current",
    "affected_source_first",
    "unknown_source_first",
]


@dataclass(frozen=True)
class RepositoryFreshnessSummary:
    """Repository-level status for display; it never substitutes query scope."""

    status: RepositoryCognitionStatus
    change_set_id: str | None
    baseline_source_digest: str | None
    current_source_digest: str | None
    scope_confidence: ScopeConfidence | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class QueryFreshnessResult:
    """Auditable freshness route for one bounded node/entity query scope."""

    status: EffectiveQueryFreshness
    repository_status: RepositoryCognitionStatus
    change_set_id: str | None
    baseline_source_digest: str | None
    current_source_digest: str | None
    scope_confidence: ScopeConfidence | None
    matched_affected_nodes: tuple[str, ...]
    matched_affected_entities: tuple[str, ...]
    unmapped_changes: tuple[dict[str, object], ...]
    reason_codes: tuple[str, ...]


class FreshnessService:
    """Derive transparent local freshness from exactly one effective ChangeSet."""

    def __init__(self, change_set: ChangeSet | None = None) -> None:
        self._change_set = change_set

    def set_change_set(self, change_set: ChangeSet | None) -> None:
        """Replace the in-memory effective ChangeSet; no per-node state exists."""
        self._change_set = change_set

    def repository_status(self) -> RepositoryFreshnessSummary:
        """Return the repository summary without treating it as a query verdict."""
        change_set = self._change_set
        if change_set is None:
            return RepositoryFreshnessSummary(
                status="fresh",
                change_set_id=None,
                baseline_source_digest=None,
                current_source_digest=None,
                scope_confidence=None,
                reason_codes=("NO_PENDING_CHANGE_SET",),
            )
        status: RepositoryCognitionStatus = (
            "unresolved" if change_set.scope_confidence == "unknown" else "pending"
        )
        return RepositoryFreshnessSummary(
            status=status,
            change_set_id=change_set.change_set_id,
            baseline_source_digest=change_set.baseline_source_digest,
            current_source_digest=change_set.current_source_digest,
            scope_confidence=change_set.scope_confidence,
            reason_codes=(
                "CHANGE_SET_SCOPE_UNKNOWN"
                if status == "unresolved"
                else "PENDING_CHANGE_SET"
            ,),
        )

    def for_query(
        self,
        node_ids: Sequence[str],
        entity_ids: Sequence[str],
    ) -> QueryFreshnessResult:
        """Return the source-first decision without making Main Codex recompute it."""
        nodes = _validated_identifiers(node_ids, "Node IDs")
        entities = _validated_identifiers(entity_ids, "Entity IDs")
        summary = self.repository_status()
        change_set = self._change_set
        if change_set is None:
            return QueryFreshnessResult(
                status="current",
                repository_status=summary.status,
                change_set_id=None,
                baseline_source_digest=None,
                current_source_digest=None,
                scope_confidence=None,
                matched_affected_nodes=(),
                matched_affected_entities=(),
                unmapped_changes=(),
                reason_codes=("NO_PENDING_CHANGE_SET",),
            )

        matched_nodes = tuple(sorted(set(nodes) & set(change_set.affected_nodes)))
        matched_entities = tuple(
            sorted(set(entities) & set(change_set.affected_entities))
        )
        unmapped = tuple(dict(item) for item in change_set.unmapped_changes)
        if matched_nodes or matched_entities:
            status: EffectiveQueryFreshness = "affected_source_first"
            reasons = ("QUERY_INTERSECTS_AFFECTED_SCOPE",)
        elif change_set.scope_confidence == "complete":
            status = "unaffected_current"
            reasons = ("COMPLETE_SCOPE_DOES_NOT_INTERSECT_QUERY",)
        else:
            status = "unknown_source_first"
            reasons = (
                "UNMAPPED_SCOPE" if unmapped else "SCOPE_NOT_COMPLETE",
            )
        return QueryFreshnessResult(
            status=status,
            repository_status=summary.status,
            change_set_id=change_set.change_set_id,
            baseline_source_digest=change_set.baseline_source_digest,
            current_source_digest=change_set.current_source_digest,
            scope_confidence=change_set.scope_confidence,
            matched_affected_nodes=matched_nodes,
            matched_affected_entities=matched_entities,
            unmapped_changes=unmapped,
            reason_codes=reasons,
        )


def _validated_identifiers(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be a sequence of identifiers")
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain non-empty identifiers")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result
