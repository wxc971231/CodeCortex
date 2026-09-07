"""Formal transaction coverage for cognition-baseline advances."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from codecortex.application.baseline import (
    BaselineAdvanceService,
    DecisionRecord,
)
from codecortex.application.fact_sync import FactSyncService
from codecortex.application.preflight import PreflightService
from codecortex.application.recovery import RecoveryService
from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    Manifest,
    SourceBaseline,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.jsonio import canonical_json_bytes
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.repository import Repository


def _write(root: Path, source: str) -> None:
    (root / "app.py").write_text(source, encoding="utf-8")


def _service(tmp_path: Path) -> tuple[BaselineAdvanceService, Repository, FormalStore]:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    repository = Repository(root)
    _write(root, "def answer() -> int:\n    return 1\n")
    lock = RepositoryLock(root)
    formal_store = FormalStore(repository)
    seed_sync = FactSyncService(repository, repository_lock=lock)
    seed = seed_sync.sync()
    formal_store.initialize(
        FormalState(
            manifest=Manifest(1, 0, True, seed.repository_source_digest),
            graph=CognitiveGraph.empty(),
            entity_refs=EntityRefs.empty(),
            source_baseline=SourceBaseline(
                1,
                1,
                1,
                seed.repository_source_digest,
                tuple(
                    {"relative_path": path, "content_digest": digest}
                    for path, digest in seed_sync.database.source_file_digests().items()
                ),
            ),
        )
    )
    # The M1a apply path normally creates the first nonzero formal revision.
    # This focused fixture has no semantic patch, so move its empty graph to a
    # valid post-initialization revision before exercising baseline-only writes.
    for relative in ("manifest.json", "graph.json", "entity_refs.json"):
        path = root / ".codecortex" / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["graph_revision"] = 1
        path.write_bytes(canonical_json_bytes(payload))
    sync = FactSyncService(repository, formal_store=formal_store, repository_lock=lock)
    sync.database.replace_baseline_entity_snapshots(seed.repository_source_digest)
    freshness_store = FreshnessStore(root / ".codecortex" / ".cache")
    return (
        BaselineAdvanceService(
            formal_store=formal_store,
            fact_sync=sync,
            facts=sync.database,
            freshness_store=freshness_store,
            repository_lock=lock,
        ),
        repository,
        formal_store,
    )


def test_baseline_advance_writes_self_contained_event_without_graph_revision_change(
    tmp_path: Path,
) -> None:
    service, repository, formal_store = _service(tmp_path)
    _write(repository.root, "def answer() -> int:\n    return 2\n")
    preflight = service.preflight()
    assert preflight.change_set is not None

    result = service.advance(
        preflight.change_set.change_set_id,
        "no_semantic_change",
        DecisionRecord(
            decided_by="main_codex",
            evidence_summary="Reviewed current source; no cognitive change.",
            decided_at="2026-09-04T08:00:00Z",
        ),
        None,
    )

    state = formal_store.load()
    assert result.graph_revision == 1
    assert state.manifest.graph_revision == 1
    assert state.manifest.cognition_baseline == result.current_source_digest
    assert result.event["event_type"] == "cognition_baseline_advanced"
    assert result.event["change_set_summary"]["before_source_digest"] != result.event["change_set_summary"]["after_source_digest"]
    assert service.freshness_store.load_effective() is None


def test_baseline_snapshots_are_copied_before_releasing_formal_lock(tmp_path: Path) -> None:
    service, repository, formal_store = _service(tmp_path)
    _write(repository.root, "def answer() -> int:\n    return 2\n")
    ready = service.preflight()
    assert ready.change_set is not None
    accepted_digest = ready.change_set.current_source_digest
    expected = {(item.address, item.fingerprint) for item in service.facts.current_entity_snapshots()}
    lock = RepositoryLock(repository.root)

    class InterleavingLock:
        fired = False

        @contextmanager
        def acquire(self, mode, timeout_seconds):
            with lock.acquire(mode, timeout_seconds):
                yield
            if not self.fired and formal_store.load().manifest.cognition_baseline == accepted_digest:
                self.fired = True
                _write(repository.root, "def answer() -> int:\n    return 3\n")
                service.fact_sync.sync()

    service.repository_lock = InterleavingLock()
    service.advance(
        ready.change_set.change_set_id, "no_semantic_change",
        DecisionRecord("analyzer", "No semantic change.", "2026-09-04T08:00:00Z"), None,
    )
    snapshots = service.facts.baseline_entity_snapshots()
    assert {(item.address, item.fingerprint) for item in snapshots} == expected
    assert {item.baseline_source_digest for item in snapshots} == {accepted_digest}


def test_stale_change_set_does_not_change_formal_baseline(tmp_path: Path) -> None:
    service, repository, formal_store = _service(tmp_path)
    _write(repository.root, "def answer() -> int:\n    return 2\n")
    preflight = service.preflight()
    assert preflight.change_set is not None
    before = (repository.root / ".codecortex/manifest.json").read_bytes()
    _write(repository.root, "def answer() -> int:\n    return 3\n")

    with pytest.raises(CodeCortexError):
        service.advance(
            preflight.change_set.change_set_id,
            "no_semantic_change",
            DecisionRecord("analyzer", "No semantic change.", "2026-09-04T08:00:00Z"),
            None,
        )

    assert (repository.root / ".codecortex/manifest.json").read_bytes() == before
    assert formal_store.load().manifest.graph_revision == 1


def test_baseline_advance_fails_closed_on_post_sync_source_race(
    tmp_path: Path,
) -> None:
    service, repository, formal_store = _service(tmp_path)
    _write(repository.root, "def answer() -> int:\n    return 2\n")
    preflight = service.preflight()
    assert preflight.change_set is not None
    accepted_before = formal_store.load().manifest.cognition_baseline
    original_probe = service.fact_sync.probe_source_digest
    probe_count = 0

    def race_after_second_sync() -> str:
        nonlocal probe_count
        probe_count += 1
        if probe_count == 2:
            _write(repository.root, "def answer() -> int:\n    return 3\n")
        return original_probe()

    with patch.object(
        service.fact_sync, "probe_source_digest", side_effect=race_after_second_sync
    ), pytest.raises(CodeCortexError) as raised:
        service.advance(
            preflight.change_set.change_set_id,
            "no_semantic_change",
            DecisionRecord("analyzer", "No semantic change.", "2026-09-04T08:00:00Z"),
            None,
        )

    assert raised.value.code is ErrorCode.PROPOSAL_STALE
    assert formal_store.load().manifest.cognition_baseline == accepted_before


def test_baseline_advance_rejects_file_records_with_wrong_aggregate_digest(
    tmp_path: Path,
) -> None:
    service, repository, formal_store = _service(tmp_path)
    _write(repository.root, "def answer() -> int:\n    return 2\n")
    preflight = service.preflight()
    assert preflight.change_set is not None
    accepted_before = formal_store.load().manifest.cognition_baseline

    with patch.object(
        service.facts,
        "source_file_digests",
        return_value={"app.py": "sha256:" + "f" * 64},
    ), pytest.raises(CodeCortexError) as raised:
        service.advance(
            preflight.change_set.change_set_id,
            "no_semantic_change",
            DecisionRecord("analyzer", "No semantic change.", "2026-09-04T08:00:00Z"),
            None,
        )

    assert raised.value.code is ErrorCode.FORMAL_STATE_CORRUPT
    assert formal_store.load().manifest.cognition_baseline == accepted_before


def test_baseline_cache_failure_warns_then_next_preflight_recovers(
    tmp_path: Path,
) -> None:
    service, repository, formal_store = _service(tmp_path)
    _write(repository.root, "def answer() -> int:\n    return 2\n")
    preflight = service.preflight()
    assert preflight.change_set is not None

    with patch.object(
        service.facts,
        "replace_baseline_entity_snapshots",
        side_effect=OSError("simulated baseline snapshot write failure"),
    ):
        result = service.advance(
            preflight.change_set.change_set_id,
            "no_semantic_change",
            DecisionRecord("analyzer", "No semantic change.", "2026-09-04T08:00:00Z"),
            None,
        )

    assert result.cache_warnings
    assert ErrorCode.CACHE_REBUILD_REQUIRED.value in result.cache_warnings[0]
    assert formal_store.load().manifest.cognition_baseline == result.current_source_digest

    recovery = RecoveryService(
        formal_store=formal_store,
        fact_sync=service.fact_sync,
        facts=service.facts,
        freshness_store=service.freshness_store,
        repository_lock=service.repository_lock,
    )
    guarded = PreflightService(
        formal_store=formal_store,
        fact_sync=service.fact_sync,
        facts=service.facts,
        freshness_store=service.freshness_store,
        repository_lock=service.repository_lock,
        recovery_service=recovery,
    )
    assert recovery.requires_recovery() is True

    repaired = guarded.run()

    assert repaired.change_set is None
    assert recovery.requires_recovery() is False
    assert {
        snapshot.baseline_source_digest
        for snapshot in service.facts.baseline_entity_snapshots()
    } == {result.current_source_digest}
