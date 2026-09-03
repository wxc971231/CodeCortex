"""End-to-end cognitive replica rebuild and bounded queries on real databases."""

import base64
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.graph import (
    Approval,
    ImplementationMapping,
    MappingRole,
    MappingSubjectKind,
    ResolutionStatus,
)
from codecortex.infrastructure.persistence.graph_replica import (
    ContextRequest,
    GraphReplica,
)
from tests.conftest import (
    ENTITY_ALPHA,
    ENTITY_BRAVO,
    ENTITY_CHARLIE,
    REPLICA_EVENT_ID,
    replica_ulid,
)

TABLE_DUMP_ORDER = (
    ("cognitive_nodes", "node_id"),
    ("cognitive_aliases", "node_id, position"),
    ("cognitive_edges", "edge_id"),
    ("logical_flows", "behavior_id"),
    ("flow_steps", "step_id"),
    ("flow_step_capabilities", "step_id, capability_id"),
    ("entity_reference_index", "entity_uid"),
    ("implementation_mappings", "mapping_id"),
    ("cognitive_evidence", "evidence_id"),
    ("history_event_index", "event_id"),
    ("cognitive_search_terms", "term, node_id, field"),
    ("replica_metadata", "singleton_id"),
)


def _repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _replica_path(root: Path) -> Path:
    return root / ".codecortex" / ".cache" / "cognitive.sqlite3"


def _replica(root: Path, entity_refs, history_events) -> GraphReplica:
    return GraphReplica.create_new(
        _replica_path(root), entity_refs=entity_refs, history_events=history_events
    )


def _table_dump(replica: GraphReplica) -> dict[str, tuple[tuple, ...]]:
    dump: dict[str, tuple[tuple, ...]] = {}
    with replica.open_read() as connection:
        for table, order_by in TABLE_DUMP_ORDER:
            dump[table] = tuple(
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY {order_by}"
                )
            )
    return dump


def _decode_continuation(continuation: str) -> list[str]:
    padded = continuation + "=" * (-len(continuation) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    assert payload["kind"] == "context"
    return payload["value"]


def test_rebuild_imports_every_graph_section(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)

    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    assert replica.metadata().graph_revision == 1
    with replica.open_read() as connection:

        def count(table: str) -> int:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        assert count("cognitive_nodes") == 4
        assert count("cognitive_aliases") == 3
        assert count("cognitive_edges") == 4
        assert count("logical_flows") == 1
        assert count("flow_steps") == 2
        assert count("flow_step_capabilities") == 2
        assert count("entity_reference_index") == 3
        assert count("implementation_mappings") == 3
        assert count("cognitive_evidence") == 5
        assert count("history_event_index") == 1
        assert count("cognitive_search_terms") > 0
        row = connection.execute(
            "SELECT resolution_status FROM entity_reference_index WHERE entity_uid = ?",
            (ENTITY_CHARLIE,),
        ).fetchone()
        assert row[0] == "ambiguous"
        revision_tables = (
            "cognitive_nodes",
            "cognitive_edges",
            "logical_flows",
            "flow_steps",
            "entity_reference_index",
            "implementation_mappings",
            "cognitive_evidence",
        )
        revisions = {
            row[0]
            for table in revision_tables
            for row in connection.execute(f"SELECT graph_revision FROM {table}")
        }
        assert revisions == {1}


def test_rebuild_is_all_or_nothing_for_invalid_graph(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)
    before = _table_dump(replica)

    dangling = ImplementationMapping(
        id=replica_ulid("map", "M09"),
        subject_kind=MappingSubjectKind.NODE,
        subject_id="behavior.does-not-exist",
        entity_uid=ENTITY_ALPHA,
        role=MappingRole.PRIMARY,
        resolution_status=ResolutionStatus.RESOLVED,
        approval=Approval(REPLICA_EVENT_ID),
    )
    invalid_graph = replace(
        discussion_graph,
        graph_revision=2,
        implementation_mappings=(*discussion_graph.implementation_mappings, dangling),
    )
    with pytest.raises(CodeCortexError) as exc:
        replica.rebuild(invalid_graph, 2)

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
    assert "DANGLING_MAPPING_SUBJECT" in {
        violation["code"] for violation in exc.value.details["violations"]
    }
    assert replica.metadata().graph_revision == 1
    assert _table_dump(replica) == before


def test_rebuild_rejects_incomplete_entity_refs(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)
    before = _table_dump(replica)

    incomplete = tuple(
        record for record in replica_entity_refs if record.uid != ENTITY_CHARLIE
    )
    stale = _replica(root, incomplete, replica_history_events)
    refreshed = replace(discussion_graph, graph_revision=2)
    with pytest.raises(CodeCortexError) as exc:
        stale.rebuild(refreshed, 2)

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
    assert exc.value.details["missing_entity_uids"] == [ENTITY_CHARLIE]
    assert replica.metadata().graph_revision == 1
    assert _table_dump(replica) == before


def test_rebuild_rejects_graph_revision_argument_mismatch(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)

    with pytest.raises(CodeCortexError) as exc:
        replica.rebuild(discussion_graph, discussion_graph.graph_revision + 1)

    assert exc.value.code is ErrorCode.GRAPH_REVISION_CONFLICT
    with pytest.raises(CodeCortexError) as read_exc:
        replica.search("resume", (), 10, expected_revision=1)
    assert read_exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_repeated_rebuild_is_deterministic(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)

    replica.rebuild(discussion_graph, discussion_graph.graph_revision)
    first = _table_dump(replica)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)
    second = _table_dump(replica)

    assert first == second


