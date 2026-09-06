"""Boundary and strictness tests for the bounded M1a AnalysisReport."""

from __future__ import annotations

import pytest

from codecortex.domain.analysis import (
    AnalysisLimits,
    AnalysisReport,
    validate_analysis_report,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from tests.conftest import (
    analysis_report_bytes,
    analysis_report_dict,
    analysis_ulid,
)

REVISION = 3
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def _node(node_id: str = "capability.source-retrieval", **overrides: object) -> dict:
    kind = node_id.split(".", 1)[0]
    node = {"id": node_id, "kind": kind, "title": "Source retrieval"}
    node.update(overrides)
    return node


def _edge(edge_id: str | None = None, **overrides: object) -> dict:
    edge = {
        "id": edge_id or analysis_ulid("edge", 1),
        "type": "uses",
        "source_id": "behavior.repository-qa",
        "target_id": "capability.source-retrieval",
    }
    edge.update(overrides)
    return edge


def _flow(**overrides: object) -> dict:
    flow = {
        "behavior_id": "behavior.repository-qa",
        "materialization_status": "materialized",
        "steps": [
            {
                "id": "behavior.repository-qa#step.resolve-scope",
                "order": 1,
                "title": "Resolve scope",
            }
        ],
    }
    flow.update(overrides)
    return flow


def _mapping(map_id: str | None = None, **overrides: object) -> dict:
    mapping = {
        "id": map_id or analysis_ulid("map", 1),
        "subject_kind": "node",
        "subject_id": "capability.source-retrieval",
        "entity_uid": analysis_ulid("ent", 7),
        "role": "primary",
        "resolution_status": "resolved",
    }
    mapping.update(overrides)
    return mapping


def _evidence(evid_id: str | None = None, **overrides: object) -> dict:
    item = {
        "id": evid_id or analysis_ulid("evid", 1),
        "kind": "code_entity",
        "entity_uid": analysis_ulid("ent", 7),
        "relative_path": "src/codecortex/application/query.py",
        "start_line": 20,
        "end_line": 85,
        "observation": "按节点范围组织查询上下文",
    }
    item.update(overrides)
    return item


def _validate(report: dict, **overrides: object) -> AnalysisReport:
    return validate_analysis_report(
        analysis_report_bytes(report),
        int(overrides.get("expected_graph_revision", REVISION)),
        str(overrides.get("expected_source_digest", DIGEST)),
    )


def _valid_report(**overrides: object) -> dict:
    report = analysis_report_dict(
        base_graph_revision=REVISION, analyzed_source_digest=DIGEST
    )
    report.update(overrides)
    return report


def _invalid(report: dict, **expected: object) -> CodeCortexError:
    with pytest.raises(CodeCortexError) as exc:
        _validate(report, **expected)
    return exc.value


def test_minimal_report_with_empty_candidates_passes() -> None:
    report = _validate(_valid_report())

    assert isinstance(report, AnalysisReport)
    assert report.schema_version == 1
    assert report.base_graph_revision == REVISION
    assert report.analyzed_source_digest == DIGEST
    assert report.analysis_scope.mode == "repository"
    assert report.coverage.analyzed_partitions == ("src/codecortex",)
    assert report.candidate_nodes == ()


def test_full_report_with_every_candidate_kind_passes() -> None:
    node = _node(
        aliases=("源码检索",),
        summary="定位真实实现",
        epistemic_status="inferred",
        intent="帮助回答实现位置",
        observed="当前先缩小认知范围",
        evidence=[_evidence()],
    )
    report = _validate(
        _valid_report(
            candidate_nodes=[
                _node("responsibility.repository-understanding"),
                _node("behavior.repository-qa"),
                node,
            ],
            candidate_edges=[_edge()],
            candidate_flows=[_flow()],
            candidate_mappings=[_mapping()],
            evidence=[_evidence(analysis_ulid("evid", 2))],
            uncertainties=["pkg/legacy 的行为不确定"],
            unmapped_regions=["scripts/ 未映射"],
            diagnostics=["pkg/b.py: 解析跳过"],
        )
    )

    assert len(report.candidate_nodes) == 3
    assert report.candidate_nodes[2].id == "capability.source-retrieval"
    assert report.candidate_edges[0].source_id == "behavior.repository-qa"
    assert report.candidate_flows[0].steps[0].order == 1
    assert report.candidate_mappings[0].entity_uid == analysis_ulid("ent", 7)
    assert report.evidence[0].id == analysis_ulid("evid", 2)
    assert report.uncertainties == ("pkg/legacy 的行为不确定",)
    assert report.unmapped_regions == ("scripts/ 未映射",)
    assert report.diagnostics == ("pkg/b.py: 解析跳过",)


def test_explicit_change_operations_are_typed_and_bound_to_candidates() -> None:
    report = _validate(
        _valid_report(
            candidate_edges=[_edge()],
            change_operations=[
                {
                    "kind": "update_edge",
                    "target_id": analysis_ulid("edge", 1),
                    "before_revision": 2,
                    "change_kind": "move",
                    "change_group": "relocate-retrieval",
                }
            ],
        )
    )

    assert report.change_operations is not None
    operation = report.change_operations[0]
    assert operation.kind == "update_edge"
    assert operation.before_revision == 2
    assert operation.change_kind == "move"
    assert operation.change_group == "relocate-retrieval"


@pytest.mark.parametrize(
    "change_operations",
    [
        [
            {
                "kind": "update_node",
                "target_id": "capability.source-retrieval",
                "before_revision": None,
                "change_kind": "update",
                "change_group": "update-retrieval",
            }
        ],
        [
            {
                "kind": "remove_node",
                "target_id": "capability.source-retrieval",
                "before_revision": 1,
                "change_kind": "remove",
                "change_group": "remove-retrieval",
            },
            {
                "kind": "remove_node",
                "target_id": "capability.source-retrieval",
                "before_revision": 1,
                "change_kind": "remove",
                "change_group": "remove-retrieval-again",
            },
        ],
        [
            {
                "kind": "add_node",
                "target_id": "capability.source-retrieval",
                "before_revision": None,
                "change_kind": "merge",
                "change_group": "merge-retrieval",
            }
        ],
        [
            {
                "kind": "add_node",
                "target_id": "capability.source-retrieval",
                "before_revision": None,
                "change_kind": "remove",
                "change_group": "contradictory-label",
            }
        ],
    ],
)
def test_explicit_change_operations_reject_incomplete_preconditions_and_groups(
    change_operations: list[dict[str, object]],
) -> None:
    error = _invalid(
        _valid_report(
            candidate_nodes=[_node()],
            change_operations=change_operations,
        )
    )

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_structural_change_groups_expand_only_to_explicit_primitives() -> None:
    edge_id = analysis_ulid("edge", 30)
    mapping_id = analysis_ulid("map", 30)
    report = _validate(
        _valid_report(
            candidate_nodes=[
                _node("capability.merge-target"),
                _node("capability.split-source"),
                _node("capability.split-result"),
            ],
            candidate_edges=[_edge(edge_id)],
            candidate_mappings=[_mapping(mapping_id)],
            change_operations=[
                {
                    "kind": "update_node",
                    "target_id": "capability.merge-target",
                    "before_revision": 1,
                    "change_kind": "merge",
                    "change_group": "merge-capabilities",
                },
                {
                    "kind": "remove_node",
                    "target_id": "capability.merge-source",
                    "before_revision": 1,
                    "change_kind": "merge",
                    "change_group": "merge-capabilities",
                },
                {
                    "kind": "update_node",
                    "target_id": "capability.split-source",
                    "before_revision": 1,
                    "change_kind": "split",
                    "change_group": "split-capability",
                },
                {
                    "kind": "add_node",
                    "target_id": "capability.split-result",
                    "before_revision": None,
                    "change_kind": "split",
                    "change_group": "split-capability",
                },
                {
                    "kind": "update_edge",
                    "target_id": edge_id,
                    "before_revision": 1,
                    "change_kind": "move",
                    "change_group": "move-behavior",
                },
                {
                    "kind": "update_mapping",
                    "target_id": mapping_id,
                    "before_revision": 1,
                    "change_kind": "conflict",
                    "change_group": "resolve-user-intent",
                },
            ],
        )
    )

    assert {operation.change_kind.value for operation in report.change_operations or ()} == {
        "move",
        "merge",
        "split",
        "conflict",
    }


def test_explicit_flow_removal_requires_no_candidate_value() -> None:
    report = _validate(
        _valid_report(
            change_operations=[
                {
                    "kind": "remove_logical_flow",
                    "target_id": "behavior.repository-qa",
                    "before_revision": 2,
                    "change_kind": "remove",
                    "change_group": "remove-obsolete-flow",
                }
            ]
        )
    )

    assert report.candidate_flows == ()
    assert report.change_operations is not None
    assert report.change_operations[0].kind == "remove_logical_flow"


def test_explicit_report_rejects_unreferenced_or_missing_candidate_values() -> None:
    unreferenced = _invalid(
        _valid_report(candidate_nodes=[_node()], change_operations=[])
    )
    missing = _invalid(
        _valid_report(
            change_operations=[
                {
                    "kind": "update_node",
                    "target_id": "capability.source-retrieval",
                    "before_revision": 1,
                    "change_kind": "update",
                    "change_group": "update-retrieval",
                }
            ]
        )
    )

    assert unreferenced.code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert missing.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_default_limits_match_design_cap() -> None:
    limits = AnalysisLimits()

    assert limits.serialized_bytes == 512 * 1024
    assert limits.nodes == 300
    assert limits.edges == 1000
    assert limits.mappings == 2000
    assert limits.evidence == 2000
    assert limits.description_codepoints == 240


def test_payload_at_exactly_512_kib_passes() -> None:
    payload = analysis_report_bytes(_valid_report())
    padded = payload + b" " * (512 * 1024 - len(payload))

    assert len(padded) == 512 * 1024
    report = validate_analysis_report(padded, REVISION, DIGEST)
    assert report.base_graph_revision == REVISION


def test_payload_over_512_kib_is_rejected_before_parsing() -> None:
    payload = analysis_report_bytes(_valid_report())
    oversized = payload + b" " * (512 * 1024 - len(payload) + 1)

    error = _invalid_bytes(oversized)
    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def _invalid_bytes(payload: bytes) -> CodeCortexError:
    with pytest.raises(CodeCortexError) as exc:
        validate_analysis_report(payload, REVISION, DIGEST)
    return exc.value


@pytest.mark.parametrize(
    ("collection", "factory", "limit"),
    [
        ("candidate_nodes", lambda i: _node(f"capability.candidate-{i:04d}"), 300),
        ("candidate_edges", lambda i: _edge(analysis_ulid("edge", i + 1)), 1000),
        ("candidate_mappings", lambda i: _mapping(analysis_ulid("map", i + 1)), 2000),
        ("evidence", lambda i: _evidence(analysis_ulid("evid", i + 1)), 2000),
    ],
)
def test_collection_at_exact_maximum_passes(collection, factory, limit) -> None:
    report = _valid_report(**{collection: [factory(i) for i in range(limit)]})

    parsed = _validate(report)

    assert len(getattr(parsed, collection)) == limit


@pytest.mark.parametrize(
    ("collection", "factory", "limit"),
    [
        ("candidate_nodes", lambda i: _node(f"capability.candidate-{i:04d}"), 300),
        ("candidate_edges", lambda i: _edge(analysis_ulid("edge", i + 1)), 1000),
        ("candidate_mappings", lambda i: _mapping(analysis_ulid("map", i + 1)), 2000),
        ("evidence", lambda i: _evidence(analysis_ulid("evid", i + 1)), 2000),
    ],
)
def test_collection_above_maximum_fails(collection, factory, limit) -> None:
    report = _valid_report(**{collection: [factory(i) for i in range(limit + 1)]})

    error = _invalid(report)

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_embedded_evidence_counts_toward_the_total_cap() -> None:
    # 2000 full-size evidence items would exceed the 512 KiB byte cap first
    # (the byte cap dominates by design), so the counter is exercised with a
    # small explicit evidence limit instead.
    limits = AnalysisLimits(evidence=10)

    def _payload(embedded: int, top_level: int) -> bytes:
        mappings = [
            _mapping(
                analysis_ulid("map", i + 1),
                evidence=[_evidence(analysis_ulid("evid", i + 1))],
            )
            for i in range(embedded)
        ]
        report = _valid_report(
            candidate_mappings=mappings,
            evidence=[
                _evidence(analysis_ulid("evid", 100 + i)) for i in range(top_level)
            ],
        )
        return analysis_report_bytes(report)

    within = _payload(embedded=9, top_level=1)
    beyond = _payload(embedded=9, top_level=2)

    parsed = validate_analysis_report(within, REVISION, DIGEST, limits=limits)
    assert len(parsed.candidate_mappings) == 9
    with pytest.raises(CodeCortexError) as exc:
        validate_analysis_report(beyond, REVISION, DIGEST, limits=limits)
    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("field", ["summary", "intent", "observed"])
def test_node_text_at_240_codepoints_passes(field: str) -> None:
    report = _valid_report(candidate_nodes=[_node(**{field: "认" * 240})])

    assert _validate(report).candidate_nodes


@pytest.mark.parametrize("field", ["summary", "intent", "observed"])
def test_node_text_at_241_codepoints_fails(field: str) -> None:
    report = _valid_report(candidate_nodes=[_node(**{field: "认" * 241})])

    error = _invalid(report)

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize(
    ("collection", "entry"),
    [
        ("candidate_mappings", _mapping(evidence_note="长" * 241)),
        ("evidence", _evidence(observation="长" * 241)),
        ("uncertainties", "长" * 241),
        ("unmapped_regions", "长" * 241),
        ("diagnostics", "长" * 241),
        ("candidate_flows", _flow(steps=[{"id": "behavior.repository-qa#step.resolve-scope", "order": 1, "title": "s", "summary": "长" * 241}])),
    ],
)
def test_prose_beyond_240_codepoints_fails(collection, entry) -> None:
    report = _valid_report(**{collection: [entry]})

    error = _invalid(report)

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "base_graph_revision",
        "analyzed_source_digest",
        "analysis_scope",
        "coverage",
        "candidate_nodes",
        "candidate_edges",
        "candidate_flows",
        "candidate_mappings",
        "evidence",
        "uncertainties",
        "unmapped_regions",
        "diagnostics",
    ],
)
def test_missing_required_top_level_field_fails(missing: str) -> None:
    report = _valid_report()
    del report[missing]

    error = _invalid(report)

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_unknown_top_level_field_fails_closed() -> None:
    report = _valid_report(extra_field="surprise")

    error = _invalid(report)

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_non_object_payload_fails() -> None:
    assert _invalid_bytes(b"[1, 2, 3]").code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert _invalid_bytes(b"\xff\xfe{}").code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert _invalid_bytes(b"{not json").code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_unsupported_schema_version_fails() -> None:
    error = _invalid(_valid_report(schema_version=2))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("digest", ["sha256:zz", "md5:" + "a" * 32, 42, None])
