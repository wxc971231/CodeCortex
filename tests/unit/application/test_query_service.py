"""Unit coverage for bounded M1a read orchestration (plan Task 8)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codecortex.application.query import QueryService
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.graph_replica import (
    ContextRequest,
    GraphReplica,
)
from tests.conftest import ENTITY_ALPHA

GRAPH_REVISION = 1
UNIT_DIGEST = "sha256:" + "0" * 64


class StubFormalStore:
    """Minimal FormalStorePort stub exposing only a graph revision."""

    def __init__(self, revision: int) -> None:
        self.revision = revision
        self.loads = 0

    def load(self) -> SimpleNamespace:
        self.loads += 1
        return SimpleNamespace(graph=SimpleNamespace(graph_revision=self.revision))


def _set_cache_metadata(
    database: FactsDatabase, revision: int, digest: str = UNIT_DIGEST
) -> None:
    with database.open_write() as connection:
        connection.execute(
            "UPDATE cache_metadata SET graph_revision = ?, "
            "repository_source_digest = ? WHERE singleton_id = 1",
            (revision, digest),
        )


def _insert_diagnostic(database: FactsDatabase, module_name: str) -> None:
    with database.open_write() as connection:
        row = connection.execute(
            "SELECT file_id FROM source_files WHERE module_name = ?", (module_name,)
        ).fetchone()
        connection.execute(
            "INSERT INTO diagnostics "
            "(file_id, code, severity, message, start_line, end_line) "
            "VALUES (?, 'E_TEST', 'error', 'broken fixture', 1, 1)",
            (row[0],),
        )


@pytest.fixture
def formal_store() -> StubFormalStore:
    return StubFormalStore(GRAPH_REVISION)


@pytest.fixture
def facts(tmp_path: Path) -> FactsDatabase:
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
    _set_cache_metadata(database, GRAPH_REVISION)
    return database


@pytest.fixture
def replica(
    tmp_path: Path,
    discussion_graph,
    replica_entity_refs,
    replica_history_events,
) -> GraphReplica:
    replica = GraphReplica.create_new(
        tmp_path / "cognitive.sqlite3",
        entity_refs=replica_entity_refs,
        history_events=replica_history_events,
    )
    replica.rebuild(discussion_graph, GRAPH_REVISION)
    return replica


@pytest.fixture
def query_service(
    formal_store: StubFormalStore,
    facts: FactsDatabase,
    replica: GraphReplica,
    tmp_path: Path,
) -> QueryService:
    return QueryService(
        formal_store=formal_store,
        facts=facts,
        replica=replica,
        repository_lock=RepositoryLock(tmp_path),
    )


def test_repository_facts_pages_with_cursor(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=3)

    first = query_service.repository_facts("pkg.mod", limit=2)

    assert len(first.entities) == 2
    assert first.truncated is True
    assert first.cursor is not None
    assert first.coordinate.graph_revision == GRAPH_REVISION
    assert first.coordinate.repository_source_digest == UNIT_DIGEST

    second = query_service.repository_facts("pkg.mod", cursor=first.cursor, limit=2)

    assert len(second.entities) == 1
    assert second.truncated is False
    assert second.cursor is None
    assert {entity.uid for entity in first.entities}.isdisjoint(
        {entity.uid for entity in second.entities}
    )


def test_repository_facts_rejects_invalid_inputs_before_guard(
    query_service: QueryService, formal_store: StubFormalStore
) -> None:
    with pytest.raises(ValueError, match="scope"):
        query_service.repository_facts("", limit=10)
    with pytest.raises(ValueError, match="limit"):
        query_service.repository_facts("pkg.mod", limit=0)
    with pytest.raises(ValueError, match="limit"):
        query_service.repository_facts("pkg.mod", limit=101)
    with pytest.raises(ValueError, match="cursor"):
        query_service.repository_facts("pkg.mod", cursor=42, limit=10)
    assert formal_store.loads == 0


def test_guard_rejects_fact_cache_revision_mismatch(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    _set_cache_metadata(facts, GRAPH_REVISION - 1)
    with pytest.raises(CodeCortexError) as exc:
        query_service.repository_facts("pkg.mod", limit=10)
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED
    assert exc.value.retryable is True


def test_guard_rejects_replica_revision_mismatch(
    query_service: QueryService, replica: GraphReplica, discussion_graph
) -> None:
    replica.rebuild(replace(discussion_graph, graph_revision=2), 2)
    with pytest.raises(CodeCortexError) as exc:
        query_service.search_cognitive_graph("resume")
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_guard_rejects_stale_expected_source_digest(query_service: QueryService) -> None:
    with pytest.raises(CodeCortexError) as exc:
        query_service.search_cognitive_graph(
            "resume", expected_source_digest="sha256:stale"
        )
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_guard_rejects_stale_expected_graph_revision(query_service: QueryService) -> None:
    with pytest.raises(CodeCortexError) as exc:
        query_service.repository_facts(
            "pkg.mod", limit=10, expected_graph_revision=GRAPH_REVISION + 1
        )
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_guard_rejects_missing_fact_cache(
    formal_store: StubFormalStore, replica: GraphReplica, tmp_path: Path
) -> None:
    service = QueryService(
        formal_store=formal_store,
        facts=FactsDatabase(tmp_path / "absent.sqlite3"),
        replica=replica,
        repository_lock=RepositoryLock(tmp_path),
    )
    with pytest.raises(CodeCortexError) as exc:
        service.repository_facts("pkg.mod", limit=10)
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_guard_rejects_missing_replica(
    formal_store: StubFormalStore, facts: FactsDatabase, tmp_path: Path
) -> None:
    service = QueryService(
        formal_store=formal_store,
        facts=facts,
        replica=GraphReplica(tmp_path / "absent-replica.sqlite3"),
        repository_lock=RepositoryLock(tmp_path),
    )
    with pytest.raises(CodeCortexError) as exc:
        service.search_cognitive_graph("resume")
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_analysis_scope_partitions_by_module(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=2)
    facts.insert_test_entities(module_name="pkg.other", count=1)
    _insert_diagnostic(facts, "pkg.mod")

    result = query_service.analysis_scope()

    partitions = {partition.module_name: partition for partition in result.partitions}
    assert partitions["pkg.mod"].entity_count == 2
    assert partitions["pkg.mod"].diagnostic_count == 1
    assert partitions["pkg.other"].file_count == 1
    # insert_test_entities reuses the test_ent_%05d uid scheme with
    # INSERT OR IGNORE, so pkg.other's colliding entity row is ignored.
    assert result.totals.entity_count == 2
    assert result.totals.file_count == 2
    assert result.totals.diagnostic_count == 1
    assert result.node_kind_counts == {
        "behavior": 1,
        "capability": 2,
        "responsibility": 1,
    }
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["E_TEST"]
    assert result.truncated is False
    assert result.cursor is None


def test_analysis_scope_scoped_to_package_prefix(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=1)
    facts.insert_test_entities(module_name="other.mod", count=1)

    result = query_service.analysis_scope(scope="pkg")

    assert {partition.module_name for partition in result.partitions} == {"pkg.mod"}


def test_analysis_scope_partitions_page_with_cursor(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    for index in range(3):
        facts.insert_test_entities(module_name=f"pkg.mod{index}", count=1)

    first = query_service.analysis_scope(limit=2)

    assert len(first.partitions) == 2
    assert first.truncated is True
    assert first.cursor is not None

    second = query_service.analysis_scope(cursor=first.cursor, limit=2)

    assert len(second.partitions) == 1
    assert second.truncated is False
    assert first.totals.file_count == 3  # totals never shrink across pages


def test_resolve_entity_context_requires_exactly_one_anchor(
    query_service: QueryService,
) -> None:
    with pytest.raises(ValueError, match="exactly one anchor"):
        query_service.resolve_entity_context()
    with pytest.raises(ValueError, match="exactly one anchor"):
        query_service.resolve_entity_context(entity_uid="ent_x", path="a/b.py")
    with pytest.raises(ValueError, match="exactly one anchor"):
        query_service.resolve_entity_context(path="a/b.py", address="a.b:c")


def test_resolve_entity_context_by_uid(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=1)
    facts.insert_test_relations(source_uid="test_ent_00000", count=2)

    result = query_service.resolve_entity_context(entity_uid="test_ent_00000")

    assert result.anchor_kind == "entity_uid"
    assert [entity.uid for entity in result.entities] == ["test_ent_00000"]
    assert len(result.relations) == 2
    assert result.mappings == ()
    assert result.truncated is False
    assert result.cursor is None


def test_resolve_entity_context_by_path(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=2)

    result = query_service.resolve_entity_context(path="__test__/pkg/mod.py", limit=10)

    assert result.anchor_kind == "path"
    assert len(result.entities) == 2


def test_resolve_entity_context_by_address(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=1)

    result = query_service.resolve_entity_context(address="pkg.mod:test_00000")

    assert [entity.name for entity in result.entities] == ["test_00000"]


def test_resolve_entity_context_filters_relation_types(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=1)
    facts.insert_test_relations(source_uid="test_ent_00000", count=2)

    result = query_service.resolve_entity_context(
        entity_uid="test_ent_00000", relation_types=("imports",)
    )

    assert result.relations == ()
    assert result.relations_truncated is False


def test_resolve_entity_context_rejects_unknown_relation_type(
    query_service: QueryService, facts: FactsDatabase
) -> None:
    facts.insert_test_entities(module_name="pkg.mod", count=1)
    with pytest.raises(ValueError, match="allowlisted"):
        query_service.resolve_entity_context(
            entity_uid="test_ent_00000", relation_types=("hacks",)
        )


def test_resolve_entity_context_maps_formal_entity_refs(
    query_service: QueryService,
) -> None:
    result = query_service.resolve_entity_context(entity_uid=ENTITY_ALPHA)

    assert result.entities == ()
    assert [mapping.entity_uid for mapping in result.mappings] == [ENTITY_ALPHA]
    assert [ref.entity_uid for ref in result.entity_refs] == [ENTITY_ALPHA]
    assert result.entity_refs[0].resolution_status == "resolved"


def test_search_cognitive_graph_ranks_and_bounds(query_service: QueryService) -> None:
    result = query_service.search_cognitive_graph("resume", kinds=("behavior",), limit=10)

    assert [hit.node_id for hit in result.hits] == ["behavior.checkpoint-resume"]
    assert list(result.hits[0].matched_fields) == ["title", "exact_alias", "summary"]
    assert result.truncated is False
    assert result.coordinate.graph_revision == GRAPH_REVISION


def test_search_cognitive_graph_marks_truncation_at_limit(
    query_service: QueryService,
) -> None:
    result = query_service.search_cognitive_graph("journal", limit=1)

    assert len(result.hits) == 1
    assert result.truncated is True


def test_search_cognitive_graph_rejects_unknown_kind(
    query_service: QueryService,
) -> None:
    with pytest.raises(ValueError, match="kind"):
        query_service.search_cognitive_graph("resume", kinds=("unknown",))


def test_discussion_context_reports_truncation(query_service: QueryService) -> None:
    result = query_service.get_discussion_context(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=2,
            max_nodes=3,
            max_entities=4,
            max_evidence=2,
        )
    )
    assert len(result.nodes) <= 3
    assert result.truncated is True


def test_discussion_context_returns_complete_neighborhood(
    query_service: QueryService,
) -> None:
    result = query_service.get_discussion_context(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=1,
            max_nodes=40,
            max_entities=80,
            max_evidence=80,
        )
    )
    assert {node.node_id for node in result.nodes} >= {
        "behavior.checkpoint-resume",
        "capability.source-retrieval",
    }
    assert [flow.behavior_id for flow in result.flows] == [
        "behavior.checkpoint-resume"
    ]
    assert result.mappings
    assert result.entities
    assert result.evidence
    assert result.truncated is False
    assert result.continuation is None
    assert result.graph_revision == GRAPH_REVISION


def test_discussion_context_requires_an_anchor(query_service: QueryService) -> None:
    with pytest.raises(ValueError, match="anchor"):
        query_service.get_discussion_context(ContextRequest())


def test_discussion_context_rejects_stale_expected_revision(
    query_service: QueryService,
) -> None:
    request = ContextRequest(
        node_ids=("behavior.checkpoint-resume",),
        expected_graph_revision=GRAPH_REVISION + 1,
    )
    with pytest.raises(CodeCortexError) as exc:
        query_service.get_discussion_context(request)
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED
