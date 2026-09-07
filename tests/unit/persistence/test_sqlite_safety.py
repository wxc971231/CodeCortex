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
