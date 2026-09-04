"""Deterministic, bounded propagation from changed facts into formal cognition."""

from __future__ import annotations

import subprocess
from pathlib import Path

from codecortex.application.affected_scope import AffectedScopeCalculator
from codecortex.application.change_detection import ChangeDetector
from codecortex.application.fact_sync import FactSyncService
from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    Manifest,
    SourceBaseline,
)
from codecortex.infrastructure.repository import Repository


def _repository(tmp_path: Path) -> Repository:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return Repository(tmp_path)


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _formal(
    sync: FactSyncService,
    *,
    nodes: tuple[dict[str, object], ...] = (),
    edges: tuple[dict[str, object], ...] = (),
    flows: tuple[dict[str, object], ...] = (),
    mappings: tuple[dict[str, object], ...] = (),
) -> FormalState:
    digest = sync.database.cache_metadata().repository_source_digest
    files = tuple(
        {"relative_path": path, "content_digest": value}
        for path, value in sync.database.source_file_digests().items()
    )
    return FormalState(
        manifest=Manifest(1, 1, True, digest),
        graph=CognitiveGraph(1, 1, nodes, edges, flows, mappings),
        entity_refs=EntityRefs(1, 1, ()),
        source_baseline=SourceBaseline(1, 1, 1, digest, files),
    )


def _change_set(formal: FormalState, sync: FactSyncService):
    sync.sync()
    result = ChangeDetector().detect(formal, sync.database)
    assert result is not None
    return result


def test_flow_step_propagates_to_behavior_and_parent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "app.py", "def save() -> int:\n    return 1\n")
    sync = FactSyncService(repository)
    sync.sync()
    entity = next(
        item
        for item in sync.database.entities_at_path("app.py", None, 10).items
        if item.kind == "function" and item.qualname == "save"
    )
    formal = _formal(
        sync,
        nodes=(
            {"id": "responsibility.training", "kind": "responsibility"},
            {"id": "behavior.train", "kind": "behavior"},
        ),
        edges=(
            {
                "type": "contains",
                "source_id": "responsibility.training",
                "target_id": "behavior.train",
            },
        ),
        flows=(
            {
                "behavior_id": "behavior.train",
                "steps": [{"id": "behavior.train#step.save", "uses_capabilities": []}],
            },
        ),
        mappings=(
            {
                "entity_uid": entity.uid,
                "subject_kind": "flow_step",
                "subject_id": "behavior.train#step.save",
                "resolution_status": "resolved",
            },
        ),
    )
    sync.database.replace_baseline_entity_snapshots(
        formal.manifest.cognition_baseline or ""
    )
    _write(repository.root, "app.py", "def save() -> int:\n    return 2\n")

    result = AffectedScopeCalculator(formal, sync.database).calculate(
        _change_set(formal, sync)
    )

    assert result.affected_flows == ("behavior.train",)
    assert {"behavior.train", "responsibility.training"} <= set(result.affected_nodes)
    assert result.scope_confidence == "complete"


def test_unmapped_new_file_forces_partial(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "known.py", "value = 1\n")
    sync = FactSyncService(repository)
    sync.sync()
    formal = _formal(sync)
    sync.database.replace_baseline_entity_snapshots(
        formal.manifest.cognition_baseline or ""
    )
    _write(repository.root, "src/new_feature.py", "def new_feature():\n    return 1\n")

    result = AffectedScopeCalculator(formal, sync.database).calculate(
        _change_set(formal, sync)
    )

    assert result.scope_confidence == "partial"
    assert result.unmapped_changes[0].relative_path == "src/new_feature.py"


def test_only_one_resolved_dependency_hop_is_propagated(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "def source() -> int:\n    return 1\n")
    _write(
        repository.root,
        "b.py",
        "from a import source\n\ndef middle() -> int:\n    return source()\n",
    )
    _write(
        repository.root,
        "c.py",
        "from b import middle\n\ndef outer() -> int:\n    return middle()\n",
    )
    sync = FactSyncService(repository)
    sync.sync()
    entities = {
        item.relative_path: item
        for path in ("a.py", "b.py", "c.py")
        for item in sync.database.entities_at_path(path, None, 10).items
        if item.kind == "function"
    }
    formal = _formal(
        sync,
        nodes=(
            {"id": "behavior.source", "kind": "behavior"},
            {"id": "behavior.middle", "kind": "behavior"},
            {"id": "behavior.outer", "kind": "behavior"},
        ),
        mappings=tuple(
            {
                "entity_uid": entities[path].uid,
                "subject_kind": "node",
                "subject_id": behavior,
                "resolution_status": "resolved",
            }
            for path, behavior in (
                ("a.py", "behavior.source"),
                ("b.py", "behavior.middle"),
                ("c.py", "behavior.outer"),
            )
        ),
    )
    sync.database.replace_baseline_entity_snapshots(
        formal.manifest.cognition_baseline or ""
    )
    _write(repository.root, "a.py", "def source() -> int:\n    return 2\n")

    result = AffectedScopeCalculator(formal, sync.database).calculate(
        _change_set(formal, sync)
    )

    assert {"behavior.source", "behavior.middle"} <= set(result.affected_nodes)
    assert "behavior.outer" not in result.affected_nodes
