"""Golden-output coverage for deterministic cognitive Markdown views."""

from __future__ import annotations

from codecortex.domain.cognition import CognitiveGraph
from codecortex.infrastructure.rendering.markdown import render_tree, render_views


def _graph() -> CognitiveGraph:
    return CognitiveGraph(
        schema_version=1,
        graph_revision=7,
        nodes=(
            {
                "id": "capability.source-search",
                "kind": "capability",
                "title": "Source search",
                "summary": "Find matching source files.",
                "epistemic_status": "established",
                "node_revision": 1,
                "approval": {"approval_event_id": "evt_01"},
            },
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Answer question",
                "summary": "Answer a repository question.",
                "intent": "Give grounded answers.",
                "observed": "Uses source search.",
                "epistemic_status": "inferred",
                "node_revision": 2,
                "approval": {"approval_event_id": "evt_01"},
            },
            {
                "id": "responsibility.repository-understanding",
                "kind": "responsibility",
                "title": "Repository understanding",
                "summary": "Maintain project understanding.",
                "epistemic_status": "established",
                "node_revision": 3,
                "approval": {"approval_event_id": "evt_01"},
            },
        ),
        semantic_edges=(
            {
                "id": "edge_contains",
                "type": "contains",
                "source_id": "responsibility.repository-understanding",
                "target_id": "behavior.answer-question",
                "epistemic_status": "established",
                "approval": {"approval_event_id": "evt_01"},
            },
            {
                "id": "edge_uses",
                "type": "uses",
                "source_id": "behavior.answer-question",
                "target_id": "capability.source-search",
                "epistemic_status": "inferred",
                "approval": {"approval_event_id": "evt_01"},
            },
        ),
        logical_flows=(
            {
                "behavior_id": "behavior.answer-question",
                "materialization_status": "materialized",
                "flow_revision": 1,
                "steps": (
                    {
                        "id": "behavior.answer-question#step.search",
                        "order": 1,
                        "title": "Search",
                        "summary": "Search the current source.",
                        "uses_capabilities": ("capability.source-search",),
                        "approval": {"approval_event_id": "evt_01"},
                    },
                ),
                "approval": {"approval_event_id": "evt_01"},
            },
        ),
        implementation_mappings=(
            {
                "id": "map_01",
                "subject_kind": "node",
                "subject_id": "behavior.answer-question",
                "entity_uid": "ent_01",
                "role": "primary",
                "resolution_status": "resolved",
                "evidence_note": "Question entry point.",
                "approval": {"approval_event_id": "evt_01"},
            },
        ),
    )


def test_tree_view_matches_golden() -> None:
    assert render_tree(_graph()) == (
        "# CodeCortex Cognitive Tree\n\n"
        "## Responsibilities\n\n"
        "- `responsibility.repository-understanding` — Repository understanding\n"
        "  - `behavior.answer-question` — Answer question\n"
        "    - uses `capability.source-search` — Source search\n\n"
        "## Shared Capabilities\n\n"
        "- `capability.source-search` — Source search\n"
    ).encode()


def test_complete_rendering_is_deterministic_and_repository_relative() -> None:
    first = render_views(_graph())
    second = render_views(_graph())

    assert first == second
    assert tuple(first) == (
        "views/TREE.md",
        "views/responsibilities/repository-understanding.md",
        "views/behaviors/answer-question.md",
        "views/capabilities/source-search.md",
    )
    page = first["views/behaviors/answer-question.md"].decode()
    assert "## Flow" in page
    assert "## Implementation mappings" in page
    assert "`ent_01`" in page
    assert "/home/" not in page
