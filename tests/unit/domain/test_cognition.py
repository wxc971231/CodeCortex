from copy import deepcopy
from dataclasses import replace

import pytest

from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    HistoryEventRef,
    Manifest,
    SourceBaseline,
    ValidationIssueCode,
    validate_formal_state,
)

EVENT_ID = "evt_01J00000000000000000000000"
ENTITY_ID = "ent_01J00000000000000000000000"


def approval() -> dict[str, object]:
    return {"approval_event_id": EVENT_ID}


def valid_linked_state() -> FormalState:
    nodes = (
        {
            "id": "responsibility.repository-understanding",
            "kind": "responsibility",
            "node_revision": 1,
            "approval": approval(),
        },
        {
            "id": "behavior.answer-question",
            "kind": "behavior",
            "node_revision": 1,
            "approval": approval(),
        },
        {
            "id": "capability.search-code",
            "kind": "capability",
            "node_revision": 1,
            "evidence": [
                {
                    "id": "evid_01J00000000000000000000000",
                    "relative_path": "src/search.py",
                }
            ],
            "approval": approval(),
        },
        {
            "id": "capability.parse-code",
            "kind": "capability",
            "node_revision": 1,
            "approval": approval(),
        },
    )
    edges = (
        {
            "id": "edge_01J00000000000000000000000",
            "type": "contains",
            "source_id": "responsibility.repository-understanding",
            "target_id": "behavior.answer-question",
            "edge_revision": 1,
            "approval": approval(),
        },
        {
            "id": "edge_01J00000000000000000000001",
            "type": "uses",
            "source_id": "behavior.answer-question",
            "target_id": "capability.search-code",
            "edge_revision": 1,
            "approval": approval(),
        },
        {
            "id": "edge_01J00000000000000000000002",
            "type": "depends_on",
            "source_id": "capability.search-code",
            "target_id": "capability.parse-code",
            "edge_revision": 1,
            "approval": approval(),
        },
    )
    flow = {
        "behavior_id": "behavior.answer-question",
        "materialization_status": "materialized",
        "flow_revision": 1,
        "steps": [
            {
                "id": "behavior.answer-question#step.find-context",
                "order": 1,
                "uses_capabilities": ["capability.search-code"],
                "approval": approval(),
            }
        ],
        "approval": approval(),
    }
    mapping = {
        "id": "map_01J00000000000000000000000",
        "subject_kind": "flow_step",
        "subject_id": "behavior.answer-question#step.find-context",
        "entity_uid": ENTITY_ID,
        "mapping_revision": 1,
        "approval": approval(),
    }
    return FormalState(
        manifest=Manifest(1, 1, False, None),
        graph=CognitiveGraph(1, 1, nodes, edges, (flow,), (mapping,)),
        entity_refs=EntityRefs(
            schema_version=1,
            graph_revision=1,
            entities=(
                {
                    "uid": ENTITY_ID,
                    "relative_path": "src/search.py",
                },
            ),
        ),
        source_baseline=SourceBaseline.empty(),
        history_events=(HistoryEventRef(EVENT_ID, "cognitive_proposal_applied"),),
    )


def empty_state(**overrides: object) -> FormalState:
    values: dict[str, object] = {
        "manifest": Manifest(
            schema_version=1,
            graph_revision=0,
            cognition_initialized=False,
            cognition_baseline=None,
        ),
        "graph": CognitiveGraph.empty(),
        "entity_refs": EntityRefs.empty(),
        "source_baseline": SourceBaseline(
            schema_version=1,
            digest_profile_version=1,
            managed_source_set_version=1,
            repository_source_digest=None,
            files=(),
        ),
        "history_events": (),
    }
    values.update(overrides)
    return FormalState(**values)  # type: ignore[arg-type]


def test_revision_zero_empty_state_is_valid() -> None:
    """Breaking the revision-zero defaults must make initialization invalid."""
    result = validate_formal_state(empty_state())

    assert result.valid is True
    assert result.issues == ()


@pytest.mark.parametrize(
    ("manifest", "baseline", "expected_code"),
    [
        (
            Manifest(1, 0, False, "sha256:" + "a" * 64),
            SourceBaseline.empty(),
            ValidationIssueCode.UNINITIALIZED_BASELINE_NOT_EMPTY,
        ),
        (
            Manifest(1, 0, False, None),
            SourceBaseline(
                1,
                1,
                1,
                "sha256:" + "a" * 64,
                ({"relative_path": "src/a.py", "content_digest": "sha256:" + "b" * 64},),
            ),
            ValidationIssueCode.UNINITIALIZED_BASELINE_NOT_EMPTY,
        ),
    ],
)
def test_uninitialized_cognition_requires_a_strictly_empty_baseline(
    manifest: Manifest,
    baseline: SourceBaseline,
    expected_code: ValidationIssueCode,
) -> None:
    """Relaxing either null/empty rule would falsely claim a revision-zero baseline."""
    result = validate_formal_state(empty_state(manifest=manifest, source_baseline=baseline))

    assert result.valid is False
    assert expected_code in {issue.code for issue in result.issues}


