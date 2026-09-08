"""Ephemeral, one-invocation materialization and reminder decisions."""

from __future__ import annotations

import pytest

from codecortex.application.discussion import DiscussionPlanner, InvocationState
from codecortex.application.freshness import FreshnessService


@pytest.fixture
def planner() -> DiscussionPlanner:
    return DiscussionPlanner(FreshnessService())


@pytest.fixture
def invocation() -> InvocationState:
    return InvocationState()


def test_declined_materialization_is_not_offered_twice(
    planner: DiscussionPlanner, invocation: InvocationState
) -> None:
    first = planner.offer_for("behavior.evaluate", invocation)
    assert first.should_ask_user is True
    assert first.choices == ("expand", "transient")

    invocation.record_materialization_decision("behavior.evaluate", "transient")
    second = planner.offer_for("behavior.evaluate", invocation)

    assert second.should_ask_user is False
    assert second.route == "source_first"
    assert second.requires_current_facts is True
    assert second.requires_current_source is True
    assert second.expansion_requires_later_patch_approval is False


def test_expand_is_recorded_once_and_requires_later_patch_approval(
    planner: DiscussionPlanner, invocation: InvocationState
) -> None:
    invocation.record_materialization_decision("behavior.evaluate", "expand")

    selected = planner.offer_for("behavior.evaluate", invocation)

    assert selected.should_ask_user is False
    assert selected.route == "source_first"
    assert selected.expansion_requires_later_patch_approval is True
    assert selected.decision == "expand"
    with pytest.raises(ValueError, match="already recorded"):
        invocation.record_materialization_decision("behavior.evaluate", "transient")


def test_unanswered_materialization_offer_is_not_repeated(
    planner: DiscussionPlanner, invocation: InvocationState
) -> None:
    first = planner.offer_for("behavior.evaluate", invocation)
    second = planner.offer_for("behavior.evaluate", invocation)

    assert first.should_ask_user is True
    assert second.should_ask_user is False
    assert second.route == "source_first"
    assert second.decision is None


def test_pending_proposal_is_summarized_once_per_invocation(
    planner: DiscussionPlanner, invocation: InvocationState
) -> None:
    assert planner.pending_summary(invocation).should_notify is True
    invocation.record_pending_summary_shown()
    assert planner.pending_summary(invocation).should_notify is False


def test_decisions_are_validated_and_stay_in_memory_only(
    invocation: InvocationState,
) -> None:
    with pytest.raises(ValueError, match="behavior"):
        invocation.record_materialization_decision("capability.search", "transient")
    with pytest.raises(ValueError, match="decision"):
        invocation.record_materialization_decision("behavior.search", "later")  # type: ignore[arg-type]

    invocation.record_materialization_decision("behavior.search", "transient")

    assert invocation.has_materialization_decision("behavior.search") is True
    assert invocation.materialization_decision("behavior.search") == "transient"
    assert invocation.serialized_chat_history is None
