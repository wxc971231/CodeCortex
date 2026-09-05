"""Task 7 routing policy for graph-guided project discussions."""

from __future__ import annotations

import pytest

from codecortex.application.discussion import (
    DiscussionPlanner,
    InvocationState,
    QuestionScope,
    SemanticSyncScope,
)
from codecortex.application.freshness import FreshnessService
from codecortex.domain.freshness import ChangeSet, EntityChanges, FileChanges
from codecortex.infrastructure.persistence.graph_replica import GraphHit


def _change_set(
    *,
    confidence: str = "complete",
    nodes: tuple[str, ...] = ("behavior.train",),
    entities: tuple[str, ...] = (),
    unmapped: tuple[dict[str, object], ...] = (),
) -> ChangeSet:
    return ChangeSet(
        change_set_id="chg_01J00000000000000000000001",
        baseline_source_digest="sha256:" + "a" * 64,
        current_source_digest="sha256:" + "b" * 64,
        created_at="2026-09-04T00:00:00Z",
        changed_files=FileChanges(modified=("training.py",)),
        changed_entities=EntityChanges(),
        file_diff_completeness="complete",
        entity_diff_completeness="complete",
        affected_nodes=nodes,
        affected_entities=entities,
        scope_confidence=confidence,  # type: ignore[arg-type]
        unmapped_changes=unmapped,
    )


def _hit(node_id: str, *, kind: str = "behavior") -> GraphHit:
    return GraphHit(node_id, kind, node_id, 10, ("title",))


@pytest.mark.parametrize(
    ("change_set", "anchors", "flow", "expected"),
    [
        (None, ("behavior.deploy",), "materialized", "graph_current"),
        (_change_set(), ("behavior.deploy",), "materialized", "graph_unaffected"),
        (_change_set(), ("behavior.train",), "materialized", "source_first"),
        (None, (), None, "native_fallback"),
        (None, ("behavior.evaluate",), "unmaterialized", "offer_materialization"),
    ],
)
def test_discussion_routes(
    change_set: ChangeSet | None,
    anchors: tuple[str, ...],
    flow: str | None,
    expected: str,
) -> None:
    planner = DiscussionPlanner(FreshnessService(change_set))
    scope = QuestionScope(
        question="How does this work?",
        confirmed_node_ids=anchors,
        behavior_materialization=flow,  # type: ignore[arg-type]
    )

    plan = planner.plan(scope, tuple(_hit(node_id) for node_id in anchors), InvocationState())

    assert plan.route == expected
    assert plan.freshness.reason_codes
    assert plan.explanation


def test_follow_up_reuses_only_current_invocation_context() -> None:
    planner = DiscussionPlanner(FreshnessService())
    invocation = InvocationState()
    planner.plan(
        QuestionScope("How does deployment work?", ("behavior.deploy",)),
        (_hit("behavior.deploy"),),
        invocation,
    )

    follow_up = planner.plan_follow_up("What calls it?", invocation)

    assert follow_up.anchor_ids == ("behavior.deploy",)
    assert invocation.serialized_chat_history is None


def test_source_first_keeps_old_graph_as_labeled_navigation_only() -> None:
    planner = DiscussionPlanner(FreshnessService(_change_set()))

    plan = planner.plan(
        QuestionScope("How does training work?", ("behavior.train",)),
        (_hit("behavior.train"),),
        InvocationState(),
    )

    assert plan.route == "source_first"
    assert plan.graph_is_baseline_navigation is True
    assert plan.requires_current_facts is True
    assert plan.requires_current_source is True
    assert plan.context_request.max_nodes == 40
    assert plan.context_request.max_entities == 80
    assert plan.context_request.max_evidence == 80


def test_graph_outside_question_never_restricts_native_search() -> None:
    plan = DiscussionPlanner(FreshnessService()).plan(
        QuestionScope("Where is the CLI command registered?"),
        (_hit("behavior.unrelated"),),
        InvocationState(),
    )

    assert plan.route == "native_fallback"
    assert plan.native_search_unrestricted is True
    assert plan.anchor_ids == ()


def test_confirmed_anchor_must_come_from_bounded_candidate_recall() -> None:
    planner = DiscussionPlanner(FreshnessService())
    with pytest.raises(ValueError, match="candidate"):
        planner.plan(
            QuestionScope("question", ("behavior.missing",)),
            (_hit("behavior.present"),),
            InvocationState(),
        )


def test_unmaterialized_route_requires_a_confirmed_behavior() -> None:
    planner = DiscussionPlanner(FreshnessService())
    with pytest.raises(ValueError, match="confirmed behavior"):
        planner.plan(
            QuestionScope(
                "question",
                ("capability.search",),
                behavior_materialization="unmaterialized",
            ),
            (_hit("capability.search", kind="capability"),),
            InvocationState(),
        )


def test_recorded_expand_precedes_affected_source_first_routing() -> None:
    planner = DiscussionPlanner(FreshnessService(_change_set()))
    invocation = InvocationState()
    invocation.record_materialization_decision("behavior.train", "expand")
    scope = SemanticSyncScope(
        affected_node_ids=("behavior.train",),
        responsibility_ids=("responsibility.training",),
        affected_entity_ids=(),
        scope_confidence="complete",
    )

    plan = planner.plan(
        QuestionScope(
            "How does training work?",
            ("behavior.train",),
            behavior_materialization="unmaterialized",
            semantic_sync_scope=scope,
        ),
        (_hit("behavior.train"),),
        invocation,
    )

    assert plan.route == "source_first"
    assert plan.semantic_sync is not None
    assert plan.semantic_sync.executor == "main"
    assert plan.requires_current_facts is True
    assert plan.requires_current_source is True
    assert plan.graph_is_baseline_navigation is True


def test_affected_unmaterialized_behavior_is_offered_only_once() -> None:
    planner = DiscussionPlanner(FreshnessService(_change_set()))
    invocation = InvocationState()
    scope = QuestionScope(
        "How does training work?",
        ("behavior.train",),
        behavior_materialization="unmaterialized",
    )

    first = planner.plan(scope, (_hit("behavior.train"),), invocation)
    second = planner.plan(scope, (_hit("behavior.train"),), invocation)

    assert first.route == "offer_materialization"
    assert first.materialization_offer is not None
    assert first.materialization_offer.should_ask_user is True
    assert second.route == "source_first"
    assert second.materialization_offer is not None
    assert second.materialization_offer.should_ask_user is False
    assert second.graph_is_baseline_navigation is True
