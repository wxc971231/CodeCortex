"""Direct fail-closed tests for Analyzer SQLite path coordinates."""

import sqlite3
from pathlib import Path

import pytest

from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.sqlite_safety import read_only_sqlite_uri


def test_read_only_sqlite_uri_rejects_parent_traversal_outside_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    cache = repository / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"outside")
    traversing_path = cache / ".." / ".." / ".." / outside.name

    with pytest.raises(sqlite3.OperationalError, match="unsafe"):
        read_only_sqlite_uri(
            traversing_path, repository, label="cognitive replica"
        )


def test_fact_cache_write_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    cache = repository / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside.sqlite3"
    FactsDatabase.create_new(outside)
    before = outside.read_bytes()
    cache_path = cache / "facts.sqlite3"
    cache_path.symlink_to(outside)

    with pytest.raises(sqlite3.OperationalError, match="unsafe"):
        FactsDatabase(cache_path, repository_root=repository).open_write()

    assert outside.read_bytes() == before


def test_main_fact_cache_read_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    cache = repository / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside.sqlite3"
    FactsDatabase.create_new(outside)
    before = outside.read_bytes()
    cache_path = cache / "facts.sqlite3"
    cache_path.symlink_to(outside)

    with pytest.raises(sqlite3.OperationalError, match="unsafe"):
        FactsDatabase(cache_path, repository_root=repository).open_read()

    assert outside.read_bytes() == before


def test_main_fact_cache_read_allows_regular_incomplete_wal_coordinate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    cache = repository / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    database = cache / "facts.sqlite3"
    database.write_bytes(b"regular sqlite placeholder")
    facts_shm = database.with_name("facts.sqlite3-shm")
    facts_shm.write_bytes(b"regular transient sidecar")
    before = facts_shm.read_bytes()

    uri = read_only_sqlite_uri(
        database,
        repository,
        label="fact cache",
        allow_incomplete_wal=True,
    )

    assert uri.endswith("?mode=ro&nofollow=1&immutable=1")
    assert facts_shm.read_bytes() == before


def test_analyzer_fact_cache_read_rejects_incomplete_wal_coordinate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    cache = repository / ".codecortex" / ".cache"
    cache.mkdir(parents=True)
    database = cache / "facts.sqlite3"
    database.write_bytes(b"regular sqlite placeholder")
    database.with_name("facts.sqlite3-wal").write_bytes(b"orphaned sidecar")

    with pytest.raises(sqlite3.OperationalError, match="incomplete WAL"):
        FactsDatabase(
            database, repository_root=repository, read_only=True
        ).open_read()
