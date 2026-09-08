"""Atomic replacement behavior for the disposable fact-cache database."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codecortex.application.fact_sync import FactSyncError, FactSyncService
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


def test_failed_replace_preserves_checkpointed_old_cache_coordinate(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "answer = 1\n")
    service = FactSyncService(repository)
    service.sync()
    old_connection = service.database.open_write()
    try:
        old_connection.execute(
            "UPDATE cache_metadata SET built_at = 'old-cache-sentinel'"
        )
        old_connection.commit()
        assert Path(f"{service.database.path}-wal").exists()
        _write(repository.root, "a.py", "answer = 2\n")

        with patch(
            "codecortex.application.fact_sync.os.replace",
            side_effect=OSError("simulated replace failure"),
        ), pytest.raises(OSError, match="simulated replace failure"):
            service.sync("full")

        assert FactsDatabase(service.database.path).integrity_ok()
        assert (
            FactsDatabase(service.database.path).cache_metadata().built_at
            == "old-cache-sentinel"
        )
    finally:
        old_connection.close()


def test_sidecars_created_during_replace_are_removed_before_publish_returns(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "answer = 1\n")
    service = FactSyncService(repository)
    service.sync()
    _write(repository.root, "a.py", "answer = 2\n")
    real_replace = os.replace

    def replace_then_race(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        Path(f"{destination}-wal").write_bytes(b"stale-old-wal")
        Path(f"{destination}-shm").write_bytes(b"stale-old-shm")

    with patch(
        "codecortex.application.fact_sync.os.replace",
        side_effect=replace_then_race,
    ):
        result = service.sync("full")

    assert result.rebuilt is True
    assert not Path(f"{service.database.path}-wal").exists()
    assert not Path(f"{service.database.path}-shm").exists()
    assert FactsDatabase(service.database.path).integrity_ok()


def test_busy_old_wal_fails_before_replacing_the_database(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _write(repository.root, "a.py", "answer = 1\n")
    service = FactSyncService(repository)
    service.sync()
    reader = service.database.open_read()
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT built_at FROM cache_metadata").fetchone()
        with service.database.open_write() as writer:
            writer.execute("UPDATE cache_metadata SET built_at = 'wal-sentinel'")
        _write(repository.root, "a.py", "answer = 2\n")

        with pytest.raises(FactSyncError, match="checkpoint remained busy"):
            service.sync("full")

        assert service.database.cache_metadata().built_at == "wal-sentinel"
    finally:
        reader.close()

    assert FactsDatabase(service.database.path).integrity_ok()
