"""Unit tests for M1a patch operations and analysis-report conversion."""

from __future__ import annotations

from dataclasses import replace

import pytest

from codecortex.application.proposals import (
    affected_nodes_from_report,
    apply_operations_to_graph,
    operations_from_report,
)
from codecortex.domain.analysis import (
    AnalysisChangeKind,
    AnalysisChangeOperation,
    AnalysisCoverage,
    AnalysisOperationKind,
    AnalysisReport,
    AnalysisScope,
)
from codecortex.domain.cognition import CognitiveGraph
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.graph import (
    Behavior,
    CognitiveEdge,
    EdgeType,
    Evidence,
    FlowStep,
    ImplementationMapping,
    LogicalFlow,
    MappingRole,
    MappingSubjectKind,
    MaterializationStatus,
    ResolutionStatus,
    Responsibility,
)
from codecortex.domain.proposals import (
    PatchOperation,
    PatchOperationKind,
    canonical_patch_digest,
)
from tests.conftest import analysis_ulid

EVENT_ID = analysis_ulid("evt", 1)
EDGE_ID = analysis_ulid("edge", 1)
MAP_ID = analysis_ulid("map", 1)
ENTITY_UID = analysis_ulid("ent", 1)
EVIDENCE_ID = analysis_ulid("evid", 1)

FLOW_VALUE = {
    "behavior_id": "behavior.answer-question",
    "materialization_status": "materialized",
    "steps": [
        {
            "id": "behavior.answer-question#step.transform",
            "order": 1,
            "title": "Transform the answer",
        }
    ],
    "evidence": [],
}
MAPPING_VALUE = {
    "id": MAP_ID,
    "subject_kind": "node",
    "subject_id": "behavior.answer-question",
    "entity_uid": ENTITY_UID,
    "role": "primary",
    "resolution_status": "resolved",
    "evidence_note": "实现入口",
    "evidence": [],
}


def _invalid(excinfo: pytest.ExceptionInfo[CodeCortexError]) -> None:
    assert excinfo.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_set_logical_flow_targets_a_behavior_and_matches_value() -> None:
    operation = PatchOperation(
        "set_logical_flow", "behavior.answer-question", FLOW_VALUE
    )
    assert operation.kind is PatchOperationKind.SET_LOGICAL_FLOW


def test_set_logical_flow_rejects_a_non_behavior_target() -> None:
    with pytest.raises(CodeCortexError) as excinfo:
        PatchOperation("set_logical_flow", "capability.retrieval", FLOW_VALUE)
    _invalid(excinfo)


def test_set_logical_flow_requires_matching_behavior_id_value() -> None:
    with pytest.raises(CodeCortexError) as excinfo:
        PatchOperation(
            "set_logical_flow",
            "behavior.answer-question",
            {**FLOW_VALUE, "behavior_id": "behavior.other"},
        )
    _invalid(excinfo)
    with pytest.raises(CodeCortexError) as excinfo:
        PatchOperation(
            "set_logical_flow",
            "behavior.answer-question",
            {"id": "behavior.answer-question"},
        )
    _invalid(excinfo)


def test_set_logical_flow_with_none_value_removes_the_flow() -> None:
    operation = PatchOperation("set_logical_flow", "behavior.answer-question", None)
    assert operation.value is None


def test_mapping_operations_use_the_mapping_namespace() -> None:
    operation = PatchOperation("add_mapping", MAP_ID, MAPPING_VALUE)
    assert operation.kind is PatchOperationKind.ADD_MAPPING

    with pytest.raises(CodeCortexError) as excinfo:
        PatchOperation("add_mapping", EDGE_ID, MAPPING_VALUE)
    assert excinfo.value.code is ErrorCode.INVALID_ID

    with pytest.raises(CodeCortexError) as excinfo:
        PatchOperation(
            "add_mapping", MAP_ID, {**MAPPING_VALUE, "id": analysis_ulid("map", 2)}
        )
    _invalid(excinfo)


def test_remove_mapping_carries_no_value() -> None:
    operation = PatchOperation("remove_mapping", MAP_ID, None)
    assert operation.kind is PatchOperationKind.REMOVE_MAPPING
    with pytest.raises(CodeCortexError) as excinfo:
        PatchOperation("remove_mapping", MAP_ID, MAPPING_VALUE)
    _invalid(excinfo)


