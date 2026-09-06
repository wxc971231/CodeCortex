"""Direct fail-closed tests for Analyzer SQLite path coordinates."""

import sqlite3
from pathlib import Path

import pytest

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