def test_malformed_source_digest_fails(digest) -> None:
    error = _invalid(_valid_report(analyzed_source_digest=digest))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_graph_revision_mismatch_is_stale() -> None:
    with pytest.raises(CodeCortexError) as exc:
        _validate(_valid_report(), expected_graph_revision=REVISION + 1)

    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_source_digest_mismatch_is_stale() -> None:
    with pytest.raises(CodeCortexError) as exc:
        _validate(_valid_report(), expected_source_digest=OTHER_DIGEST)

    assert exc.value.code is ErrorCode.PROPOSAL_STALE


@pytest.mark.parametrize(
    "node_id",
    ["Capability.source-retrieval", "capability.Source-Retrieval", "capability.", "module.x"],
)
def test_malformed_node_id_fails(node_id: str) -> None:
    node = _node()
    node["id"] = node_id
    node["kind"] = "capability"

    error = _invalid(_valid_report(candidate_nodes=[node]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_node_kind_must_match_id_namespace() -> None:
    error = _invalid(_valid_report(candidate_nodes=[_node(kind="behavior")]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("kind", ["module", "project", "behavior "])
def test_unknown_node_kind_fails(kind: str) -> None:
    node = _node()
    node["kind"] = kind

    error = _invalid(_valid_report(candidate_nodes=[node]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_candidate_node_cannot_claim_established_status() -> None:
    error = _invalid(
        _valid_report(candidate_nodes=[_node(epistemic_status="established")])
    )

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_duplicate_candidate_node_ids_fail() -> None:
    error = _invalid(_valid_report(candidate_nodes=[_node(), _node()]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("edge_id", ["edge_short", "map_" + "0" * 26, "EDGE_" + "0" * 26])
def test_malformed_edge_id_fails(edge_id: str) -> None:
    error = _invalid(_valid_report(candidate_edges=[_edge(edge_id)]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("edge_type", ["calls", "CONTAINS ", "inherits"])
def test_unknown_edge_type_fails(edge_type: str) -> None:
    error = _invalid(_valid_report(candidate_edges=[_edge(type=edge_type)]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("map_id", ["map_nope", "evid_" + "0" * 26])
def test_malformed_mapping_id_fails(map_id: str) -> None:
    error = _invalid(_valid_report(candidate_mappings=[_mapping(map_id)]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize(
    ("subject_kind", "subject_id"),
    [
        ("file", "capability.source-retrieval"),
        ("node", "not a node id"),
        ("flow_step", "capability.source-retrieval#step.x"),
        ("flow_step", "behavior.repository-qa#other.x"),
    ],
)
def test_malformed_mapping_subject_fails(subject_kind: str, subject_id: str) -> None:
    mapping = _mapping(subject_kind=subject_kind, subject_id=subject_id)

    error = _invalid(_valid_report(candidate_mappings=[mapping]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_malformed_entity_uid_fails() -> None:
    error = _invalid(_valid_report(candidate_mappings=[_mapping(entity_uid="ent_xyz")]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize(
    "path",
    ["../secret.py", "/abs/x.py", "a\\b.py", "./x.py", "", "a//b.py"],
)
def test_malformed_evidence_path_fails(path: str) -> None:
    error = _invalid(_valid_report(evidence=[_evidence(relative_path=path)]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_line": 0},
        {"start_line": 5, "end_line": 4},
        {"start_line": 5, "end_line": None},
        {"start_line": None, "end_line": 6},
        {"start_line": "5", "end_line": 6},
    ],
)
def test_malformed_evidence_line_range_fails(overrides) -> None:
    item = _evidence()
    for key, value in overrides.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value

    error = _invalid(_valid_report(evidence=[item]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_code_entity_evidence_requires_entity_uid() -> None:
    item = _evidence()
    del item["entity_uid"]

    error = _invalid(_valid_report(evidence=[item]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_document_evidence_needs_no_entity_uid() -> None:
    item = {
        "id": analysis_ulid("evid", 9),
        "kind": "repository_document",
        "relative_path": "docs/architecture.md",
        "observation": "架构总览",
    }

    report = _validate(_valid_report(evidence=[item]))

    assert report.evidence[0].kind == "repository_document"


def test_duplicate_evidence_ids_fail() -> None:
    item = _evidence()

    error = _invalid(_valid_report(evidence=[item, dict(item)]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_flow_step_ids_and_order_are_enforced() -> None:
    bad_prefix = _flow(
        steps=[{"id": "behavior.other#step.x", "order": 1, "title": "X"}]
    )
    bad_order = _flow(
        steps=[
            {"id": "behavior.repository-qa#step.a", "order": 1, "title": "A"},
            {"id": "behavior.repository-qa#step.b", "order": 3, "title": "B"},
        ]
    )

    assert _invalid(_valid_report(candidate_flows=[bad_prefix])).code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert _invalid(_valid_report(candidate_flows=[bad_order])).code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_duplicate_flow_owner_fails() -> None:
    error = _invalid(_valid_report(candidate_flows=[_flow(), _flow()]))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


@pytest.mark.parametrize("mode", ["workspace", "partition", "REPOSITORY"])
def test_unknown_scope_mode_fails(mode: str) -> None:
    report = _valid_report()
    report["analysis_scope"]["mode"] = mode

    error = _invalid(report)

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_scope_counts_must_be_non_negative_integers() -> None:
    report = _valid_report()
    report["analysis_scope"]["files"] = -1
    assert _invalid(report).code is ErrorCode.ANALYSIS_REPORT_INVALID

    report = _valid_report()
    report["analysis_scope"]["modules"] = True
    assert _invalid(report).code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_boolean_revisions_are_rejected() -> None:
    error = _invalid(_valid_report(base_graph_revision=True))

    assert error.code is ErrorCode.ANALYSIS_REPORT_INVALID