def test_canonical_patch_digest_covers_the_new_operations() -> None:
    operations = (
        PatchOperation("set_logical_flow", "behavior.answer-question", FLOW_VALUE),
        PatchOperation("add_mapping", MAP_ID, MAPPING_VALUE),
    )
    again = (
        PatchOperation("set_logical_flow", "behavior.answer-question", FLOW_VALUE),
        PatchOperation("add_mapping", MAP_ID, MAPPING_VALUE),
    )
    changed = (
        PatchOperation("set_logical_flow", "behavior.answer-question", FLOW_VALUE),
        PatchOperation(
            "add_mapping", MAP_ID, {**MAPPING_VALUE, "role": "supporting"}
        ),
    )
    assert canonical_patch_digest(operations) == canonical_patch_digest(again)
    assert canonical_patch_digest(operations) != canonical_patch_digest(changed)


def _report() -> AnalysisReport:
    return AnalysisReport(
        schema_version=1,
        base_graph_revision=3,
        analyzed_source_digest="sha256:" + "0" * 64,
        analysis_scope=AnalysisScope(mode="repository", files=2, modules=1),
        coverage=AnalysisCoverage(
            analyzed_partitions=("pkg",), unexamined_partitions=()
        ),
        candidate_nodes=(
            Responsibility(id="responsibility.answering", title="Answering"),
            Behavior(
                id="behavior.answer-question",
                title="Answer questions",
                summary="回答用户问题",
                evidence=(
                    Evidence(
                        id=EVIDENCE_ID,
                        kind="code_entity",
                        entity_uid=ENTITY_UID,
                        relative_path="pkg/a.py",
                        start_line=1,
                        end_line=2,
                        observation="实现",
                    ),
                ),
            ),
        ),
        candidate_edges=(
            CognitiveEdge(
                id=EDGE_ID,
                type=EdgeType.CONTAINS,
                source_id="responsibility.answering",
                target_id="behavior.answer-question",
                epistemic_status="established",
            ),
        ),
        candidate_flows=(
            LogicalFlow(
                behavior_id="behavior.answer-question",
                materialization_status=MaterializationStatus.MATERIALIZED,
                flow_revision=1,
                steps=(
                    FlowStep(
                        id="behavior.answer-question#step.transform",
                        order=1,
                        title="Transform",
                        uses_capabilities=("capability.text-transform",),
                    ),
                ),
            ),
        ),
        candidate_mappings=(
            ImplementationMapping(
                id=MAP_ID,
                subject_kind=MappingSubjectKind.NODE,
                subject_id="behavior.answer-question",
                entity_uid=ENTITY_UID,
                role=MappingRole.PRIMARY,
                resolution_status=ResolutionStatus.RESOLVED,
                evidence_note="实现入口",
            ),
        ),
        evidence=(),
        uncertainties=("覆盖不完全",),
        unmapped_regions=(),
        diagnostics=(),
    )


def test_operations_from_report_emits_stable_id_operations_in_order() -> None:
    operations = operations_from_report(_report())

    assert [operation.kind for operation in operations] == [
        PatchOperationKind.ADD_NODE,
        PatchOperationKind.ADD_NODE,
        PatchOperationKind.ADD_EDGE,
        PatchOperationKind.SET_LOGICAL_FLOW,
        PatchOperationKind.ADD_MAPPING,
    ]
    node = operations[1]
    assert node.target_id == "behavior.answer-question"
    value = dict(node.value or {})
    assert value["kind"] == "behavior"
    assert value["summary"] == "回答用户问题"
    assert "approval" not in value
    assert "node_revision" not in value
    evidence = value["evidence"]
    assert isinstance(evidence, (tuple, list))
    first_evidence = dict(evidence[0])
    assert first_evidence["id"] == EVIDENCE_ID
    assert first_evidence["entity_uid"] == ENTITY_UID

    flow = operations[3]
    assert flow.target_id == "behavior.answer-question"
    flow_value = dict(flow.value or {})
    assert "flow_revision" not in flow_value
    mapping = operations[4]
    assert mapping.target_id == MAP_ID


