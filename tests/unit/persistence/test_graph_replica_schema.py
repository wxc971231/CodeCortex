"""Cognitive replica schema, connection policy, and search invariants."""

import sqlite3

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.graph import (
    Approval,
    Behavior,
    Capability,
    CognitiveEdge,
    CognitiveGraph,
    EdgeType,
    Responsibility,
)
from codecortex.infrastructure.persistence.graph_replica import (
    GraphReplica,
    normalize_search_terms,
)
from tests.conftest import REPLICA_EVENT_ID, replica_ulid

DESIGN_TABLES = {
    "cognitive_nodes",
    "cognitive_aliases",
    "cognitive_edges",
    "logical_flows",
    "flow_steps",
    "flow_step_capabilities",
    "entity_reference_index",
    "implementation_mappings",
    "cognitive_evidence",
    "history_event_index",
    "cognitive_search_terms",
    "replica_metadata",
}

REQUIRED_INDEXES = {
    ("cognitive_nodes", ("kind", "node_id")),
    ("cognitive_aliases", ("normalized_alias", "node_id")),
    ("cognitive_edges", ("source_node_id", "edge_type")),
    ("cognitive_edges", ("target_node_id", "edge_type")),
    ("logical_flows", ("materialization_status", "behavior_id")),
    ("flow_steps", ("behavior_id", "step_order")),
    ("implementation_mappings", ("subject_kind", "subject_id")),
    ("implementation_mappings", ("entity_uid", "resolution_status")),
    ("cognitive_evidence", ("owner_kind", "owner_id")),
    ("cognitive_evidence", ("entity_uid",)),
    ("cognitive_evidence", ("relative_path",)),
    ("history_event_index", ("resulting_graph_revision", "event_type")),
    ("cognitive_search_terms", ("term", "weight", "node_id")),
}


def _approval() -> Approval:
    return Approval(REPLICA_EVENT_ID)


def _graph(*nodes, revision: int = 1) -> CognitiveGraph:
    """Assemble a valid graph, parenting orphan behaviors automatically."""
    members = list(nodes)
    edges: list[CognitiveEdge] = []
    behaviors = [node for node in members if isinstance(node, Behavior)]
    if behaviors:
        parent = Responsibility(
            id="responsibility.search-parent", title="Search parent", approval=_approval()
        )
        members.insert(0, parent)
        for index, behavior in enumerate(behaviors):
            edges.append(
                CognitiveEdge(
                    id=replica_ulid("edge", f"S{index:02d}"),
                    type=EdgeType.CONTAINS,
                    source_id=parent.id,
                    target_id=behavior.id,
                    epistemic_status="established",
                    approval=_approval(),
                )
            )
    return CognitiveGraph(
        graph_revision=revision, nodes=tuple(members), semantic_edges=tuple(edges)
    )


def _built_replica(tmp_path, graph: CognitiveGraph) -> GraphReplica:
    replica = GraphReplica.create_new(tmp_path / "cognitive.sqlite3")
    replica.rebuild(graph, graph.graph_revision)
    return replica


def test_schema_creates_every_design_table(tmp_path):
    replica = GraphReplica.create_new(tmp_path / "cognitive.sqlite3")

    with replica.open_read() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert DESIGN_TABLES <= tables


def test_schema_defines_every_required_index(tmp_path):
    replica = GraphReplica.create_new(tmp_path / "cognitive.sqlite3")

    missing = {
        (table, columns)
        for table, columns in REQUIRED_INDEXES
        if not replica.has_index(table, columns)
    }

    assert missing == set()
    assert replica.foreign_keys_enabled()


def test_connection_policies_match_fact_cache(tmp_path):
    replica = GraphReplica.create_new(tmp_path / "cognitive.sqlite3")

    with replica.open_read() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")

    with replica.open_write() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000


def test_main_write_rejects_database_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    cache = root / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside.sqlite3"
    with sqlite3.connect(outside) as connection:
        connection.execute("CREATE TABLE outside_marker(value TEXT)")
        connection.execute("INSERT INTO outside_marker VALUES ('unchanged')")
    path = cache / "cognitive.sqlite3"
    path.symlink_to(outside)
    before = outside.read_bytes()
    replica = GraphReplica(path, repository_root=root)

    with pytest.raises(sqlite3.OperationalError, match="unsafe"):
        replica.open_write()

    assert path.is_symlink()
    assert outside.read_bytes() == before


