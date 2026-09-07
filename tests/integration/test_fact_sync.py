"""End-to-end current-fact synchronization over real temporary Git repositories."""

import subprocess
from contextlib import contextmanager
from pathlib import Path

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.services import ApplicationServices
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.facts_db import FactScope, FactsDatabase
from codecortex.infrastructure.python.parser import parse_python_file
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views


def _repository(tmp_path: Path) -> Repository:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return Repository(tmp_path)


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _service(repository: Repository, **kwargs: object) -> FactSyncService:
    return FactSyncService(repository, **kwargs)


def test_incremental_rechecks_parse_coverage_after_concurrent_cache_change(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original = "def original():\n    return 1\n"
    _write(repository.root, "app.py", original)
    peer = _service(repository)
    initial = peer.sync()
    expected = peer.database.current_entity_snapshots()
    lock = RepositoryLock(repository.root)

    class InterleavingLock:
        fired = False

        @contextmanager
        def acquire(self, mode, timeout_seconds):
            if not self.fired:
                self.fired = True
                _write(repository.root, "app.py", "def interim():\n    return 2\n")
                peer.sync()
                _write(repository.root, "app.py", original)
            with lock.acquire(mode, timeout_seconds):
                yield

    service = _service(repository, repository_lock=InterleavingLock())
    result = service.sync()
    assert result.repository_source_digest == initial.repository_source_digest
    assert service.database.source_file_digests() == service._source_snapshot().digests_by_path
    assert {(item.address, item.fingerprint) for item in service.database.current_entity_snapshots()} == {
        (item.address, item.fingerprint) for item in expected
    }


def test_full_sync_preserves_historical_baseline_entities(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "app.py", "def removed():\n    return 1\n")
    service = _service(repository)
    initial = service.sync()
    service.database.replace_baseline_entity_snapshots(initial.repository_source_digest)
    expected = service.database.baseline_entity_snapshots()
    _write(repository.root, "app.py", "value = 2\n")
    service.sync("full")
    assert service.database.baseline_entity_snapshots() == expected
    assert service.database.cache_metadata().baseline_entity_snapshot_completeness == "complete"


def test_unchanged_auto_sync_does_not_advance_generation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "pkg/a.py", "value = 1\n")
    service = _service(repository)

    first = service.sync("auto")
    second = service.sync("auto")

    assert first.index_generation == 1
    assert second.index_generation == first.index_generation
    assert second.parsed_files == 0
    assert not second.rebuilt
    assert FactsDatabase(service.database.path).cache_metadata().repository_source_digest == (
        second.repository_source_digest
    )


def test_fact_sync_reads_formal_revision_without_mutating_formal_state(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "value = 1\n")
    formal_store = FormalStore(repository)
    application = ApplicationServices(
        repository=repository,
        formal_store=formal_store,
        repository_lock=RepositoryLock(repository.root),
        view_renderer=render_views,
    )
    application.initialize_repository()
    formal_paths = (
        "manifest.json",
        "graph.json",
        "entity_refs.json",
        "source_baseline.json",
        "config.toml",
    )
    before = {
        path: (repository.root / ".codecortex" / path).read_bytes()
        for path in formal_paths
    }

    result = FactSyncService(repository, formal_store=formal_store).sync()

    assert result.graph_revision == 0
    assert {
        path: (repository.root / ".codecortex" / path).read_bytes()
        for path in formal_paths
    } == before


def test_incremental_add_modify_delete_and_parse_error(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "consumer.py", "from target import Service\n")
    service = _service(repository)
    service.sync()

    _write(repository.root, "target.py", "class Service: pass\n")
    added = service.sync()
    assert (added.added_files, added.changed_files, added.deleted_files) == (1, 0, 0)
    assert service.database.relations_for_path("consumer.py")[0].target_address == "target:Service"

    _write(repository.root, "target.py", "def broken(:\n")
    modified = service.sync()
    assert (modified.added_files, modified.changed_files, modified.deleted_files) == (0, 1, 0)
    with service.database.open_read() as connection:
        diagnostic = connection.execute(
            "SELECT code FROM diagnostics WHERE code = 'PYTHON_SYNTAX_ERROR'"
        ).fetchone()
    assert diagnostic is not None
    assert service.database.relations_for_path("consumer.py")[0].resolution_status == "unresolved"

    (repository.root / "target.py").unlink()
    deleted = service.sync()
    assert (deleted.added_files, deleted.changed_files, deleted.deleted_files) == (0, 0, 1)
    assert service.database.relations_for_path("consumer.py")[0].resolution_status == "unresolved"


def test_unique_digest_rename_preserves_entity_identity(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "old.py", "def work(value: int) -> int:\n    return value\n")
    service = _service(repository)
    service.sync()
    old = next(
        entity
        for entity in service.database.query_entities(FactScope.module("old"), None, 10).items
        if entity.qualname == "work"
    )

    (repository.root / "old.py").unlink()
    _write(repository.root, "new.py", "def work(value: int) -> int:\n    return value\n")
    renamed = service.sync()
    new = next(
        entity
        for entity in service.database.query_entities(FactScope.module("new"), None, 10).items
        if entity.qualname == "work"
    )

    assert (renamed.added_files, renamed.deleted_files) == (1, 1)
    assert new.uid == old.uid


def test_source_change_after_parse_retries_before_commit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "value = 1\n")
    calls = 0

    def parse_then_change(source, previous):
        nonlocal calls
        parsed = parse_python_file(source, previous)
        calls += 1
        if calls == 1:
            _write(repository.root, "a.py", "value = 2\n")
        return parsed

    service = _service(repository, parse_file=parse_then_change)
    result = service.sync()

    assert result.retry_count == 1
    assert result.repository_source_digest == _service(repository).sync().repository_source_digest
    assert service.database.query_entities(
        # This also proves the stale parse never became the visible cache.
        FactScope.module("a"),
        cursor=None,
        limit=10,
    ).items[0].fingerprint


def test_full_and_incremental_fact_results_are_equivalent(tmp_path: Path) -> None:
    incremental_root = tmp_path / "incremental"
    full_root = tmp_path / "full"
    incremental = _service(_repository(incremental_root))
    full = _service(_repository(full_root))
    for root in (incremental.repository.root, full.repository.root):
        _write(root, "consumer.py", "from target import Service\n\ndef use():\n    return Service()\n")
        _write(root, "target.py", "class Service: pass\n")

    incremental.sync()
    _write(incremental.repository.root, "target.py", "class Service:\n    pass\n")
    incremental.sync()
    _write(full.repository.root, "target.py", "class Service:\n    pass\n")
    full.sync("full")

    assert _fact_projection(incremental.database) == _fact_projection(full.database)


def _fact_projection(database: FactsDatabase) -> tuple[tuple[str, str, str], ...]:
    with database.open_read() as connection:
        rows = connection.execute(
            "SELECT relative_path, content_digest, parse_status FROM source_files "
            "ORDER BY relative_path"
        ).fetchall()
    return tuple((row["relative_path"], row["content_digest"], row["parse_status"]) for row in rows)