def test_explicit_report_updates_existing_objects_without_add_conflicts() -> None:
    report = _report()
    report = replace(
        report,
        change_operations=(
                AnalysisChangeOperation(
                    AnalysisOperationKind.UPDATE_NODE,
                    "behavior.answer-question",
                    1,
                    AnalysisChangeKind.UPDATE,
                    "refresh-existing",
                ),
                AnalysisChangeOperation(
                    AnalysisOperationKind.UPDATE_EDGE,
                    EDGE_ID,
                    1,
                    AnalysisChangeKind.MOVE,
                    "move-behavior",
                ),
                AnalysisChangeOperation(
                    AnalysisOperationKind.SET_LOGICAL_FLOW,
                    "behavior.answer-question",
                    1,
                    AnalysisChangeKind.UPDATE,
                    "refresh-existing",
                ),
                AnalysisChangeOperation(
                    AnalysisOperationKind.UPDATE_MAPPING,
                    MAP_ID,
                    1,
                    AnalysisChangeKind.MOVE,
                    "move-behavior",
                ),
                AnalysisChangeOperation(
                    AnalysisOperationKind.ADD_NODE,
                    "responsibility.answering",
                    None,
                    AnalysisChangeKind.ADD,
                    "add-parent",
                ),
        ),
    )
    current = _graph(
        nodes=(
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Old title",
                "node_revision": 1,
            },
        ),
        edges=(
            {
                "id": EDGE_ID,
                "type": "contains",
                "source_id": "responsibility.old",
                "target_id": "behavior.answer-question",
                "edge_revision": 1,
            },
        ),
        flows=({**FLOW_VALUE, "flow_revision": 1},),
        mappings=({**MAPPING_VALUE, "mapping_revision": 1},),
    )

    operations = operations_from_report(
        report, current, cognition_initialized=True
    )

    assert [operation.kind for operation in operations] == [
        PatchOperationKind.UPDATE_NODE,
        PatchOperationKind.UPDATE_EDGE,
        PatchOperationKind.SET_LOGICAL_FLOW,
        PatchOperationKind.UPDATE_MAPPING,
        PatchOperationKind.ADD_NODE,
    ]


def test_explicit_report_removals_are_never_inferred_from_missing_candidates() -> None:
    report = replace(
        _report(),
        candidate_nodes=(),
        candidate_edges=(),
        candidate_flows=(),
        candidate_mappings=(),
        change_operations=(
                AnalysisChangeOperation(
                    AnalysisOperationKind.REMOVE_MAPPING,
                    MAP_ID,
                    1,
                    AnalysisChangeKind.REMOVE,
                    "remove-obsolete",
                ),
        ),
    )
    current = _graph(
        nodes=(
            {
                "id": "capability.keep-me",
                "kind": "capability",
                "title": "Keep me",
                "node_revision": 1,
            },
        ),
        mappings=({**MAPPING_VALUE, "mapping_revision": 1},),
    )

    operations = operations_from_report(
        report, current, cognition_initialized=True
    )
    applied = apply_operations_to_graph(current, operations, EVENT_ID, 2)

    assert [node["id"] for node in applied.nodes] == ["capability.keep-me"]
    assert applied.implementation_mappings == ()


def test_explicit_report_rejects_a_stale_target_revision() -> None:
    report = replace(
        _report(),
        candidate_edges=(),
        candidate_flows=(),
        candidate_mappings=(),
        change_operations=(
                AnalysisChangeOperation(
                    AnalysisOperationKind.UPDATE_NODE,
                    "behavior.answer-question",
                    1,
                    AnalysisChangeKind.UPDATE,
                    "refresh-existing",
                ),
                AnalysisChangeOperation(
                    AnalysisOperationKind.ADD_NODE,
                    "responsibility.answering",
                    None,
                    AnalysisChangeKind.ADD,
                    "add-parent",
                ),
        ),
    )
    current = _graph(
        nodes=(
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Current",
                "node_revision": 2,
            },
        ),
        revision=2,
    )

    with pytest.raises(CodeCortexError) as excinfo:
        operations_from_report(report, current, cognition_initialized=True)

    _invalid(excinfo)
    assert "before_revision" in str(excinfo.value)


