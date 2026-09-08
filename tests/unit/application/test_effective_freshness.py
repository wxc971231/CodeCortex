"""Repository summary and query-local cognition freshness decisions."""

from __future__ import annotations

import pytest

from codecortex.application.freshness import FreshnessService
from codecortex.domain.freshness import ChangeSet, EntityChanges, FileChanges


def _change_set(
    *,
    confidence: str = "complete",
    nodes: tuple[str, ...] = ("behavior.train",),
    entities: tuple[str, ...] = ("ent_01J00000000000000000000001",),
    unmapped: tuple[dict[str, object], ...] = (),
) -> ChangeSet:
    return ChangeSet(
        change_set_id="chg_01J00000000000000000000001",
        baseline_source_digest="sha256:" + "a" * 64,
        current_source_digest="sha256:" + "b" * 64,
        created_at="2026-09-04T00:00:00Z",
        changed_files=FileChanges(modified=("training.py",)),
        changed_entities=EntityChanges(modified=("ent_01J00000000000000000000001",)),
        file_diff_completeness="complete",
        entity_diff_completeness="complete",
        affected_nodes=nodes,
        affected_entities=entities,
        scope_confidence=confidence,  # type: ignore[arg-type]
        unmapped_changes=unmapped,
    )


@pytest.mark.parametrize(
    ("change_set", "nodes", "entities", "expected"),
    [
        (None, ("behavior.deploy",), (), "current"),
        (_change_set(), ("behavior.deploy",), (), "unaffected_current"),
        (_change_set(), ("behavior.train",), (), "affected_source_first"),
        (
            _change_set(confidence="partial", unmapped=({"relative_path": "unknown.py"},)),
            ("behavior.deploy",),
            (),
            "unknown_source_first",
        ),
        (
            _change_set(confidence="unknown"),
            (),
            ("ent_01J00000000000000000000001",),
            "affected_source_first",
        ),
    ],
)
def test_effective_query_freshness(
    change_set: ChangeSet | None,
    nodes: tuple[str, ...],
    entities: tuple[str, ...],
    expected: str,
) -> None:
    service = FreshnessService(change_set)

    result = service.for_query(nodes, entities)

    assert result.status == expected
    assert result.reason_codes


def test_repository_status_is_summary_not_global_query_invalidation() -> None:
    service = FreshnessService(_change_set())

    summary = service.repository_status()
    query = service.for_query(("behavior.deploy",), ())

    assert summary.status == "pending"
    assert query.status == "unaffected_current"
    assert query.matched_affected_nodes == ()
    assert query.baseline_source_digest == summary.baseline_source_digest
    assert query.current_source_digest == summary.current_source_digest


def test_unknown_scope_is_repository_unresolved_and_explains_unmapped_work() -> None:
    service = FreshnessService(
        _change_set(
            confidence="unknown",
            unmapped=({"relative_path": "dynamic.py", "reason": "parse_diagnostic"},),
        )
    )

    result = service.for_query(("behavior.deploy",), ())

    assert service.repository_status().status == "unresolved"
    assert result.status == "unknown_source_first"
    assert result.unmapped_changes == (
        {"relative_path": "dynamic.py", "reason": "parse_diagnostic"},
    )
    assert "UNMAPPED_SCOPE" in result.reason_codes


def test_query_validation_rejects_empty_or_duplicate_identifiers() -> None:
    service = FreshnessService(None)

    with pytest.raises(ValueError):
        service.for_query(("behavior.train", "behavior.train"), ())
    with pytest.raises(ValueError):
        service.for_query((), ("",))
