"""Atomic replacement behavior for the disposable fact-cache database."""

import subprocess
from pathlib import Path

from codecortex.application.fact_sync import FactSyncService
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.repository import Repository


def _repository(tmp_path: Path) -> Repository:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return Repository(tmp_path)


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_corrupt_cache_is_replaced_from_current_sources(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "answer = 42\n")
    service = FactSyncService(repository)
    first = service.sync()

    service.database.path.write_bytes(b"not a sqlite database")
    repaired = service.sync("auto")

    metadata = FactsDatabase(service.database.path).cache_metadata()
    assert repaired.rebuilt
    assert repaired.index_generation == 1
    assert metadata.repository_source_digest == first.repository_source_digest
    assert FactsDatabase(service.database.path).integrity_ok()


def test_full_sync_replaces_database_without_wal_sidecars(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "answer = 1\n")
    service = FactSyncService(repository)
    service.sync()
    _write(repository.root, "a.py", "answer = 2\n")

    result = service.sync("full")

    assert result.rebuilt
    assert not Path(f"{service.database.path}-wal").exists()
    assert not Path(f"{service.database.path}-shm").exists()
    assert FactsDatabase(service.database.path).integrity_ok()
