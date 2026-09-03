"""End-to-end regression over the production CLI composition root.

These tests exist because every pre-review apply test wired replica providers
by hand while the production root did not, which let the read plane break
unnoticed.  They compose through ``_default_services`` exactly as the CLI/MCP
entrypoints do and assert that guarded reads work before and after an
analysis-backed apply, with zero cache warnings.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.services import ApplicationServices
from codecortex.domain.proposals import ApprovalRecord
from codecortex.interfaces.cli.main import _default_services

APPROVED_AT = "2026-09-03T08:00:00Z"


def _ulid(prefix: str, index: int) -> str:
    suffix = format(index, "X")
    return f"{prefix}_01J{'0' * (23 - len(suffix))}{suffix}"


@pytest.fixture
def production_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ApplicationServices:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        '"""Core module."""\n\n\n'
        "def greet(name: str) -> str:\n"
        '    return f"hello {name}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    services = _default_services()
    services.initialize_repository()
    services.synchronize_facts("full")
    return services


def _report_payload(
    services: ApplicationServices,
    *,
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    mappings: list[dict[str, object]],
) -> bytes:
    assert services.initialization_service is not None
    coordinate = services.initialization_service.begin_analysis()
    payload = {
        "schema_version": 1,
        "base_graph_revision": coordinate.graph_revision,
        "analyzed_source_digest": coordinate.source_digest,
        "analysis_scope": {"mode": "repository", "files": 2, "modules": 1},
        "coverage": {"analyzed_partitions": ["pkg"], "unexamined_partitions": []},
        "candidate_nodes": nodes,
        "candidate_edges": edges,
        "candidate_flows": [],
        "candidate_mappings": mappings,
        "evidence": [],
        "uncertainties": ["scoped regression report"],
        "unmapped_regions": [],
        "diagnostics": [],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _approve_and_apply(services: ApplicationServices, payload: bytes):
    proposal = services.create_cognitive_proposal_from_analysis(
        payload, "Initialize repository understanding"
    )
    return services.apply_cognitive_proposal(
        proposal.proposal_id,
        ApprovalRecord(
            proposal_id=proposal.proposal_id,
            patch_digest=proposal.patch_digest,
            approved_by="user",
            approved_at=APPROVED_AT,
            approval_summary="Approve current patch",
        ),
    )


def test_analysis_apply_with_entity_mapping_then_guarded_reads(
    production_services: ApplicationServices,
) -> None:
    services = production_services
    # Guarded reads already work before any apply: initialization rebuilt the
    # revision-0 replica through the production composition root.
    page = services.repository_facts("pkg.core", None, 10)
    entity = next(item for item in page.entities if item.name == "greet")
    evidence = {
        "id": _ulid("evid", 1),
        "kind": "code_entity",
        "entity_uid": entity.uid,
        "relative_path": entity.relative_path,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
        "observation": "greeting entry point",
    }
    payload = _report_payload(
        services,
        nodes=[
            {"id": "responsibility.greeting", "kind": "responsibility",
             "title": "Greeting"},
            {"id": "behavior.greet-user", "kind": "behavior",
             "title": "Greet user", "evidence": [evidence]},
            {"id": "capability.text-format", "kind": "capability",
             "title": "Text formatting"},
        ],
        edges=[
            {
                "id": _ulid("edge", 1),
                "type": "contains",
                "source_id": "responsibility.greeting",
                "target_id": "behavior.greet-user",
                "epistemic_status": "inferred",
                "evidence": [dict(evidence, id=_ulid("evid", 2))],
            },
            {
                "id": _ulid("edge", 2),
                "type": "uses",
                "source_id": "behavior.greet-user",
                "target_id": "capability.text-format",
                "epistemic_status": "inferred",
                "evidence": [dict(evidence, id=_ulid("evid", 3))],
            },
        ],
        mappings=[
            {
                "id": _ulid("map", 1),
                "subject_kind": "node",
                "subject_id": "behavior.greet-user",
                "entity_uid": entity.uid,
                "role": "primary",
                "resolution_status": "resolved",
                "evidence_note": "greeting entry point",
            }
        ],
    )

    result = _approve_and_apply(services, payload)

    assert result.graph_revision == 1
    assert result.cache_warnings == ()
    facts = services.repository_facts(
        "pkg.core", None, 10, expected_graph_revision=1
    )
    assert len(facts.entities) > 0
    hits = services.search_cognitive_graph("Greet user", expected_graph_revision=1)
    assert any(hit.node_id == "behavior.greet-user" for hit in hits.hits)


def test_contains_edge_without_evidence_applies_and_stays_readable(
    production_services: ApplicationServices,
) -> None:
    services = production_services
    payload = _report_payload(
        services,
        nodes=[
            {"id": "responsibility.greeting", "kind": "responsibility",
             "title": "Greeting"},
            {"id": "behavior.greet-user", "kind": "behavior",
             "title": "Greet user"},
        ],
        edges=[
            {
                "id": _ulid("edge", 1),
                "type": "contains",
                "source_id": "responsibility.greeting",
                "target_id": "behavior.greet-user",
                "epistemic_status": "inferred",
            }
        ],
        mappings=[],
    )

    result = _approve_and_apply(services, payload)

    assert result.graph_revision == 1
    assert result.cache_warnings == ()
    hits = services.search_cognitive_graph("Greet user", expected_graph_revision=1)
    assert any(hit.node_id == "behavior.greet-user" for hit in hits.hits)
