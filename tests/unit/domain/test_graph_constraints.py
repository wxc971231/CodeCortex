"""Contract tests for the complete, typed M1a cognitive graph."""

from dataclasses import replace

import pytest

from codecortex.domain.graph import (
    Approval,
    Behavior,
    Capability,
    CognitiveEdge,
    CognitiveGraph,
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
    validate_cognitive_graph,
)

EVENT_ID = "evt_01J00000000000000000000000"
EDGE_ID = "edge_01J00000000000000000000000"
MAPPING_ID = "map_01J00000000000000000000000"
ENTITY_ID = "ent_01J00000000000000000000000"
EVIDENCE_ID = "evid_01J00000000000000000000000"


def approval() -> Approval:
    return Approval(EVENT_ID)


def evidence() -> Evidence:
    return Evidence(
        id=EVIDENCE_ID,
        kind="code_entity",
        entity_uid=ENTITY_ID,
        relative_path="src/query.py",
        start_line=10,
        end_line=20,
        observation="The function selects source-grounded context.",
    )


@pytest.fixture
def valid_graph() -> CognitiveGraph:
    responsibility = Responsibility(
        id="responsibility.repository-understanding",
        title="Repository understanding",
        aliases=("project understanding",),
        approval=approval(),
    )
    behavior = Behavior(
        id="behavior.answer-question",
        title="Answer repository question",
        approval=approval(),
    )
    capability = Capability(
        id="capability.source-retrieval",
        title="Source retrieval",
        approval=approval(),
    )
    return CognitiveGraph(
        graph_revision=1,
        nodes=(responsibility, behavior, capability),
        semantic_edges=(
            CognitiveEdge(
                id=EDGE_ID,
                type=EdgeType.CONTAINS,
                source_id=responsibility.id,
                target_id=behavior.id,
                epistemic_status="established",
                approval=approval(),
            ),
            CognitiveEdge(
                id="edge_01J00000000000000000000001",
                type=EdgeType.USES,
                source_id=behavior.id,
                target_id=capability.id,
                evidence=(evidence(),),
                approval=approval(),
            ),
        ),
        logical_flows=(
            LogicalFlow(
                behavior_id=behavior.id,
                materialization_status=MaterializationStatus.MATERIALIZED,
                flow_revision=1,
                steps=(
                    FlowStep(
                        id="behavior.answer-question#step.find-context",
                        order=1,
                        title="Find context",
                        uses_capabilities=(capability.id,),
                        approval=approval(),
                    ),
                ),
                approval=approval(),
            ),
        ),
        implementation_mappings=(
            ImplementationMapping(
                id=MAPPING_ID,
                subject_kind=MappingSubjectKind.FLOW_STEP,
                subject_id="behavior.answer-question#step.find-context",
                entity_uid=ENTITY_ID,
                role=MappingRole.PRIMARY,
                resolution_status=ResolutionStatus.RESOLVED,
                approval=approval(),
            ),
        ),
        entity_uids=frozenset({ENTITY_ID}),
    )


def violation_codes(graph: CognitiveGraph) -> set[str]:
    return {violation.code for violation in validate_cognitive_graph(graph)}


def test_complete_valid_graph_passes_all_invariants(
    valid_graph: CognitiveGraph,
) -> None:
    assert validate_cognitive_graph(valid_graph) == ()


def test_behavior_requires_exactly_one_responsibility_parent(
    valid_graph: CognitiveGraph,
) -> None:
    other = Responsibility(
        id="responsibility.other",
        title="Other",
        approval=approval(),
    )
    extra = CognitiveEdge(
        id="edge_01J00000000000000000000002",
        type=EdgeType.CONTAINS,
        source_id=other.id,
        target_id="behavior.answer-question",
        epistemic_status="established",
        approval=approval(),
    )

    invalid = replace(
        valid_graph,
        nodes=(*valid_graph.nodes, other),
        semantic_edges=(*valid_graph.semantic_edges, extra),
    )

    assert violation_codes(invalid) == {"BEHAVIOR_PARENT_COUNT"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (MaterializationStatus.UNMATERIALIZED, "UNMATERIALIZED_FLOW_HAS_STEPS"),
        (MaterializationStatus.NOT_APPLICABLE, "NOT_APPLICABLE_FLOW_HAS_STEPS"),
    ],
)
def test_nonmaterialized_flow_has_no_steps(
    valid_graph: CognitiveGraph, status: MaterializationStatus, expected: str
) -> None:
    flow = replace(valid_graph.logical_flows[0], materialization_status=status)
    invalid = replace(valid_graph, logical_flows=(flow,))

    assert violation_codes(invalid) == {expected}