def test_main_reset_rejects_cache_directory_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    (root / ".codecortex").mkdir(parents=True)
    outside_cache = tmp_path / "outside-cache"
    outside_cache.mkdir()
    outside = outside_cache / "cognitive.sqlite3"
    with sqlite3.connect(outside) as connection:
        connection.execute("CREATE TABLE outside_marker(value TEXT)")
        connection.execute("INSERT INTO outside_marker VALUES ('unchanged')")
    (root / ".codecortex" / ".cache").symlink_to(
        outside_cache, target_is_directory=True
    )
    before = outside.read_bytes()
    replica = GraphReplica(
        root / ".codecortex" / ".cache" / "cognitive.sqlite3",
        repository_root=root,
    )

    with pytest.raises(sqlite3.OperationalError, match="unsafe"):
        replica.reset()

    assert outside.read_bytes() == before


def test_main_write_rejects_parent_traversal_outside_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.sqlite3"
    replica = GraphReplica(root / ".." / outside.name, repository_root=root)

    with pytest.raises(sqlite3.OperationalError, match="parent traversal"):
        replica.open_write()

    assert not outside.exists()


def test_enum_checks_are_locked(tmp_path):
    replica = GraphReplica.create_new(tmp_path / "cognitive.sqlite3")

    with replica.open_write() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO cognitive_nodes "
                "(node_id, kind, title, summary, epistemic_status, intent, observed, "
                "node_revision, approval_event_id, graph_revision) "
                "VALUES ('module.x', 'module', 't', '', 'inferred', NULL, '', 1, 'evt_x', 1)"
            )
        connection.execute("ROLLBACK")

    with replica.open_write() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO cognitive_evidence "
                "(evidence_id, owner_kind, owner_id, evidence_kind, entity_uid, "
                "relative_path, start_line, end_line, observation, graph_revision) "
                "VALUES ('evid_x', 'flow', 'behavior.x', 'code_entity', NULL, "
                "'a.py', NULL, NULL, '', 1)"
            )
        connection.execute("ROLLBACK")


def test_normalize_search_terms_latin_nfkc_and_casefold():
    assert normalize_search_terms("Checkpoint Resume") == ("checkpoint", "resume")
    assert normalize_search_terms("ＲＥＳＵＭＥ　Ｖ２") == ("resume", "v2")
    assert "parser" in normalize_search_terms("source_parser.py")


def test_normalize_search_terms_cjk_full_word_and_ngrams():
    terms = normalize_search_terms("源码检索")

    assert "源码检索" in terms
    assert {"源码", "码检", "检索"} <= set(terms)
    assert {"源码检", "码检索"} <= set(terms)


def test_normalize_search_terms_mixed_latin_and_cjk():
    terms = normalize_search_terms("恢复journal方向")

    assert "journal" in terms
    assert "恢复" in terms
    assert "方向" in terms
    assert "恢" not in terms  # bigrams only from runs longer than one character


def test_search_requires_built_replica(tmp_path):
    replica = GraphReplica.create_new(tmp_path / "cognitive.sqlite3")

    with pytest.raises(CodeCortexError) as exc:
        replica.search("resume", (), 10, expected_revision=1)

    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED


def test_search_rejects_revision_mismatch(tmp_path):
    replica = _built_replica(
        tmp_path, _graph(Capability(id="capability.alpha", title="Alpha", approval=_approval()))
    )

    with pytest.raises(CodeCortexError) as exc:
        replica.search("alpha", (), 10, expected_revision=2)

    assert exc.value.code is ErrorCode.CACHE_REBUILD_REQUIRED
    assert exc.value.details["expected_graph_revision"] == 2
    assert exc.value.details["actual_graph_revision"] == 1


def test_search_uses_alias_and_kind_filter(tmp_path):
    graph = _graph(
        Behavior(
            id="behavior.checkpoint-resume",
            title="Checkpoint resume",
            aliases=("resume",),
            approval=_approval(),
        ),
        Capability(
            id="capability.resume-counter",
            title="Resume counter",
            approval=_approval(),
        ),
    )
    replica = _built_replica(tmp_path, graph)

    hits = replica.search("resume", ("behavior",), 10, expected_revision=1)

    assert [hit.node_id for hit in hits] == ["behavior.checkpoint-resume"]
    assert hits[0].kind == "behavior"
    assert "exact_alias" in hits[0].matched_fields