def test_context_traversal_returns_complete_bounded_discussion(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    context = replica.context(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=1,
            expected_graph_revision=1,
        )
    )

    assert context.graph_revision == 1
    assert [node.node_id for node in context.nodes] == [
        "behavior.checkpoint-resume",
        "capability.journal-recovery",
        "capability.source-retrieval",
        "responsibility.repository-understanding",
    ]
    behavior_node = context.nodes[0]
    assert behavior_node.aliases == ("resume",)
    assert behavior_node.observed == "恢复流程先校验 journal 再选择方向"
    assert {edge.edge_id for edge in context.edges} == {
        replica_ulid("edge", "E01"),
        replica_ulid("edge", "E02"),
        replica_ulid("edge", "E03"),
        replica_ulid("edge", "E04"),
    }
    assert [flow.behavior_id for flow in context.flows] == ["behavior.checkpoint-resume"]
    steps = context.flows[0].steps
    assert [step.order for step in steps] == [1, 2]
    assert steps[0].uses_capabilities == ("capability.journal-recovery",)
    assert [mapping.mapping_id for mapping in context.mappings] == [
        replica_ulid("map", "M01"),
        replica_ulid("map", "M02"),
        replica_ulid("map", "M03"),
    ]
    assert [entity.entity_uid for entity in context.entities] == [
        ENTITY_ALPHA,
        ENTITY_BRAVO,
        ENTITY_CHARLIE,
    ]
    assert len(context.evidence) == 5
    assert context.truncated is False
    assert context.continuation is None


def test_context_depth_zero_returns_only_anchors(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    context = replica.context(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=0,
            expected_graph_revision=1,
        )
    )

    assert [node.node_id for node in context.nodes] == ["behavior.checkpoint-resume"]
    assert context.edges == ()
    assert len(context.flows) == 1
    assert len(context.mappings) == 3
    assert context.truncated is False


def test_context_limits_report_truncation_and_continuation(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    context = replica.context(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=2,
            max_nodes=3,
            expected_graph_revision=1,
        )
    )

    assert [node.node_id for node in context.nodes] == [
        "behavior.checkpoint-resume",
        "capability.journal-recovery",
        "capability.source-retrieval",
    ]
    assert context.truncated is True
    assert context.continuation is not None
    assert _decode_continuation(context.continuation) == [
        "responsibility.repository-understanding"
    ]

    capped = replica.context(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=1,
            max_entities=1,
            max_evidence=2,
            expected_graph_revision=1,
        )
    )

    assert [entity.entity_uid for entity in capped.entities] == [ENTITY_ALPHA]
    assert len(capped.evidence) == 2
    assert capped.truncated is True


def test_context_entity_anchor_resolves_owning_nodes(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    context = replica.context(
        ContextRequest(
            entity_uids=(ENTITY_BRAVO,), depth=0, expected_graph_revision=1
        )
    )

    assert [node.node_id for node in context.nodes] == ["behavior.checkpoint-resume"]


def test_context_requires_anchors_and_bounded_arguments(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    with pytest.raises(ValueError, match="anchor"):
        replica.context(ContextRequest())
    with pytest.raises(ValueError, match="depth"):
        replica.context(
            ContextRequest(node_ids=("behavior.checkpoint-resume",), depth=-1)
        )
    with pytest.raises(ValueError, match="max_nodes"):
        replica.context(
            ContextRequest(node_ids=("behavior.checkpoint-resume",), max_nodes=0)
        )
    with pytest.raises(CodeCortexError) as exc:
        replica.context(
            ContextRequest(
                node_ids=("behavior.checkpoint-resume",), expected_graph_revision=9
            )
        )
    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED
    with pytest.raises(CodeCortexError) as unknown:
        replica.context(ContextRequest(node_ids=("behavior.unknown",)))
    assert unknown.value.code is ErrorCode.INVALID_ID


def test_context_uses_a_fixed_number_of_queries(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events, monkeypatch
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    def count_selects(request: ContextRequest) -> int:
        statements: list[str] = []
        with replica.open_read() as connection:
            monkeypatch.setattr(replica, "open_read", lambda: connection)
            connection.set_trace_callback(statements.append)
            replica.context(request)
            connection.set_trace_callback(None)
        monkeypatch.undo()
        return len(
            [
                statement
                for statement in statements
                if statement.lstrip().upper().startswith("SELECT")
            ]
        )

    narrow = count_selects(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=1,
            max_nodes=3,
            expected_graph_revision=1,
        )
    )
    wide = count_selects(
        ContextRequest(
            node_ids=("behavior.checkpoint-resume",),
            depth=1,
            max_nodes=40,
            expected_graph_revision=1,
        )
    )

    assert narrow == wide
    assert narrow <= 16


def test_search_end_to_end_cjk_and_alias(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    cjk = replica.search("源码检索", (), 10, expected_revision=1)
    alias = replica.search("resume", ("behavior",), 10, expected_revision=1)

    assert cjk[0].node_id == "capability.source-retrieval"
    assert [hit.node_id for hit in alias] == ["behavior.checkpoint-resume"]


def test_context_ignores_empty_anchor_strings(
    tmp_path, discussion_graph, replica_entity_refs, replica_history_events
):
    root = _repository(tmp_path)
    replica = _replica(root, replica_entity_refs, replica_history_events)
    replica.rebuild(discussion_graph, discussion_graph.graph_revision)

    with pytest.raises(ValueError, match="non-empty"):
        replica.context(ContextRequest(node_ids=("",)))
