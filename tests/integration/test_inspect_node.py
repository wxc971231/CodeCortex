"""Current-source navigation tests for M1a node inspection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codecortex.application.query import QueryService
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.graph_replica import ReplicaMetadata

_DIGEST = "sha256:" + "0" * 64
_UID = "ent_01J0000000000000000000000A"


class _FormalStore:
    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state

    def load(self) -> SimpleNamespace:
        return self._state


class _Replica:
    def metadata(self) -> ReplicaMetadata:
        return ReplicaMetadata(replica_schema_version=1, graph_revision=1)


@pytest.fixture
def inspect_service(tmp_path: Path) -> QueryService:
    facts = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
    with facts.open_write() as connection:
        connection.execute(
            "UPDATE cache_metadata SET graph_revision = 1, "
            "repository_source_digest = ? WHERE singleton_id = 1",
            (_DIGEST,),
        )
        connection.execute(
            "INSERT INTO source_files "
            "(relative_path, content_digest, size_bytes, parse_status, is_test) "
            "VALUES ('src/new.py', ?, 1, 'parsed', 0)",
            ("sha256:" + "a" * 64,),
        )
        file_id = connection.execute("SELECT file_id FROM source_files").fetchone()[0]
        connection.execute(
            "INSERT INTO entities "
            "(uid, file_id, address, module_name, qualname, kind, name, "
            "start_line, end_line, signature, fingerprint, resolution_status) "
            "VALUES ('ent_01J0000000000000000000000B', ?, 'pkg.new:answer', "
            "'pkg.new', 'answer', 'function', 'answer', 10, 12, "
            "'(question: str) -> str', ?, 'resolved')",
            (file_id, "sha256:" + "b" * 64),
        )
    state = SimpleNamespace(
        graph=SimpleNamespace(
            graph_revision=1,
            nodes=(
                {
                    "id": "behavior.answer",
                    "kind": "behavior",
                    "title": "Answer",
                },
            ),
            semantic_edges=(),
            logical_flows=(),
            implementation_mappings=(
                {
                    "id": "map_01J00000000000000000000001",
                    "subject_kind": "node",
                    "subject_id": "behavior.answer",
                    "entity_uid": _UID,
                    "role": "primary",
                    "resolution_status": "resolved",
                    "evidence_note": "Answer entry point.",
                },
            ),
        ),
        entity_refs=SimpleNamespace(
            entities=(
                {
                    "uid": _UID,
                    "last_known_address": "pkg.old:answer",
                    "kind": "function",
                    "relative_path": "src/old.py",
                    "signature": "(question: str) -> str",
                    "fingerprint": "sha256:" + "b" * 64,
                    "resolution_status": "resolved",
                },
            )
        ),
    )
    return QueryService(
        formal_store=_FormalStore(state),
        facts=facts,
        replica=_Replica(),
        repository_lock=RepositoryLock(tmp_path),
    )


def test_inspect_resolves_current_location_after_entity_move(
    inspect_service: QueryService,
) -> None:
    result = inspect_service.inspect_node("behavior.answer")

    mapping = result.mappings[0]
    assert mapping.resolution_status == "resolved"
    assert mapping.current_location is not None
    assert mapping.current_location.relative_path == "src/new.py"
    assert mapping.current_location.start_line == 10
    assert mapping.last_known_location.relative_path == "src/old.py"


def test_inspect_preserves_last_known_location_when_unresolved(
    inspect_service: QueryService,
) -> None:
    # A different fingerprint rejects the fallback instead of mutating the
    # formal mapping or guessing an unrelated current entity.
    with inspect_service._facts.open_write() as connection:
        connection.execute("UPDATE entities SET fingerprint = ?", ("sha256:" + "c" * 64,))

    mapping = inspect_service.inspect_node("behavior.answer").mappings[0]

    assert mapping.resolution_status == "missing"
    assert mapping.current_location is None
    assert mapping.last_known_location.relative_path == "src/old.py"


def test_inspect_bounds_nested_collections_and_reports_omissions(inspect_service):
    graph = inspect_service._formal_store.load().graph
    evidence = [{"id": f"evid_{i:03}", "observation": "Observed"} for i in range(20)]
    graph.nodes[0]["evidence"] = evidence
    graph.nodes[0]["aliases"] = [str(i) for i in range(20)]
    graph.semantic_edges = tuple(
        {
            "id": f"edge_{i:03}",
            "source_id": "behavior.answer",
            "target_id": f"capability.other-{i}",
            "evidence": evidence,
        }
        for i in range(20)
    )
    graph.logical_flows = (
        {
            "behavior_id": "behavior.answer",
            "steps": [
                {
                    "id": f"behavior.answer#step.{i}",
                    "order": i,
                    "uses_capabilities": [f"capability.other-{j}" for j in range(20)],
                    "evidence": evidence,
                }
                for i in range(20)
            ],
        },
    )
    template = graph.implementation_mappings[0]
    graph.implementation_mappings = tuple(
        {**template, "id": f"map_{i:03}", "evidence": evidence} for i in range(20)
    )
    inspect_service._max_node_limit = 2
    inspect_service._max_entity_limit = 3
    inspect_service._max_evidence_limit = 4
    result = inspect_service.inspect_node("behavior.answer")
    assert len(result.relations) == 2
    assert len(result.mappings) == 3
    assert len(result.flow["steps"]) == 2
    assert len(result.node["aliases"]) == 2
    nested = [*result.node.get("evidence", ())]
    for owner in (
        *result.relations,
        result.flow,
        *result.flow["steps"],
        *(mapping.mapping for mapping in result.mappings),
    ):
        nested.extend(owner.get("evidence", ()))
    assert len(nested) <= 4
    assert len(result.evidence) <= 4
    assert result.truncated
    assert "max_evidence" in result.truncation_reasons
    assert result.continuation_hints
    assert len(graph.nodes[0]["evidence"]) == 20