def test_search_weights_title_exact_alias_alias_summary_observed(tmp_path):
    graph = _graph(
        Responsibility(
            id="responsibility.a-title", title="Checkpoint alpha", approval=_approval()
        ),
        Responsibility(
            id="responsibility.b-exact-alias",
            title="Other",
            aliases=("checkpoint",),
            approval=_approval(),
        ),
        Responsibility(
            id="responsibility.c-alias",
            title="Another",
            aliases=("checkpoint helper",),
            approval=_approval(),
        ),
        Responsibility(
            id="responsibility.d-summary",
            title="Plain",
            summary="Handles checkpoint logic.",
            approval=_approval(),
        ),
        Responsibility(
            id="responsibility.e-observed",
            title="Plainest",
            observed="checkpoint 由恢复流程触发",
            approval=_approval(),
        ),
    )
    replica = _built_replica(tmp_path, graph)

    hits = replica.search("checkpoint", (), 10, expected_revision=1)

    assert [hit.node_id for hit in hits] == [
        "responsibility.a-title",
        "responsibility.b-exact-alias",
        "responsibility.c-alias",
        "responsibility.d-summary",
        "responsibility.e-observed",
    ]
    assert [hit.score for hit in hits] == [50, 40, 30, 20, 10]


def test_search_exact_alias_matches_full_multi_word_query(tmp_path):
    graph = _graph(
        Responsibility(
            id="responsibility.a-title", title="Checkpoint alpha", approval=_approval()
        ),
        Responsibility(
            id="responsibility.c-alias",
            title="Another",
            aliases=("checkpoint helper",),
            approval=_approval(),
        ),
    )
    replica = _built_replica(tmp_path, graph)

    hits = replica.search("checkpoint helper", (), 10, expected_revision=1)

    assert hits[0].node_id == "responsibility.c-alias"
    assert "exact_alias" in hits[0].matched_fields


def test_search_tie_breaks_by_kind_then_stable_node_id(tmp_path):
    graph = _graph(
        Capability(
            id="capability.beta-resume",
            title="Resume beta",
            approval=_approval(),
        ),
        Capability(
            id="capability.alpha-resume",
            title="Resume alpha",
            approval=_approval(),
        ),
        Responsibility(
            id="responsibility.resume-docs",
            title="Resume docs",
            approval=_approval(),
        ),
    )
    replica = _built_replica(tmp_path, graph)

    hits = replica.search("resume", (), 10, expected_revision=1)

    assert [hit.node_id for hit in hits] == [
        "capability.alpha-resume",
        "capability.beta-resume",
        "responsibility.resume-docs",
    ]
    assert len({hit.score for hit in hits}) == 1


def test_search_cjk_full_word_and_ngram(tmp_path):
    graph = _graph(
        Capability(
            id="capability.source-retrieval",
            title="Source retrieval",
            aliases=("源码检索",),
            approval=_approval(),
        ),
        Capability(
            id="capability.catalog",
            title="Catalog",
            summary="提供源码检索能力目录",
            approval=_approval(),
        ),
    )
    replica = _built_replica(tmp_path, graph)

    full = replica.search("源码检索", (), 10, expected_revision=1)
    bigram = replica.search("码检", (), 10, expected_revision=1)

    assert full[0].node_id == "capability.source-retrieval"
    assert full[0].score > full[1].score
    assert bigram[0].node_id == "capability.source-retrieval"
    assert "alias" in bigram[0].matched_fields
    assert bigram[1].node_id == "capability.catalog"


def test_search_enforces_limit_and_validates_arguments(tmp_path):
    graph = _graph(
        *[
            Capability(
                id=f"capability.resume-{index:02d}",
                title=f"Resume {index}",
                approval=_approval(),
            )
            for index in range(5)
        ]
    )
    replica = _built_replica(tmp_path, graph)

    hits = replica.search("resume", (), 2, expected_revision=1)

    assert len(hits) == 2
    assert [hit.node_id for hit in hits] == sorted(hit.node_id for hit in hits)
    with pytest.raises(ValueError, match="positive"):
        replica.search("resume", (), 0, expected_revision=1)
    with pytest.raises(ValueError, match="maximum"):
        replica.search("resume", (), GraphReplica.max_limit + 1, expected_revision=1)
    with pytest.raises(ValueError, match="kind"):
        replica.search("resume", ("module",), 10, expected_revision=1)


def test_search_without_terms_returns_no_hits(tmp_path):
    replica = _built_replica(
        tmp_path, _graph(Capability(id="capability.alpha", title="Alpha", approval=_approval()))
    )

    assert replica.search("!!!", (), 10, expected_revision=1) == ()
    assert replica.search("", (), 10, expected_revision=1) == ()
