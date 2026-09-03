"""Bounded M1a read orchestration over the fact cache and cognitive replica.

Design authority: ``docs/CodeCortex_M1a_Detailed_Design.md`` sections 5 and 17.

Every request first validates its scope/limit inputs, then passes the cache
guard, and only then runs batched, bounded queries.  The guard enforces
section 5: a version, source-digest, or graph-revision mismatch forbids
joining cognitive query results with source facts, so the read fails closed
with ``CACHE_REBUILD_REQUIRED`` instead of returning mixed snapshots.  All
list results carry cursor and truncation metadata; no query here loads a
whole table into memory or loops per row.

The service depends on narrow structural ports (``FactQueryPort`` /
``GraphQueryPort``) satisfied by ``FactsDatabase`` and ``GraphReplica``; it
never constructs infrastructure concretions itself.

Documented design decisions:

- The guard compares the fact-cache schema version, both stored graph
  revisions, and any caller-pinned expectations.  Parser/digest/managed-set
  version drift is Fact Sync's rebuild concern: a version change forces the
  next sync to rewrite the cache, while read-path consistency is anchored on
  the graph revision and the recorded source digest.
- The live on-disk source digest is deliberately not recomputed per read;
  every response reports the ``(repository_source_digest, graph_revision)``
  coordinate it was read at so callers can pin or refresh explicitly.
- ``search_cognitive_graph`` has no cursor semantics in the replica; its
  ``truncated`` flag is conservatively true whenever the page is full.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from codecortex.application.ports import FormalStorePort, RepositoryLockPort
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.persistence.facts_db import (
    ALLOWED_RELATION_TYPES,
    CacheMetadata,
    CodeEntity,
    CodeRelation,
    FactDiagnostic,
    FactScope,
    FactTotals,
    ModulePartition,
    Page,
)
from codecortex.infrastructure.persistence.graph_replica import (
    NODE_KINDS,
    ContextEntity,
    ContextMapping,
    ContextRequest,
    DiscussionContext,
    GraphHit,
    ReplicaMetadata,
)

DEFAULT_PAGE_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 20


class FactQueryPort(Protocol):
    """The bounded read surface of the disposable fact cache."""

    schema_version: int
    max_page_size: int

    def cache_metadata(self) -> CacheMetadata:
        """Load the sole cache metadata row."""

    def analysis_totals(self) -> FactTotals:
        """Return whole-cache aggregate counts."""

    def analysis_partitions(
        self, module: str | None, cursor: str | None, limit: int
    ) -> Page[ModulePartition]:
        """Return one bounded page of package/module partitions."""

    def query_diagnostics(
        self, module: str | None, cursor: str | None, limit: int
    ) -> Page[FactDiagnostic]:
        """Return one bounded page of recorded diagnostics."""

    def query_entities(
        self, scope: FactScope, cursor: str | None, limit: int
    ) -> Page[CodeEntity]:
        """Return one bounded page of entities in an allowlisted scope."""

    def entities_at_path(
        self, relative_path: str, cursor: str | None, limit: int
    ) -> Page[CodeEntity]:
        """Return one bounded page of entities declared in one file."""

    def entities_by_address(
        self, address: str, cursor: str | None, limit: int
    ) -> Page[CodeEntity]:
        """Return one bounded page of entities with one exact address."""

    def entity_by_uid(self, uid: str) -> CodeEntity | None:
        """Return the current entity for *uid*, or None when it is gone."""

    def query_relations(
        self,
        entity_uids: Sequence[str],
        relation_types: Sequence[str],
        limit: int,
        cursor: str | None = None,
    ) -> Page[CodeRelation]:
        """Return one bounded page of relations for explicit entity UIDs."""


class GraphQueryPort(Protocol):
    """The bounded read surface of the cognitive query replica."""

    def metadata(self) -> ReplicaMetadata:
        """Load the sole replica metadata row."""

    def search(
        self,
        query: str,
        kinds: Sequence[str],
        limit: int,
        expected_revision: int,
    ) -> tuple[GraphHit, ...]:
        """Return deterministic weighted hits bounded by *limit*."""

    def context(self, request: ContextRequest) -> DiscussionContext:
        """Traverse a bounded neighborhood and materialize it as DTOs."""

    def node_kind_counts(self, expected_revision: int) -> dict[str, int]:
        """Count indexed nodes per kind at exactly *expected_revision*."""

    def entity_refs_for(
        self, entity_uids: Sequence[str], expected_revision: int
    ) -> tuple[ContextEntity, ...]:
        """Return formal entity-reference rows for *entity_uids*."""

    def mappings_for_entities(
        self, entity_uids: Sequence[str], limit: int, expected_revision: int
    ) -> tuple[tuple[ContextMapping, ...], bool]:
        """Return bounded implementation mappings and a truncation flag."""


@dataclass(frozen=True)
class CacheCoordinate:
    """The snapshot coordinate one guarded read was served from."""

    repository_source_digest: str
    graph_revision: int


@dataclass(frozen=True)
class RepositoryFactsPage:
    """One guarded page of code entities for a module scope."""

    coordinate: CacheCoordinate
    entities: tuple[CodeEntity, ...]
    cursor: str | None
    truncated: bool


@dataclass(frozen=True)
class AnalysisScopeResult:
    """The guarded package/module partition overview for delegation checks."""

    coordinate: CacheCoordinate
    scope: str | None
    totals: FactTotals
    node_kind_counts: Mapping[str, int]
    partitions: tuple[ModulePartition, ...]
    cursor: str | None
    truncated: bool
    diagnostics: tuple[FactDiagnostic, ...]
    diagnostics_truncated: bool


@dataclass(frozen=True)
class EntityContextResult:
    """The guarded cognitive and factual context around one entity anchor.

    ``cursor``/``truncated`` page the entity list (path/address anchors);
    mappings and relations are bounded secondary lists with their own
    truncation flags so callers can narrow with relation types or limits.
    """

    coordinate: CacheCoordinate
    anchor_kind: str
    anchor_value: str
    entities: tuple[CodeEntity, ...]
    cursor: str | None
    truncated: bool
    entity_refs: tuple[ContextEntity, ...]
    mappings: tuple[ContextMapping, ...]
    mappings_truncated: bool
    relations: tuple[CodeRelation, ...]
    relations_truncated: bool


@dataclass(frozen=True)
class SearchPage:
    """One guarded page of weighted cognitive search hits."""

    coordinate: CacheCoordinate
    hits: tuple[GraphHit, ...]
    truncated: bool


def _rebuild_required(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.CACHE_REBUILD_REQUIRED,
        message,
        retryable=True,
        suggested_action=(
            "Run sync_repository_facts from the Main profile to rebuild the "
            "local caches, then retry the read"
        ),
    )


class CacheGuard:
    """Reject mixed-snapshot reads before facts and cognition are joined."""

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        facts: FactQueryPort,
        replica: GraphQueryPort,
    ) -> None:
        self._formal_store = formal_store
        self._facts = facts
        self._replica = replica

    def require_current(
        self,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> CacheCoordinate:
        """Return the current coordinate or fail closed on any mismatch."""
        revision = self._formal_store.load().graph.graph_revision
        cache = self._cache_metadata()
        replica_metadata = self._replica_metadata()
        if cache.cache_schema_version != self._facts.schema_version:
            raise _rebuild_required("Fact cache schema version is incompatible")
        if cache.graph_revision != revision:
            raise _rebuild_required(
                "Fact cache graph revision does not match the formal graph"
            )
        if replica_metadata.graph_revision != revision:
            raise _rebuild_required(
                "Cognitive replica graph revision does not match the formal graph"
            )
        if expected_graph_revision is not None:
            if (
                type(expected_graph_revision) is not int
                or isinstance(expected_graph_revision, bool)
                or expected_graph_revision < 0
            ):
                raise ValueError("Expected graph revision must be a non-negative integer")
            if expected_graph_revision != revision:
                raise _rebuild_required(
                    "Caller-pinned graph revision is stale; re-read the current "
                    "coordinate before continuing"
                )
        if expected_source_digest is not None:
            if not isinstance(expected_source_digest, str) or not expected_source_digest:
                raise ValueError("Expected source digest must be a non-empty string")
            if expected_source_digest != cache.repository_source_digest:
                raise _rebuild_required(
                    "Caller-pinned source digest is stale; re-run Fact Sync or "
                    "re-read the current coordinate before continuing"
                )
        return CacheCoordinate(cache.repository_source_digest, revision)

    def _cache_metadata(self) -> CacheMetadata:
        try:
            return self._facts.cache_metadata()
        except (OSError, sqlite3.Error, ValueError) as error:
            raise _rebuild_required(
                "Fact cache is missing or unreadable"
            ) from error

    def _replica_metadata(self) -> ReplicaMetadata:
        try:
            return self._replica.metadata()
        except CodeCortexError:
            raise
        except (OSError, sqlite3.Error, ValueError) as error:
            raise _rebuild_required(
                "Cognitive replica is missing or unreadable"
            ) from error


class QueryService:
    """Coordinate the bounded M1a read APIs over locked, guarded snapshots."""

    def __init__(
        self,
        *,
        formal_store: FormalStorePort,
        facts: FactQueryPort,
        replica: GraphQueryPort,
        repository_lock: RepositoryLockPort,
        lock_timeout_seconds: float = 10,
        max_limit: int = 100,
    ) -> None:
        if type(max_limit) is not int or isinstance(max_limit, bool) or max_limit < 1:
            raise ValueError("Query maximum limit must be a positive integer")
        self.cache_guard = CacheGuard(
            formal_store=formal_store, facts=facts, replica=replica
        )
        self._facts = facts
        self._replica = replica
        self._repository_lock = repository_lock
        self._lock_timeout_seconds = lock_timeout_seconds
        self._max_limit = max_limit

    def repository_facts(
        self,
        scope: str,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        *,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> RepositoryFactsPage:
        """Return one guarded, cursor-paginated page of module entities."""
        module = _required_text(scope, "repository_facts scope")
        checked_limit = self._bounded_limit(limit)
        checked_cursor = _optional_cursor(cursor)
        with self._repository_lock.acquire("shared", self._lock_timeout_seconds):
            coordinate = self.cache_guard.require_current(
                expected_source_digest, expected_graph_revision
            )
            page = self._facts.query_entities(
                FactScope.module(module), checked_cursor, checked_limit
            )
        return RepositoryFactsPage(
            coordinate=coordinate,
            entities=page.items,
            cursor=page.next_cursor,
            truncated=page.truncated,
        )

    def analysis_scope(
        self,
        scope: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        *,
        diagnostics_limit: int = DEFAULT_PAGE_LIMIT,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> AnalysisScopeResult:
        """Return guarded package/module partitions, totals, and diagnostics."""
        module = _optional_text(scope, "analysis_scope scope")
        checked_limit = self._bounded_limit(limit)
        checked_diagnostics_limit = self._bounded_limit(diagnostics_limit)
        checked_cursor = _optional_cursor(cursor)
        with self._repository_lock.acquire("shared", self._lock_timeout_seconds):
            coordinate = self.cache_guard.require_current(
                expected_source_digest, expected_graph_revision
            )
            totals = self._facts.analysis_totals()
            partitions = self._facts.analysis_partitions(
                module, checked_cursor, checked_limit
            )
            diagnostics = self._facts.query_diagnostics(
                module, None, checked_diagnostics_limit
            )
            kind_counts = self._replica.node_kind_counts(coordinate.graph_revision)
        return AnalysisScopeResult(
            coordinate=coordinate,
            scope=module,
            totals=totals,
            node_kind_counts=kind_counts,
            partitions=partitions.items,
            cursor=partitions.next_cursor,
            truncated=partitions.truncated,
            diagnostics=diagnostics.items,
            diagnostics_truncated=diagnostics.truncated,
        )

    def resolve_entity_context(
        self,
        *,
        entity_uid: str | None = None,
        path: str | None = None,
        address: str | None = None,
        relation_types: Sequence[str] = (),
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> EntityContextResult:
        """Resolve exactly one anchor to entities, mappings, and relations."""
        anchors = tuple(
            (kind, value)
            for kind, value in (
                ("entity_uid", entity_uid),
                ("path", path),
                ("address", address),
            )
            if value is not None
        )
        if len(anchors) != 1:
            raise ValueError(
                "resolve_entity_context accepts exactly one anchor form: "
                "entity_uid, path, or address"
            )
        anchor_kind, raw_anchor = anchors[0]
        anchor_value = _required_text(raw_anchor, f"{anchor_kind} anchor")
        checked_limit = self._bounded_limit(limit)
        checked_cursor = _optional_cursor(cursor)
        types = _relation_types(relation_types)
        with self._repository_lock.acquire("shared", self._lock_timeout_seconds):
            coordinate = self.cache_guard.require_current(
                expected_source_digest, expected_graph_revision
            )
            if anchor_kind == "entity_uid":
                entity = self._facts.entity_by_uid(anchor_value)
                entity_page: Page[CodeEntity] = Page(
                    items=() if entity is None else (entity,),
                    next_cursor=None,
                    truncated=False,
                )
            elif anchor_kind == "path":
                entity_page = self._facts.entities_at_path(
                    anchor_value, checked_cursor, checked_limit
                )
            else:
                entity_page = self._facts.entities_by_address(
                    anchor_value, checked_cursor, checked_limit
                )
            uids = tuple(entity.uid for entity in entity_page.items)
            lookup_uids = uids or (
                (anchor_value,) if anchor_kind == "entity_uid" else ()
            )
            entity_refs = self._replica.entity_refs_for(
                lookup_uids, coordinate.graph_revision
            )
            mappings, mappings_truncated = self._replica.mappings_for_entities(
                lookup_uids, checked_limit, coordinate.graph_revision
            )
            relations = (
                self._facts.query_relations(uids, types, checked_limit)
                if uids
                else Page(items=(), next_cursor=None, truncated=False)
            )
        return EntityContextResult(
            coordinate=coordinate,
            anchor_kind=anchor_kind,
            anchor_value=anchor_value,
            entities=entity_page.items,
            cursor=entity_page.next_cursor,
            truncated=entity_page.truncated,
            entity_refs=entity_refs,
            mappings=mappings,
            mappings_truncated=mappings_truncated,
            relations=relations.items,
            relations_truncated=relations.truncated,
        )

    def get_discussion_context(self, request: ContextRequest) -> DiscussionContext:
        """Return one guarded, bounded discussion-context neighborhood."""
        if not isinstance(request, ContextRequest):
            raise ValueError(  # noqa: TRY004 - validation contract uses ValueError
                "Discussion context requires a ContextRequest"
            )
        with self._repository_lock.acquire("shared", self._lock_timeout_seconds):
            coordinate = self.cache_guard.require_current(
                request.expected_source_digest, request.expected_graph_revision
            )
            return self._replica.context(
                replace(request, expected_graph_revision=coordinate.graph_revision)
            )

    def search_cognitive_graph(
        self,
        query: str,
        kinds: Sequence[str] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        *,
        expected_source_digest: str | None = None,
        expected_graph_revision: int | None = None,
    ) -> SearchPage:
        """Return guarded, deterministic, bounded cognitive search hits."""
        text = _required_text(query, "search query")
        checked_limit = self._bounded_limit(limit)
        kind_filter = _node_kinds(kinds)
        with self._repository_lock.acquire("shared", self._lock_timeout_seconds):
            coordinate = self.cache_guard.require_current(
                expected_source_digest, expected_graph_revision
            )
            hits = self._replica.search(
                text, kind_filter, checked_limit, coordinate.graph_revision
            )
        return SearchPage(
            coordinate=coordinate,
            hits=hits,
            truncated=len(hits) >= checked_limit,
        )

    def _bounded_limit(self, limit: int) -> int:
        if type(limit) is not int or isinstance(limit, bool) or limit <= 0:
            raise ValueError("Query limit must be a positive integer")
        if limit > self._max_limit:
            raise ValueError("Query limit exceeds the configured maximum")
        return limit


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _optional_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Query cursor must be a non-empty string")
    return cursor


def _node_kinds(kinds: Sequence[str]) -> tuple[str, ...]:
    if isinstance(kinds, str):
        raise ValueError(  # noqa: TRY004 - validation contract uses ValueError
            "Search kind filter must be a sequence of node kinds"
        )
    materialized = tuple(kinds)
    if any(not isinstance(kind, str) or not kind for kind in materialized):
        raise ValueError("Search kind filter must contain non-empty strings")
    unknown = sorted({kind for kind in materialized if kind not in NODE_KINDS})
    if unknown:
        raise ValueError(f"Search kind filter contains unknown node kinds: {unknown}")
    return materialized


def _relation_types(relation_types: Sequence[str]) -> tuple[str, ...]:
    if isinstance(relation_types, str):
        raise ValueError(  # noqa: TRY004 - validation contract uses ValueError
            "Relation types must be a sequence of relation type names"
        )
    if not relation_types:
        return tuple(sorted(ALLOWED_RELATION_TYPES))
    materialized = tuple(relation_types)
    if any(not isinstance(item, str) or not item for item in materialized):
        raise ValueError("Relation types must contain non-empty strings")
    unknown = sorted({item for item in materialized if item not in ALLOWED_RELATION_TYPES})
    if unknown:
        raise ValueError(f"Relation types must be allowlisted; unknown: {unknown}")
    return materialized