def test_materialized_flow_requires_contiguous_step_orders(
    valid_graph: CognitiveGraph,
) -> None:
    second = FlowStep(
        id="behavior.answer-question#step.answer",
        order=3,
        title="Answer",
        approval=approval(),
    )
    flow = replace(
        valid_graph.logical_flows[0],
        steps=(*valid_graph.logical_flows[0].steps, second),
    )
    invalid = replace(valid_graph, logical_flows=(flow,))

    assert violation_codes(invalid) == {"FLOW_ORDER_NOT_CONTIGUOUS"}


def test_allowed_edge_matrix_passes_and_forbidden_matrix_fails(
    valid_graph: CognitiveGraph,
) -> None:
    invalid_edge = replace(
        valid_graph.semantic_edges[1],
        type=EdgeType.USES,
        source_id="capability.source-retrieval",
        target_id="behavior.answer-question",
    )
    invalid = replace(
        valid_graph,
        semantic_edges=(valid_graph.semantic_edges[0], invalid_edge),
    )

    assert violation_codes(invalid) == {"EDGE_KIND_NOT_ALLOWED"}


def test_inferred_relation_requires_evidence(valid_graph: CognitiveGraph) -> None:
    edge = replace(valid_graph.semantic_edges[1], evidence=())
    invalid = replace(valid_graph, semantic_edges=(valid_graph.semantic_edges[0], edge))

    assert violation_codes(invalid) == {"RELATION_EVIDENCE_REQUIRED"}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("role", "incidental", "INVALID_MAPPING_ROLE"),
        ("resolution_status", "current", "INVALID_MAPPING_RESOLUTION_STATUS"),
        ("subject_kind", "edge", "INVALID_MAPPING_SUBJECT_KIND"),
    ],
)
def test_mapping_constrains_subject_role_and_resolution(
    valid_graph: CognitiveGraph, field: str, value: str, expected: str
) -> None:
    mapping = replace(valid_graph.implementation_mappings[0], **{field: value})
    invalid = replace(valid_graph, implementation_mappings=(mapping,))

    assert violation_codes(invalid) == {expected}


def test_evidence_requires_portable_path_and_valid_line_range(
    valid_graph: CognitiveGraph,
) -> None:
    bad = replace(evidence(), relative_path="../outside.py", start_line=20, end_line=10)
    edge = replace(valid_graph.semantic_edges[1], evidence=(bad,))
    invalid = replace(valid_graph, semantic_edges=(valid_graph.semantic_edges[0], edge))

    assert violation_codes(invalid) == {
        "INVALID_EVIDENCE_LOCATION",
        "INVALID_RELATIVE_PATH",
    }


def test_formal_objects_require_event_namespace(valid_graph: CognitiveGraph) -> None:
    node = replace(
        valid_graph.nodes[0], approval=Approval("prop_01J00000000000000000000000")
    )
    invalid = replace(valid_graph, nodes=(node, *valid_graph.nodes[1:]))

    assert violation_codes(invalid) == {"APPROVAL_EVENT_NAMESPACE"}


def test_aliases_are_unique_after_portable_text_normalization(
    valid_graph: CognitiveGraph,
) -> None:
    extra = Capability(
        id="capability.other",
        title="Other",
        aliases=("Project Understanding",),
        approval=approval(),
    )
    invalid = replace(valid_graph, nodes=(*valid_graph.nodes, extra))

    assert violation_codes(invalid) == {"DUPLICATE_ALIAS"}


def test_evidence_id_and_entity_reference_are_global_graph_constraints(
    valid_graph: CognitiveGraph,
) -> None:
    duplicate = replace(evidence(), entity_uid="ent_01J00000000000000000000001")
    node = replace(valid_graph.nodes[0], evidence=(duplicate,))
    invalid = replace(valid_graph, nodes=(node, *valid_graph.nodes[1:]))

    assert violation_codes(invalid) == {
        "DANGLING_EVIDENCE_ENTITY",
        "DUPLICATE_EVIDENCE_ID",
    }
