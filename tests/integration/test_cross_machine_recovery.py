"""Recovery of disposable M1b caches from Git-carried formal state."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.preflight import PreflightService
from codecortex.application.recovery import RecoveryService
from codecortex.application.replica_providers import (
    formal_entity_ref_provider,
    formal_history_event_provider,
)
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
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.infrastructure.repository import Repository


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _initialize_origin(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    repository = Repository(root)
    _write(root, ".gitignore", ".codecortex/.cache/\n")
    _write(root, "src/pkg/a.py", "def answer() -> int:\n    return 1\n")
    _write(root, "src/pkg/__init__.py", "")
    lock = RepositoryLock(root)
    formal = FormalStore(repository)
    seed = FactSyncService(repository, repository_lock=lock)
    result = seed.sync("full")
    baseline = SourceBaseline(
        1,
        1,
        1,
        result.repository_source_digest,
        tuple(
            {"relative_path": path, "content_digest": digest}
            for path, digest in seed.database.source_file_digests().items()
        ),
    )
    formal.initialize(
        FormalState(
            manifest=Manifest(1, 0, True, result.repository_source_digest),
            graph=CognitiveGraph.empty(),
            entity_refs=EntityRefs.empty(),
            source_baseline=baseline,
        )
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "formal baseline",
        ],
        check=True,
    )


def _recovery(root: Path) -> RecoveryService:
    repository = Repository(root)
    formal = FormalStore(repository)
    lock = RepositoryLock(root)
    facts = FactsDatabase(root / ".codecortex" / ".cache" / "facts.sqlite3")
    return RecoveryService(
        formal_store=formal,
        fact_sync=FactSyncService(
            repository,
            database=facts,
            formal_store=formal,
            repository_lock=lock,
        ),
        facts=facts,
        freshness_store=FreshnessStore(root / ".codecortex" / ".cache"),
        repository_lock=lock,
        cognitive_replica=GraphReplica.create_new(
            root / ".codecortex" / ".cache" / "cognitive.sqlite3",
            entity_refs=formal_entity_ref_provider(formal),
            history_events=formal_history_event_provider(formal),
        ),
    )


@pytest.fixture
def cloned_repo_without_cache(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    _initialize_origin(origin)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    assert not (clone / ".codecortex" / ".cache").exists()
    return clone


def _formal_bytes(root: Path) -> dict[str, bytes]:
    paths = (
        "manifest.json",
        "graph.json",
        "entity_refs.json",
        "source_baseline.json",
        "view_manifest.json",
    )
    return {
        path: (root / ".codecortex" / path).read_bytes()
        for path in paths
        if (root / ".codecortex" / path).exists()
    }


def test_clone_with_changed_source_recovers_exact_file_diff(
    cloned_repo_without_cache: Path,
) -> None:
    _write(cloned_repo_without_cache, "src/pkg/a.py", "changed = True\n")
    before = _formal_bytes(cloned_repo_without_cache)

    result = _recovery(cloned_repo_without_cache).ensure_cache()

    assert result.change_set is not None
    assert result.change_set.changed_files.modified == ("src/pkg/a.py",)
    assert result.change_set.file_diff_completeness == "complete"
    assert result.change_set.entity_diff_completeness == "partial"
    assert result.agent_calls == 0
    assert result.rebuilt_facts is True
    assert result.rebuilt_replica is True
    assert _formal_bytes(cloned_repo_without_cache) == before


def test_changed_non_owned_existing_file_is_not_complete_after_partial_recovery(
    cloned_repo_without_cache: Path,
) -> None:
    _write(
        cloned_repo_without_cache,
        "src/pkg/a.py",
        "def unrelated_current_behavior() -> int:\n    return 2\n",
    )

    result = _recovery(cloned_repo_without_cache).ensure_cache()

    assert result.change_set is not None
    assert result.change_set.entity_diff_completeness == "partial"
    assert result.change_set.scope_confidence == "partial"
    assert {
        (item["relative_path"], item["reason"])
        for item in result.change_set.unmapped_changes
    } == {("src/pkg/a.py", "changed_path_has_no_formal_owner")}


def test_clean_clone_becomes_fresh_with_complete_current_baseline(
    cloned_repo_without_cache: Path,
) -> None:
    result = _recovery(cloned_repo_without_cache).ensure_cache()

    assert result.change_set is None
    assert result.repository_status.status == "fresh"
    assert result.facts.cache_metadata().baseline_entity_snapshot_completeness == "complete"


def test_crlf_only_clone_change_does_not_create_change_set(
    cloned_repo_without_cache: Path,
) -> None:
    _write(
        cloned_repo_without_cache,
        "src/pkg/a.py",
        "def answer() -> int:\r\n    return 1\r\n",
    )

    result = _recovery(cloned_repo_without_cache).ensure_cache()

    assert result.change_set is None


def test_corrupt_local_cache_is_replaced_without_formal_mutation(
    cloned_repo_without_cache: Path,
) -> None:
    cache = cloned_repo_without_cache / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    (cache / "facts.sqlite3").write_bytes(b"not a sqlite database")
    (cache / "cognitive.sqlite3").write_bytes(b"not a sqlite database")
    before = _formal_bytes(cloned_repo_without_cache)

    recovery = _recovery(cloned_repo_without_cache)
    assert recovery.requires_recovery() is True
    result = recovery.ensure_cache()

    assert result.rebuilt_facts is True
    assert result.change_set is None
    assert result.facts.integrity_ok() is True
    assert _formal_bytes(cloned_repo_without_cache) == before


def test_coordinate_mismatched_cache_is_rebuilt_from_formal_revision(
    cloned_repo_without_cache: Path,
) -> None:
    recovery = _recovery(cloned_repo_without_cache)
    recovery.fact_sync.sync("full")
    recovery.facts.advance_graph_revision(99)

    assert recovery.requires_recovery() is True
    result = recovery.ensure_cache()

    assert result.rebuilt_facts is True
    assert result.fact_sync.graph_revision == 0
    assert recovery.facts.cache_metadata().graph_revision == 0


def test_preflight_routes_missing_clone_cache_through_recovery(
    cloned_repo_without_cache: Path,
) -> None:
    recovery = _recovery(cloned_repo_without_cache)
    preflight = PreflightService(
        formal_store=recovery.formal_store,
        fact_sync=recovery.fact_sync,
        facts=recovery.facts,
        freshness_store=recovery.freshness_store,
        repository_lock=recovery.repository_lock,
        recovery_service=recovery,
    )

    result = preflight.run()

    assert result.fact_sync.rebuilt is True
    assert result.change_set is None
    assert result.repository_status.status == "fresh"
    assert recovery.requires_recovery() is False


def test_unsupported_formal_schema_fails_before_cache_mutation(
    cloned_repo_without_cache: Path,
) -> None:
    manifest = cloned_repo_without_cache / ".codecortex" / "manifest.json"
    payload = manifest.read_text(encoding="utf-8").replace('"schema_version": 1', '"schema_version": 999')
    manifest.write_text(payload, encoding="utf-8")
    recovery = _recovery(cloned_repo_without_cache)

    with pytest.raises(CodeCortexError) as raised:
        recovery.ensure_cache()

    assert raised.value.code is ErrorCode.UNSUPPORTED_SCHEMA
    assert not recovery.facts.path.exists()
