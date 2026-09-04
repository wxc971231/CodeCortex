"""Unit-level baseline-to-current ChangeSet behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

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


def _baseline(repository: Repository) -> tuple[FormalState, FactSyncService]:
    sync = FactSyncService(repository)
    result = sync.sync()
    files = tuple(
        {"relative_path": path, "content_digest": digest}
        for path, digest in sync.database.source_file_digests().items()
    )
    formal = FormalState(
        manifest=Manifest(
            schema_version=1,
            graph_revision=0,
            cognition_initialized=True,
            cognition_baseline=result.repository_source_digest,
        ),
        graph=CognitiveGraph.empty(),
        entity_refs=EntityRefs.empty(),
        source_baseline=SourceBaseline(
            schema_version=1,
            digest_profile_version=1,
            managed_source_set_version=1,
            repository_source_digest=result.repository_source_digest,
            files=files,
        ),
    )
    sync.database.replace_baseline_entity_snapshots(result.repository_source_digest)
    return formal, sync


def _detect_current(formal: FormalState, sync: FactSyncService):
    sync.sync()
    return ChangeDetector().detect(formal, sync.database)


def test_change_set_is_always_baseline_to_current(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "x = 1\n")
    formal, sync = _baseline(repository)

    _write(repository.root, "a.py", "x = 2\n")
    first = _detect_current(formal, sync)
    assert first is not None

    _write(repository.root, "a.py", "x = 3\n")
    second = _detect_current(formal, sync)
    assert second is not None

    assert first.baseline_source_digest == second.baseline_source_digest
    assert second.current_source_digest == sync.database.cache_metadata().repository_source_digest
    assert second.changed_files.modified == ("a.py",)


def test_unique_same_digest_pair_is_rename(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "old.py", "x = 1\n")
    formal, sync = _baseline(repository)

    (repository.root / "old.py").unlink()
    _write(repository.root, "new.py", "x = 1\n")

    result = _detect_current(formal, sync)

    assert result is not None
    assert result.changed_files.renamed == (("old.py", "new.py"),)
    assert result.changed_files.added == result.changed_files.deleted == ()


def test_non_unique_same_digest_pairs_stay_added_and_deleted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "old_a.py", "x = 1\n")
    _write(repository.root, "old_b.py", "x = 1\n")
    formal, sync = _baseline(repository)

    (repository.root / "old_a.py").unlink()
    (repository.root / "old_b.py").unlink()
    _write(repository.root, "new_a.py", "x = 1\n")
    _write(repository.root, "new_b.py", "x = 1\n")

    result = _detect_current(formal, sync)

    assert result is not None
    assert result.changed_files.renamed == ()
    assert result.changed_files.added == ("new_a.py", "new_b.py")
    assert result.changed_files.deleted == ("old_a.py", "old_b.py")


def test_reverting_to_formal_baseline_removes_change_set(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "x = 1\n")
    formal, sync = _baseline(repository)

    _write(repository.root, "a.py", "x = 2\n")
    assert _detect_current(formal, sync) is not None

    _write(repository.root, "a.py", "x = 1\n")
    assert _detect_current(formal, sync) is None


def test_missing_baseline_entity_snapshot_marks_entity_diff_partial(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "def before():\n    return 1\n")
    formal, sync = _baseline(repository)

    with sync.database.open_write() as connection:
        connection.execute("DELETE FROM baseline_entity_snapshots")
        connection.execute(
            "UPDATE cache_metadata SET baseline_entity_snapshot_completeness = 'partial'"
        )
    _write(repository.root, "a.py", "def after():\n    return 2\n")

    result = _detect_current(formal, sync)

    assert result is not None
    assert result.entity_diff_completeness == "partial"
