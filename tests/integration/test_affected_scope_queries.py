"""Affected-scope confidence integration cases over the real facts cache."""

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


def _formal(sync: FactSyncService) -> FormalState:
    digest = sync.database.cache_metadata().repository_source_digest
    return FormalState(
        manifest=Manifest(1, 1, True, digest),
        graph=CognitiveGraph(1, 1, (), (), (), ()),
        entity_refs=EntityRefs(1, 1, ()),
        source_baseline=SourceBaseline(
            1,
            1,
            1,
            digest,
            tuple(
                {"relative_path": path, "content_digest": value}
                for path, value in sync.database.source_file_digests().items()
            ),
        ),
    )


def test_parse_error_is_concrete_unknown_scope(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    repository = Repository(tmp_path)
    (tmp_path / "a.py").write_text("def stable():\n    return 1\n", encoding="utf-8")
    sync = FactSyncService(repository)
    sync.sync()
    formal = _formal(sync)
    sync.database.replace_baseline_entity_snapshots(
        formal.manifest.cognition_baseline or ""
    )
    (tmp_path / "a.py").write_text("def broken(:\n", encoding="utf-8")
    sync.sync()
    change_set = ChangeDetector().detect(formal, sync.database)
    assert change_set is not None

    result = AffectedScopeCalculator(formal, sync.database).calculate(change_set)

    assert result.scope_confidence == "unknown"
    assert any("PYTHON_SYNTAX_ERROR" in item for item in result.diagnostics)