def test_legacy_add_only_report_is_rejected_after_initialization() -> None:
    with pytest.raises(CodeCortexError) as excinfo:
        operations_from_report(_report(), _graph(), cognition_initialized=True)

    _invalid(excinfo)


def test_explicit_flow_removal_maps_to_the_existing_null_set_primitive() -> None:
    report = replace(
        _report(),
        candidate_nodes=(),
        candidate_edges=(),
        candidate_flows=(),
        candidate_mappings=(),
        change_operations=(
            AnalysisChangeOperation(
                AnalysisOperationKind.REMOVE_LOGICAL_FLOW,
                "behavior.answer-question",
                1,
                AnalysisChangeKind.REMOVE,
                "remove-obsolete-flow",
            ),
        ),
    )
    graph = _graph(flows=({**FLOW_VALUE, "flow_revision": 1},))

    operations = operations_from_report(
        report, graph, cognition_initialized=True
    )

    assert len(operations) == 1
    assert operations[0].kind is PatchOperationKind.SET_LOGICAL_FLOW
    assert operations[0].value is None
    assert operations[0].expected_revision == 1


def test_apply_rechecks_an_analysis_target_revision_precondition() -> None:
    operation = PatchOperation(
        "update_node",
        "behavior.answer-question",
        {
            "id": "behavior.answer-question",
            "kind": "behavior",
            "title": "New title",
        },
        expected_revision=1,
    )
    graph = _graph(
        nodes=(
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Current title",
                "node_revision": 2,
            },
        ),
        revision=2,
    )

    with pytest.raises(CodeCortexError) as excinfo:
        apply_operations_to_graph(graph, (operation,), EVENT_ID, 3)

    _invalid(excinfo)
    assert "expected revision" in str(excinfo.value)


def test_affected_nodes_from_report_collects_semantic_subjects() -> None:
    assert affected_nodes_from_report(_report()) == (
        "behavior.answer-question",
        "responsibility.answering",
    )


def test_affected_nodes_include_existing_objects_touched_by_explicit_removals() -> None:
    report = replace(
        _report(),
        candidate_nodes=(),
        candidate_edges=(),
        candidate_flows=(),
        candidate_mappings=(),
        change_operations=(
            AnalysisChangeOperation(
                AnalysisOperationKind.REMOVE_EDGE,
                EDGE_ID,
                1,
                AnalysisChangeKind.MOVE,
                "move-behavior",
            ),
            AnalysisChangeOperation(
                AnalysisOperationKind.REMOVE_MAPPING,
                MAP_ID,
                1,
                AnalysisChangeKind.REMOVE,
                "remove-mapping",
            ),
        ),
    )
    graph = _graph(
        edges=(
            {
                "id": EDGE_ID,
                "type": "contains",
                "source_id": "responsibility.answering",
                "target_id": "behavior.answer-question",
                "edge_revision": 1,
            },
        ),
        mappings=({**MAPPING_VALUE, "mapping_revision": 1},),
    )

    assert affected_nodes_from_report(report, graph) == (
        "behavior.answer-question",
        "responsibility.answering",
    )


def _graph(
    *,
    nodes: tuple[dict, ...] = (),
    edges: tuple[dict, ...] = (),
    flows: tuple[dict, ...] = (),
    mappings: tuple[dict, ...] = (),
    revision: int = 1,
) -> CognitiveGraph:
    return CognitiveGraph(1, revision, nodes, edges, flows, mappings)


def test_apply_operations_injects_provenance_and_revision() -> None:
    operations = (
        PatchOperation(
            "add_node",
            "behavior.answer-question",
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Answer questions",
            },
        ),
        PatchOperation("set_logical_flow", "behavior.answer-question", FLOW_VALUE),
        PatchOperation("add_mapping", MAP_ID, MAPPING_VALUE),
    )

    graph = apply_operations_to_graph(_graph(), operations, EVENT_ID, 2)

    node = dict(graph.nodes[0])
    assert node["approval"] == {"approval_event_id": EVENT_ID}
    assert node["node_revision"] == 2
    flow = dict(graph.logical_flows[0])
    assert flow["approval"] == {"approval_event_id": EVENT_ID}
    assert flow["flow_revision"] == 2
    step = dict(flow["steps"][0])
    assert step["approval"] == {"approval_event_id": EVENT_ID}
    mapping = dict(graph.implementation_mappings[0])
    assert mapping["approval"] == {"approval_event_id": EVENT_ID}
    assert mapping["mapping_revision"] == 2