def test_graph_and_manifest_revisions_must_match() -> None:
    """Ignoring one revision would allow readers to combine different formal snapshots."""
    graph = CognitiveGraph(
        schema_version=1,
        graph_revision=1,
        nodes=(),
        semantic_edges=(),
        logical_flows=(),
        implementation_mappings=(),
    )

    result = validate_formal_state(empty_state(graph=graph))

    assert ValidationIssueCode.GRAPH_REVISION_MISMATCH in {
        issue.code for issue in result.issues
    }


@pytest.mark.parametrize(
    ("node", "expected_code"),
    [
        (
            {
                "id": "edge_01J00000000000000000000000",
                "kind": "capability",
                "node_revision": 1,
                "approval": {"approval_event_id": "evt_01J00000000000000000000000"},
            },
            ValidationIssueCode.INVALID_ID_NAMESPACE,
        ),
        (
            {
                "id": "capability.storage",
                "kind": "capability",
                "node_revision": 1,
            },
            ValidationIssueCode.MISSING_PROVENANCE,
        ),
        (
            {
                "id": "capability.storage",
                "kind": "capability",
                "node_revision": 1,
                "approval": {"approval_event_id": "evt_01J00000000000000000000000"},
            },
            ValidationIssueCode.DANGLING_APPROVAL_EVENT,
        ),
    ],
)
def test_non_empty_graph_validates_namespace_provenance_and_history(
    node: dict[str, object], expected_code: ValidationIssueCode
) -> None:
    """Removing graph integrity checks would admit unapproved or misidentified cognition."""
    manifest = Manifest(1, 1, False, None)
    graph = CognitiveGraph(1, 1, (node,), (), (), ())

    result = validate_formal_state(empty_state(manifest=manifest, graph=graph))

    assert expected_code in {issue.code for issue in result.issues}


def test_m0_revision_may_advance_without_claiming_cognition_initialization() -> None:
    """Tying cognition_initialized to graph revision would break M0 persistence tests."""
    event_id = "evt_01J00000000000000000000000"
    node = {
        "id": "capability.storage",
        "kind": "capability",
        "node_revision": 1,
        "approval": {"approval_event_id": event_id},
    }
    state = empty_state(
        manifest=Manifest(1, 1, False, None),
        graph=CognitiveGraph(1, 1, (node,), (), (), ()),
        entity_refs=EntityRefs(schema_version=1, graph_revision=1, entities=()),
        history_events=(HistoryEventRef(event_id, "cognitive_proposal_applied"),),
    )

    assert validate_formal_state(state).valid is True


@pytest.mark.parametrize(
    ("graph_values", "expected_location"),
    [
        (
            {
                "semantic_edges": (
                    {
                        "id": "edge_01J00000000000000000000000",
                        "approval": {
                            "approval_event_id": "evt_01J00000000000000000000000"
                        },
                    },
                )
            },
            "graph.semantic_edges[0].edge_revision",
        ),
        (
            {
                "logical_flows": (
                    {
                        "behavior_id": "behavior.answer-question",
                        "steps": [],
                        "approval": {
                            "approval_event_id": "evt_01J00000000000000000000000"
                        },
                    },
                )
            },
            "graph.logical_flows[0].flow_revision",
        ),
        (
            {
                "implementation_mappings": (
                    {
                        "id": "map_01J00000000000000000000000",
                        "approval": {
                            "approval_event_id": "evt_01J00000000000000000000000"
                        },
                    },
                )
            },
            "graph.implementation_mappings[0].mapping_revision",
        ),
    ],
)
def test_every_revisioned_graph_record_requires_a_valid_revision(
    graph_values: dict[str, object], expected_location: str
) -> None:
    """Skipping per-record revisions would make later conflict checks ambiguous."""
    values: dict[str, object] = {
        "schema_version": 1,
        "graph_revision": 1,
        "nodes": (),
        "semantic_edges": (),
        "logical_flows": (),
        "implementation_mappings": (),
    }
    values.update(graph_values)
    state = empty_state(
        manifest=Manifest(1, 1, False, None),
        graph=CognitiveGraph(**values),  # type: ignore[arg-type]
        entity_refs=EntityRefs(1, 1, ()),
        history_events=(
            HistoryEventRef(
                "evt_01J00000000000000000000000", "cognitive_proposal_applied"
            ),
        ),
    )

    result = validate_formal_state(state)

    assert expected_location in {issue.location for issue in result.issues}


