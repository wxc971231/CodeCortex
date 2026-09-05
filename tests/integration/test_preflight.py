"""Mandatory deterministic fact preflight over real formal/cache repository state."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.preflight import PreflightService
from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    Manifest,
    SourceBaseline,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.repository import Repository


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _initialized_preflight(tmp_path: Path) -> tuple[Repository, FormalStore, FactSyncService, PreflightService]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    repository = Repository(tmp_path)
    _write(repository.root, "app.py", "def answer() -> int:\n    return 1\n")
    lock = RepositoryLock(repository.root)
    formal_store = FormalStore(repository)
    seed_sync = FactSyncService(repository, repository_lock=lock)
    first = seed_sync.sync()
    baseline = SourceBaseline(
        1,
        1,
        1,
        first.repository_source_digest,
        tuple(
            {"relative_path": path, "content_digest": digest}
            for path, digest in seed_sync.database.source_file_digests().items()
        ),
    )
    formal_store.initialize(
        FormalState(
            manifest=Manifest(1, 0, True, first.repository_source_digest),
            graph=CognitiveGraph.empty(),
            entity_refs=EntityRefs.empty(),
            source_baseline=baseline,
        )
    )
    sync = FactSyncService(repository, formal_store=formal_store, repository_lock=lock)
    sync.database.replace_baseline_entity_snapshots(first.repository_source_digest)
    preflight = PreflightService(
        formal_store=formal_store,
        fact_sync=sync,
        facts=sync.database,
        freshness_store=FreshnessStore(repository.root / ".codecortex" / ".cache"),
        repository_lock=lock,
    )
    return repository, formal_store, sync, preflight


def test_preflight_is_idempotent_and_replaces_one_baseline_to_current_change_set(
    tmp_path: Path,
) -> None:
    repository, formal_store, _sync, preflight = _initialized_preflight(tmp_path)
    initial = preflight.run()
    assert initial.change_set is None
    assert initial.repository_status.status == "fresh"
    initial_generation = initial.fact_sync.index_generation

    _write(repository.root, "app.py", "def answer() -> int:\n    return 2\n")
    first_change = preflight.run()
    assert first_change.change_set is not None

    _write(repository.root, "app.py", "def answer() -> int:\n    return 3\n")
    second_change = preflight.run()
    assert second_change.change_set is not None
    assert second_change.change_set.baseline_source_digest == first_change.change_set.baseline_source_digest
    assert second_change.change_set.current_source_digest != first_change.change_set.current_source_digest
    assert preflight.freshness_store.load_effective() == second_change.change_set

    repeat = preflight.run()
    assert repeat.fact_sync.index_generation == second_change.fact_sync.index_generation
    assert repeat.change_set == second_change.change_set
    assert repeat.fact_sync.index_generation > initial_generation
    assert formal_store.load().manifest.cognition_baseline == first_change.change_set.baseline_source_digest


def test_preflight_rebuilds_deleted_cache_with_partial_entity_diff(tmp_path: Path) -> None:
    repository, formal_store, sync, preflight = _initialized_preflight(tmp_path)
    _write(repository.root, "app.py", "def answer() -> int:\n    return 2\n")
    sync.database.path.unlink()

    result = preflight.run()

    assert result.fact_sync.rebuilt
    assert result.change_set is not None
    assert result.change_set.entity_diff_completeness == "partial"
    assert result.repository_status.status == "pending"
    assert formal_store.load().manifest.cognition_initialized


def test_preflight_rejects_m0_technical_state_before_fact_sync(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    repository = Repository(tmp_path)
    _write(repository.root, "app.py", "value = 1\n")
    lock = RepositoryLock(repository.root)
    formal_store = FormalStore(repository)
    formal_store.initialize(
        FormalState.empty(
            manifest=Manifest(1, 0, False, None),
            source_baseline=SourceBaseline.empty(),
        )
    )
    sync = FactSyncService(repository, formal_store=formal_store, repository_lock=lock)
    preflight = PreflightService(
        formal_store=formal_store,
        fact_sync=sync,
        facts=sync.database,
        freshness_store=FreshnessStore(repository.root / ".codecortex" / ".cache"),
        repository_lock=lock,
    )

    with pytest.raises(CodeCortexError) as raised:
        preflight.run()

    assert raised.value.code is ErrorCode.NOT_INITIALIZED
    assert not sync.database.path.exists()


def test_preflight_never_writes_formal_cognition(tmp_path: Path) -> None:
    repository, _, _, preflight = _initialized_preflight(tmp_path)
    formal_paths = ("manifest.json", "graph.json", "entity_refs.json", "source_baseline.json")
    before = {
        path: (repository.root / ".codecortex" / path).read_bytes()
        for path in formal_paths
    }
    _write(repository.root, "app.py", "def answer() -> int:\n    return 2\n")

    result = preflight.run()

    assert result.change_set is not None
    assert {
        path: (repository.root / ".codecortex" / path).read_bytes()
        for path in formal_paths
    } == before


def test_preflight_parse_failure_never_reports_fresh(tmp_path: Path) -> None:
    repository, _, _, preflight = _initialized_preflight(tmp_path)
    _write(repository.root, "app.py", "def broken(:\n")

    result = preflight.run()

    assert result.change_set is not None
    assert result.change_set.scope_confidence == "unknown"
    assert result.repository_status.status == "unresolved"


def test_preflight_retries_when_source_changes_after_fact_sync(tmp_path: Path) -> None:
    repository, _, sync, preflight = _initialized_preflight(tmp_path)
    original_probe = sync.probe_source_digest
    raced = False

    def mutate_before_final_probe() -> str:
        nonlocal raced
        if not raced:
            raced = True
            _write(repository.root, "app.py", "def answer() -> int:\n    return 2\n")
        return original_probe()

    with (
        patch.object(sync, "probe_source_digest", side_effect=mutate_before_final_probe),
        patch.object(sync, "sync", wraps=sync.sync) as synchronize,
    ):
        result = preflight.run()

    assert synchronize.call_count == 2
    assert result.fact_sync.repository_source_digest == original_probe()
    assert result.change_set is not None
