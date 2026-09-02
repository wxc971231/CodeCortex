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
        entity_refs=EntityRefs(schema_version=1, graph_revision=1, entity_refs=()),
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
