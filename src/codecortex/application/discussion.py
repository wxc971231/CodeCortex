"""Pure, bounded routing plans for explicit CodeCortex discussions.

Core deliberately does not answer natural-language questions.  It turns the
Main agent's confirmed semantic anchors plus deterministic local freshness into
an auditable retrieval plan.  In particular, a graph miss is never a source
search restriction and stale graph material is only baseline navigation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
MaterializationDecision = Literal["expand", "transient"]
SemanticSyncExecutor = Literal["main", "analyzer"]

_MAX_CANDIDATES = 20
_CONTEXT_DEPTH = 2
_MAX_CONTEXT_NODES = 40
_MAX_CONTEXT_ENTITIES = 80
_MAX_CONTEXT_EVIDENCE = 80
_MAX_MAIN_SYNC_NODES = 10
_MAX_MAIN_SYNC_ENTITIES = 20


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
    semantic_sync_scope: SemanticSyncScope | None = None

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
        if self.semantic_sync_scope is not None and not isinstance(
            self.semantic_sync_scope, SemanticSyncScope
        ):
            raise TypeError("Semantic sync scope is invalid")


@dataclass(frozen=True)
class SemanticSyncScope:
    """Bounded scope used to select Main analysis or the read-only Analyzer."""

    affected_node_ids: tuple[str, ...]
    responsibility_ids: tuple[str, ...]
    affected_entity_ids: tuple[str, ...]
    scope_confidence: Literal["complete", "partial", "unknown"]

    def __post_init__(self) -> None:
        _unique_identifiers(self.affected_node_ids, "Affected node IDs")
        _unique_identifiers(self.responsibility_ids, "Responsibility IDs")
        _unique_identifiers(self.affected_entity_ids, "Affected entity IDs")
        if any(not identifier.startswith("responsibility.") for identifier in self.responsibility_ids):
            raise ValueError("Responsibility IDs must use the responsibility namespace")
        if self.scope_confidence not in {"complete", "partial", "unknown"}:
            raise ValueError("Semantic sync scope confidence is invalid")


@dataclass(frozen=True)
class SemanticSyncPlan:
    """An instruction for Main; Core never dispatches either kind of Agent."""

    executor: SemanticSyncExecutor
    scope: SemanticSyncScope
    reason_codes: tuple[str, ...]
    creates_proposal_after_analysis: bool = True
    auto_apply: bool = False


@dataclass(frozen=True)
class MaterializationOffer:
    """The one-time A/B choice for one relevant unmaterialized Behavior."""

    behavior_id: str
    route: Literal["offer_materialization", "source_first"]
    should_ask_user: bool
    choices: tuple[MaterializationDecision, ...]
    decision: MaterializationDecision | None
    requires_current_facts: bool
    requires_current_source: bool
    expansion_requires_later_patch_approval: bool
    semantic_sync: SemanticSyncPlan | None
    explanation: tuple[str, ...]


@dataclass(frozen=True)
class PendingProposalSummary:
    """A per-invocation reminder gate; callers supply the actual pending data."""

    should_notify: bool


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
    _materialization_decisions: dict[str, MaterializationDecision] = field(
        default_factory=dict, init=False, repr=False
    )
    _materialization_offers: set[str] = field(default_factory=set, init=False, repr=False)
    _pending_summary_shown: bool = field(default=False, init=False, repr=False)

    def set_anchors(self, anchor_ids: tuple[str, ...]) -> None:
        _unique_identifiers(anchor_ids, "Invocation anchor IDs")
        self.anchor_ids = anchor_ids

    def record_materialization_decision(
        self, behavior_id: str, decision: MaterializationDecision
    ) -> None:
        """Record the user's one explicit choice for this live invocation only."""
        _behavior_id(behavior_id)
        if decision not in {"expand", "transient"}:
            raise ValueError("Materialization decision is invalid")
        existing = self._materialization_decisions.get(behavior_id)
        if existing is not None and existing != decision:
            raise ValueError("Materialization decision is already recorded")
        self._materialization_decisions[behavior_id] = decision

    def has_materialization_decision(self, behavior_id: str) -> bool:
        _behavior_id(behavior_id)
        return behavior_id in self._materialization_decisions

    def record_materialization_offer_shown(self, behavior_id: str) -> None:
        """Remember an unanswered prompt so Main does not repeat it this invocation."""
        _behavior_id(behavior_id)
        self._materialization_offers.add(behavior_id)

    def materialization_offer_shown(self, behavior_id: str) -> bool:
        _behavior_id(behavior_id)
        return behavior_id in self._materialization_offers

    def materialization_decision(
        self, behavior_id: str
    ) -> MaterializationDecision | None:
        _behavior_id(behavior_id)
        return self._materialization_decisions.get(behavior_id)

    def record_pending_summary_shown(self) -> None:
        """Suppress further pending-Proposal reminders for this invocation."""
        self._pending_summary_shown = True

    @property
    def pending_summary_shown(self) -> bool:
        return self._pending_summary_shown


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
    materialization_offer: MaterializationOffer | None = None
    semantic_sync: SemanticSyncPlan | None = None
    auto_mutates_cognition: bool = False


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
        return self._plan(question_scope, candidate_ids, invocation)

    def plan_follow_up(self, question: str, invocation: InvocationState) -> DiscussionPlan:
        """Reuse live anchors only; no full transcript is serialized or persisted."""
        if not isinstance(invocation, InvocationState):
            raise TypeError("Follow-up planning requires an InvocationState")
        return self._plan(QuestionScope(question, invocation.anchor_ids), (), invocation)

    def offer_for(
        self,
        behavior_id: str,
        invocation: InvocationState,
        sync_scope: SemanticSyncScope | None = None,
    ) -> MaterializationOffer:
        """Return the current one-time materialization choice without writing state."""
        _behavior_id(behavior_id)
        if not isinstance(invocation, InvocationState):
            raise TypeError("Materialization offer requires an InvocationState")
        if sync_scope is not None and not isinstance(sync_scope, SemanticSyncScope):
            raise TypeError("Materialization offer requires a SemanticSyncScope")
        decision = invocation.materialization_decision(behavior_id)
        if decision is None:
            if invocation.materialization_offer_shown(behavior_id):
                return MaterializationOffer(
                    behavior_id=behavior_id,
                    route="source_first",
                    should_ask_user=False,
                    choices=(),
                    decision=None,
                    requires_current_facts=True,
                    requires_current_source=True,
                    expansion_requires_later_patch_approval=False,
                    semantic_sync=None,
                    explanation=(
                        "MATERIALIZATION_CHOICE_ALREADY_SHOWN",
                        "ANSWER_TRANSIENTLY_UNLESS_USER_CHOOSES_EXPANSION",
                        "NO_FORMAL_COGNITION_WRITE",
                    ),
                )
            invocation.record_materialization_offer_shown(behavior_id)
            return MaterializationOffer(
                behavior_id=behavior_id,
                route="offer_materialization",
                should_ask_user=True,
                choices=("expand", "transient"),
                decision=None,
                requires_current_facts=False,
                requires_current_source=False,
                expansion_requires_later_patch_approval=True,
                semantic_sync=None,
                explanation=(
                    "ASK_ONCE_PER_EXPLICIT_CODE_CORTEX_INVOCATION",
                    "EXPANSION_AUTHORIZES_ANALYSIS_NOT_PATCH_APPROVAL",
                ),
            )
        if decision == "transient":
            return MaterializationOffer(
                behavior_id=behavior_id,
                route="source_first",
                should_ask_user=False,
                choices=(),
                decision=decision,
                requires_current_facts=True,
                requires_current_source=True,
                expansion_requires_later_patch_approval=False,
                semantic_sync=None,
                explanation=(
                    "TRANSIENT_MATERIALIZATION_SELECTED",
                    "ANSWER_WITH_CURRENT_GRAPH_FACTS_AND_SOURCE",
                    "NO_FORMAL_COGNITION_WRITE",
                ),
            )
        semantic_sync = self.semantic_sync_plan(
            sync_scope if sync_scope is not None else _unknown_sync_scope(behavior_id)
        )
        return MaterializationOffer(
            behavior_id=behavior_id,
            route="source_first",
            should_ask_user=False,
            choices=(),
            decision=decision,
            requires_current_facts=True,
            requires_current_source=True,
            expansion_requires_later_patch_approval=True,
            semantic_sync=semantic_sync,
            explanation=(
                "PROPOSAL_BACKED_EXPANSION_SELECTED",
                "ANALYZE_CURRENT_FACTS_AND_SOURCE",
                "DISPLAY_PROPOSAL_BEFORE_ANY_FORMAL_WRITE",
                *semantic_sync.reason_codes,
            ),
        )

    def pending_summary(self, invocation: InvocationState) -> PendingProposalSummary:
        """Return the one-time reminder gate for an already-known pending Proposal."""
        if not isinstance(invocation, InvocationState):
            raise TypeError("Pending summary requires an InvocationState")
        return PendingProposalSummary(should_notify=not invocation.pending_summary_shown)

    @staticmethod
    def semantic_sync_plan(scope: SemanticSyncScope) -> SemanticSyncPlan:
        """Choose a bounded Main pass only when scope has one proven owner."""
        if not isinstance(scope, SemanticSyncScope):
            raise TypeError("Semantic sync planning requires a SemanticSyncScope")
        if scope.scope_confidence != "complete":
            return SemanticSyncPlan(
                executor="analyzer",
                scope=scope,
                reason_codes=("SCOPE_NOT_COMPLETE", "ANALYZER_REQUIRED"),
            )
        if len(scope.responsibility_ids) != 1:
            return SemanticSyncPlan(
                executor="analyzer",
                scope=scope,
                reason_codes=("CROSS_OR_UNOWNED_RESPONSIBILITY_SCOPE", "ANALYZER_REQUIRED"),
            )
        if (
            len(scope.affected_node_ids) > _MAX_MAIN_SYNC_NODES
            or len(scope.affected_entity_ids) > _MAX_MAIN_SYNC_ENTITIES
        ):
            return SemanticSyncPlan(
                executor="analyzer",
                scope=scope,
                reason_codes=("SCOPE_EXCEEDS_MAIN_BOUNDED_ANALYSIS", "ANALYZER_REQUIRED"),
            )
        return SemanticSyncPlan(
            executor="main",
            scope=scope,
            reason_codes=("SINGLE_RESPONSIBILITY_BOUNDED_SCOPE", "MAIN_ANALYSIS_ALLOWED"),
        )

    def _plan(
        self,
        question_scope: QuestionScope,
        candidate_ids: tuple[str, ...],
        invocation: InvocationState,
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
            behavior_id = _confirmed_behavior_id(question_scope)
            offer = self.offer_for(
                behavior_id, invocation, question_scope.semantic_sync_scope
            )
            if not offer.should_ask_user:
                return DiscussionPlan(
                    route="source_first",
                    anchor_ids=anchors,
                    entity_ids=question_scope.entity_ids,
                    candidate_node_ids=candidate_ids,
                    freshness=freshness,
                    context_request=context,
                    requires_current_facts=True,
                    requires_current_source=True,
                    graph_is_baseline_navigation=False,
                    native_search_unrestricted=False,
                    explanation=offer.explanation,
                    materialization_offer=offer,
                    semantic_sync=offer.semantic_sync,
                )
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
                explanation=(*offer.explanation, *freshness.reason_codes),
                materialization_offer=offer,
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


def _behavior_id(behavior_id: str) -> None:
    if not isinstance(behavior_id, str) or not behavior_id.startswith("behavior."):
        raise ValueError("Materialization requires a behavior ID")


def _confirmed_behavior_id(question_scope: QuestionScope) -> str:
    behavior_ids = tuple(
        node_id
        for node_id in question_scope.confirmed_node_ids
        if node_id.startswith("behavior.")
    )
    if not behavior_ids:
        raise ValueError("Behavior materialization requires a confirmed behavior")
    return behavior_ids[0]


def _unknown_sync_scope(behavior_id: str) -> SemanticSyncScope:
    """Conservatively require Analyzer when Main did not provide bounded scope."""
    return SemanticSyncScope(
        affected_node_ids=(behavior_id,),
        responsibility_ids=(),
        affected_entity_ids=(),
        scope_confidence="unknown",
    )