def test_materialized_flow_step_requires_approval_provenance() -> None:
    """Approving only the flow container must not leave an unapproved formal step."""
    event_id = "evt_01J00000000000000000000000"
    flow = {
        "behavior_id": "behavior.answer-question",
        "materialization_status": "materialized",
        "flow_revision": 1,
        "steps": [
            {
                "id": "behavior.answer-question#step.find-context",
                "order": 1,
            }
        ],
        "approval": {"approval_event_id": event_id},
    }
    state = empty_state(
        manifest=Manifest(1, 1, False, None),
        graph=CognitiveGraph(1, 1, (), (), (flow,), ()),
        entity_refs=EntityRefs(1, 1, ()),
        history_events=(HistoryEventRef(event_id, "cognitive_proposal_applied"),),
    )

    result = validate_formal_state(state)

    assert ValidationIssueCode.MISSING_PROVENANCE in {
        issue.code for issue in result.issues
    }


def test_graph_evidence_paths_must_be_repository_relative() -> None:
    """Trusting an absolute evidence path would break portability and repository isolation."""
    event_id = "evt_01J00000000000000000000000"
    node = {
        "id": "capability.storage",
        "kind": "capability",
        "node_revision": 1,
        "evidence": [
            {
                "id": "evid_01J00000000000000000000000",
                "relative_path": "/tmp/outside.py",
            }
        ],
        "approval": {"approval_event_id": event_id},
    }
    state = empty_state(
        manifest=Manifest(1, 1, False, None),
        graph=CognitiveGraph(1, 1, (node,), (), (), ()),
        entity_refs=EntityRefs(1, 1, ()),
        history_events=(HistoryEventRef(event_id, "cognitive_proposal_applied"),),
    )

    result = validate_formal_state(state)

    assert ValidationIssueCode.INVALID_RELATIVE_PATH in {
        issue.code for issue in result.issues
    }


def test_valid_edge_mapping_evidence_and_entity_namespaces_are_accepted() -> None:
    """Over-strict namespace or reference checks would reject a fully linked formal graph."""
    assert validate_formal_state(valid_linked_state()).valid is True


@pytest.mark.parametrize(
    ("collection", "index", "field", "value", "expected_code"),
    [
        (
            "semantic_edges",
            0,
            "source_id",
            ENTITY_ID,
            "INVALID_ID_NAMESPACE",
        ),
        (
            "semantic_edges",
            1,
            "target_id",
            "capability.missing",
            "DANGLING_REFERENCE",
        ),
        (
            "implementation_mappings",
            0,
            "entity_uid",
            "prop_01J00000000000000000000000",
            "INVALID_ID_NAMESPACE",
        ),
        (
            "implementation_mappings",
            0,
            "subject_id",
            "behavior.missing#step.nowhere",
            "DANGLING_REFERENCE",
        ),
    ],
)
def test_edges_and_mappings_reject_wrong_namespaces_or_dangling_subjects(
    collection: str,
    index: int,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    """Skipping reference resolution would admit links to missing or cross-domain subjects."""
    state = valid_linked_state()
    records = deepcopy(getattr(state.graph, collection))
    records[index][field] = value
    graph = replace(state.graph, **{collection: records})

    result = validate_formal_state(replace(state, graph=graph))

    assert expected_code in {issue.code.value for issue in result.issues}


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("flow_capability", "responsibility.repository-understanding"),
        ("evidence", "edge_01J00000000000000000000000"),
        ("entity", "map_01J00000000000000000000000"),
    ],
)
def test_nested_and_entity_identifiers_reject_cross_namespace_values(
    target: str, value: str
) -> None:
    """A namespace check limited to top-level graph records would miss nested identities."""
    state = valid_linked_state()
    if target == "flow_capability":
        flows = deepcopy(state.graph.logical_flows)
        flows[0]["steps"][0]["uses_capabilities"] = [value]  # type: ignore[index]
        state = replace(state, graph=replace(state.graph, logical_flows=flows))
    elif target == "evidence":
        nodes = deepcopy(state.graph.nodes)
        nodes[2]["evidence"][0]["id"] = value  # type: ignore[index]
        state = replace(state, graph=replace(state.graph, nodes=nodes))
    else:
        entities = deepcopy(state.entity_refs.entities)
        entities[0]["uid"] = value
        state = replace(
            state,
            entity_refs=replace(state.entity_refs, entities=entities),
        )

    result = validate_formal_state(state)

    assert ValidationIssueCode.INVALID_ID_NAMESPACE in {
        issue.code for issue in result.issues
    }


@pytest.mark.parametrize(("field", "value"), [("epistemic_status", []), ("created_by", {})])
def test_malformed_nested_node_values_return_validation_issues(
    field: str, value: object
) -> None:
    """Unhashable nested values must be reported rather than escaping as TypeError."""
    state = valid_linked_state()
    nodes = deepcopy(state.graph.nodes)
    nodes[0][field] = value

    result = validate_formal_state(replace(state, graph=replace(state.graph, nodes=nodes)))

    assert ValidationIssueCode.INVALID_NODE in {issue.code for issue in result.issues}
