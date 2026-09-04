"""Pure, bounded routing plans for explicit CodeCortex discussions.

Core deliberately does not answer natural-language questions.  It turns the
Main agent's confirmed semantic anchors plus deterministic local freshness into
an auditable retrieval plan.  In particular, a graph miss is never a source
search restriction and stale graph material is only baseline navigation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from codecortex.application.freshness import FreshnessService, QueryFreshnessResult
from codecortex.infrastructure.persistence.graph_replica import ContextRequest, GraphHit

DiscussionRoute = Literal[
    "graph_current",
    "graph_unaffected",
    "source_first",
    "native_fallback",
    "offer_materialization",
]
BehaviorMaterialization = Literal["materialized", "unmaterialized"]

_MAX_CANDIDATES = 20
_CONTEXT_DEPTH = 2
_MAX_CONTEXT_NODES = 40
_MAX_CONTEXT_ENTITIES = 80
_MAX_CONTEXT_EVIDENCE = 80


def _unique_identifiers(values: tuple[str, ...], name: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{name} must contain non-empty identifiers")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


@dataclass(frozen=True)
class QuestionScope:
    """Main's bounded semantic decision for one user question.

    ``confirmed_node_ids`` are intentionally supplied by Main after candidate
    recall.  Core search is deterministic retrieval, not an attempt to infer
    semantic relevance from the wording of the question.
    """

    question: str
    confirmed_node_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    behavior_materialization: BehaviorMaterialization | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("Question text must be non-empty")
        _unique_identifiers(self.confirmed_node_ids, "Confirmed node IDs")
        _unique_identifiers(self.entity_ids, "Entity IDs")
        if self.behavior_materialization not in (
            None,
            "materialized",
            "unmaterialized",
        ):
            raise ValueError("Behavior materialization status is invalid")


@dataclass
class InvocationState:
    """Ephemeral Main-owned state for one explicit CodeCortex invocation.

    The Core stores neither a Codex thread identifier nor chat transcript.
    Task 8 extends this object with one-time materialization decisions, but
    this initial state already makes follow-ups reuse only the current live
    anchor selection.
    """

    anchor_ids: tuple[str, ...] = ()
    serialized_chat_history: None = None

    def set_anchors(self, anchor_ids: tuple[str, ...]) -> None:
        _unique_identifiers(anchor_ids, "Invocation anchor IDs")
        self.anchor_ids = anchor_ids


@dataclass(frozen=True)
class DiscussionPlan:
    """A transparent bounded retrieval route; never a cognition write plan."""

    route: DiscussionRoute
    anchor_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    candidate_node_ids: tuple[str, ...]
    freshness: QueryFreshnessResult
    context_request: ContextRequest | None
    requires_current_facts: bool
    requires_current_source: bool
    graph_is_baseline_navigation: bool
    native_search_unrestricted: bool
    explanation: tuple[str, ...]


class DiscussionPlanner:
    """Select a conservative graph/source/native route without Agent calls."""

    def __init__(self, freshness: FreshnessService) -> None:
        self._freshness = freshness

    def plan(
        self,
        question_scope: QuestionScope,
        candidates: Sequence[GraphHit],
        invocation: InvocationState,
    ) -> DiscussionPlan:
        """Plan one explicit question after bounded candidate recall.

        A confirmed anchor must have been returned by the candidate call.  It
        prevents arbitrary graph IDs from being smuggled into bounded context,
        while an empty confirmation intentionally takes unrestricted Native
        Codex fallback even if recall produced weak/irrelevant candidates.
        """
        if not isinstance(question_scope, QuestionScope):
            raise TypeError("Discussion planning requires a QuestionScope")
        if not isinstance(invocation, InvocationState):
            raise TypeError("Discussion planning requires an InvocationState")
        candidate_ids = self._candidate_ids(candidates)
        unknown = set(question_scope.confirmed_node_ids) - set(candidate_ids)
        if unknown:
            raise ValueError("Confirmed node IDs must be returned by candidate recall")
        if question_scope.behavior_materialization is not None and not any(
            hit.node_id in question_scope.confirmed_node_ids and hit.kind == "behavior"
            for hit in candidates
        ):
            raise ValueError("Behavior materialization requires a confirmed behavior")
        invocation.set_anchors(question_scope.confirmed_node_ids)
        return self._plan(question_scope, candidate_ids)

    def plan_follow_up(self, question: str, invocation: InvocationState) -> DiscussionPlan:
        """Reuse live anchors only; no full transcript is serialized or persisted."""
        if not isinstance(invocation, InvocationState):
            raise TypeError("Follow-up planning requires an InvocationState")
        return self._plan(QuestionScope(question, invocation.anchor_ids), ())

    def _plan(
        self, question_scope: QuestionScope, candidate_ids: tuple[str, ...]
    ) -> DiscussionPlan:
        freshness = self._freshness.for_query(
            question_scope.confirmed_node_ids, question_scope.entity_ids
        )
        anchors = question_scope.confirmed_node_ids
        if not anchors:
            return DiscussionPlan(
                route="native_fallback",
                anchor_ids=(),
                entity_ids=question_scope.entity_ids,
                candidate_node_ids=candidate_ids,
                freshness=freshness,
                context_request=None,
                requires_current_facts=False,
                requires_current_source=False,
                graph_is_baseline_navigation=False,
                native_search_unrestricted=True,
                explanation=(
                    "NO_CONFIRMED_SEMANTIC_ANCHOR",
                    "NATIVE_SOURCE_SEARCH_UNRESTRICTED",
                    *freshness.reason_codes,
                ),
            )

        context = ContextRequest(
            node_ids=anchors,
            entity_uids=question_scope.entity_ids,
            depth=_CONTEXT_DEPTH,
            max_nodes=_MAX_CONTEXT_NODES,
            max_entities=_MAX_CONTEXT_ENTITIES,
            max_evidence=_MAX_CONTEXT_EVIDENCE,
        )
        if freshness.status in {"affected_source_first", "unknown_source_first"}:
            return DiscussionPlan(
                route="source_first",
                anchor_ids=anchors,
                entity_ids=question_scope.entity_ids,
                candidate_node_ids=candidate_ids,
                freshness=freshness,
                context_request=context,
                requires_current_facts=True,
                requires_current_source=True,
                graph_is_baseline_navigation=True,
                native_search_unrestricted=False,
                explanation=(
                    "GRAPH_IS_BASELINE_NAVIGATION_ONLY",
                    "READ_CURRENT_FACTS_AND_SOURCE_BEFORE_CONCLUSION",
                    *freshness.reason_codes,
                ),
            )
        if question_scope.behavior_materialization == "unmaterialized":
            return DiscussionPlan(
                route="offer_materialization",
                anchor_ids=anchors,
                entity_ids=question_scope.entity_ids,
                candidate_node_ids=candidate_ids,
                freshness=freshness,
                context_request=context,
                requires_current_facts=False,
                requires_current_source=False,
                graph_is_baseline_navigation=False,
                native_search_unrestricted=False,
                explanation=(
                    "UNMATERIALIZED_BEHAVIOR_RELEVANT",
                    "OFFER_TRANSIENT_OR_PROPOSAL_BACKED_EXPANSION",
                    *freshness.reason_codes,
                ),
            )

        route: DiscussionRoute = (
            "graph_current"
            if freshness.status == "current"
            else "graph_unaffected"
        )
        return DiscussionPlan(
            route=route,
            anchor_ids=anchors,
            entity_ids=question_scope.entity_ids,
            candidate_node_ids=candidate_ids,
            freshness=freshness,
            context_request=context,
            requires_current_facts=False,
            requires_current_source=False,
            graph_is_baseline_navigation=False,
            native_search_unrestricted=False,
            explanation=(
                "GRAPH_CONTEXT_IS_CURRENT"
                if route == "graph_current"
                else "COMPLETE_PENDING_SCOPE_DOES_NOT_INTERSECT_QUERY",
                "BOUNDED_GRAPH_CONTEXT",
                *freshness.reason_codes,
            ),
        )

    @staticmethod
    def _candidate_ids(candidates: Sequence[GraphHit]) -> tuple[str, ...]:
        if isinstance(candidates, (str, bytes)):
            raise TypeError("Discussion candidates must be GraphHit records")
        result = tuple(candidates)
        if len(result) > _MAX_CANDIDATES:
            raise ValueError(f"Discussion candidate recall is capped at {_MAX_CANDIDATES}")
        if any(not isinstance(hit, GraphHit) for hit in result):
            raise TypeError("Discussion candidates must be GraphHit records")
        ids = tuple(hit.node_id for hit in result)
        _unique_identifiers(ids, "Discussion candidate IDs")
        return ids
