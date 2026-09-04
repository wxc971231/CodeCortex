"""Deterministic acceptance for the M1b materialization interaction contract."""

from __future__ import annotations

from pathlib import Path

from codecortex.application.discussion import (
    DiscussionPlanner,
    InvocationState,
    QuestionScope,
    SemanticSyncScope,
)
from codecortex.application.freshness import FreshnessService
from codecortex.infrastructure.persistence.graph_replica import GraphHit
from codecortex.integrations.codex.install import install_codex


def _hit(behavior_id: str) -> GraphHit:
    return GraphHit(behavior_id, "behavior", behavior_id, 10, ("title",))


def _unmaterialized_scope(
    *, sync_scope: SemanticSyncScope | None = None
) -> QuestionScope:
    return QuestionScope(
        "How does evaluation work?",
        ("behavior.evaluate",),
        behavior_materialization="unmaterialized",
        semantic_sync_scope=sync_scope,
    )


def test_transient_choice_answers_from_current_evidence_without_proposal() -> None:
    planner = DiscussionPlanner(FreshnessService())
    invocation = InvocationState()

    first = planner.plan(_unmaterialized_scope(), (_hit("behavior.evaluate"),), invocation)
    invocation.record_materialization_decision("behavior.evaluate", "transient")
    second = planner.plan(_unmaterialized_scope(), (_hit("behavior.evaluate"),), invocation)

    assert first.route == "offer_materialization"
    assert first.materialization_offer is not None
    assert first.materialization_offer.should_ask_user is True
    assert second.route == "source_first"
    assert second.requires_current_facts is True
    assert second.requires_current_source is True
    assert second.semantic_sync is None
    assert second.auto_mutates_cognition is False


def test_expand_selects_main_or_analyzer_but_never_auto_applies() -> None:
    planner = DiscussionPlanner(FreshnessService())
    invocation = InvocationState()
    invocation.record_materialization_decision("behavior.evaluate", "expand")

    small = planner.plan(
        _unmaterialized_scope(
            sync_scope=SemanticSyncScope(
                affected_node_ids=("behavior.evaluate",),
                responsibility_ids=("responsibility.evaluation",),
                affected_entity_ids=("ent_01J00000000000000000000001",),
                scope_confidence="complete",
            )
        ),
        (_hit("behavior.evaluate"),),
        invocation,
    )
    large = planner.plan(
        _unmaterialized_scope(
            sync_scope=SemanticSyncScope(
                affected_node_ids=("behavior.evaluate", "behavior.reporting"),
                responsibility_ids=(
                    "responsibility.evaluation",
                    "responsibility.reporting",
                ),
                affected_entity_ids=("ent_01J00000000000000000000001",),
                scope_confidence="complete",
            )
        ),
        (_hit("behavior.evaluate"),),
        invocation,
    )

    assert small.semantic_sync is not None
    assert small.semantic_sync.executor == "main"
    assert large.semantic_sync is not None
    assert large.semantic_sync.executor == "analyzer"
    for plan in (small, large):
        assert plan.route == "source_first"
        assert plan.semantic_sync is not None
        assert plan.semantic_sync.creates_proposal_after_analysis is True
        assert plan.semantic_sync.auto_apply is False
        assert plan.auto_mutates_cognition is False


def test_installed_skill_declares_one_time_choice_and_approval_boundary(tmp_path: Path) -> None:
    executable = tmp_path / "codecortex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    install_codex(tmp_path, executable, dry_run=False, force=False)
    skill = (tmp_path / ".agents" / "skills" / "codecortex" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "once per explicit CodeCortex invocation" in skill
    assert "Never auto-apply" in skill
    assert "Main Codex" in skill and "Analyzer" in skill