def test_set_logical_flow_replaces_and_deletes_existing_flows() -> None:
    existing = {
        "behavior_id": "behavior.answer-question",
        "materialization_status": "unmaterialized",
        "flow_revision": 1,
        "steps": [],
        "evidence": [],
        "approval": {"approval_event_id": analysis_ulid("evt", 2)},
    }
    graph = _graph(flows=(existing,))

    replaced = apply_operations_to_graph(
        graph,
        (PatchOperation("set_logical_flow", "behavior.answer-question", FLOW_VALUE),),
        EVENT_ID,
        2,
    )
    assert len(replaced.logical_flows) == 1
    assert dict(replaced.logical_flows[0])["materialization_status"] == "materialized"

    deleted = apply_operations_to_graph(
        graph,
        (PatchOperation("set_logical_flow", "behavior.answer-question", None),),
        EVENT_ID,
        2,
    )
    assert deleted.logical_flows == ()


def test_add_and_update_mapping_enforce_existence() -> None:
    with pytest.raises(CodeCortexError) as excinfo:
        apply_operations_to_graph(
            _graph(mappings=(dict(MAPPING_VALUE),)),
            (PatchOperation("add_mapping", MAP_ID, MAPPING_VALUE),),
            EVENT_ID,
            2,
        )
    _invalid(excinfo)

    with pytest.raises(CodeCortexError) as excinfo:
        apply_operations_to_graph(
            _graph(),
            (PatchOperation("update_mapping", MAP_ID, MAPPING_VALUE),),
            EVENT_ID,
            2,
        )
    _invalid(excinfo)

    updated = apply_operations_to_graph(
        _graph(mappings=(dict(MAPPING_VALUE),)),
        (
            PatchOperation(
                "update_mapping", MAP_ID, {**MAPPING_VALUE, "role": "supporting"}
            ),
        ),
        EVENT_ID,
        2,
    )
    mapping = dict(updated.implementation_mappings[0])
    assert mapping["role"] == "supporting"
    assert mapping["mapping_revision"] == 2


def _graph_with_behavior() -> CognitiveGraph:
    return _graph(
        nodes=(
            {
                "id": "responsibility.answering",
                "kind": "responsibility",
                "title": "Answering",
            },
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Answer questions",
            },
        ),
        edges=(
            {
                "id": EDGE_ID,
                "type": "contains",
                "source_id": "responsibility.answering",
                "target_id": "behavior.answer-question",
            },
        ),
        flows=(
            {
                "behavior_id": "behavior.answer-question",
                "materialization_status": "materialized",
                "flow_revision": 1,
                "steps": [
                    {
                        "id": "behavior.answer-question#step.transform",
                        "order": 1,
                        "title": "Transform",
                        "uses_capabilities": ["capability.text-transform"],
                    }
                ],
                "evidence": [],
            },
        ),
        mappings=(dict(MAPPING_VALUE),),
    )


def test_remove_node_without_explicit_reference_handling_fails() -> None:
    with pytest.raises(CodeCortexError) as excinfo:
        apply_operations_to_graph(
            _graph_with_behavior(),
            (PatchOperation("remove_node", "behavior.answer-question", None),),
            EVENT_ID,
            2,
        )
    _invalid(excinfo)
    assert "edge" in str(excinfo.value)


def test_remove_node_with_explicit_handling_succeeds() -> None:
    graph = apply_operations_to_graph(
        _graph_with_behavior(),
        (
            PatchOperation("remove_mapping", MAP_ID, None),
            PatchOperation("set_logical_flow", "behavior.answer-question", None),
            PatchOperation("remove_edge", EDGE_ID, None),
            PatchOperation("remove_node", "behavior.answer-question", None),
        ),
        EVENT_ID,
        2,
    )
    assert [node["id"] for node in graph.nodes] == ["responsibility.answering"]
    assert graph.semantic_edges == ()
    assert graph.logical_flows == ()
    assert graph.implementation_mappings == ()
