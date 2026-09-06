"""No-follow path validation for Analyzer SQLite read handles."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path


def read_only_sqlite_uri(
    database_path: Path, repository_root: Path, *, label: str
) -> str:
    """Return a fail-closed URI for one repository-contained SQLite coordinate."""
    if ".." in database_path.parts:
        raise sqlite3.OperationalError(
            f"Read-only {label} path is unsafe: parent traversal"
        )
    root = Path(os.path.abspath(repository_root))
    database = Path(os.path.abspath(database_path))
    _require_regular_path(database, root, allow_missing=False, label=label)
    wal = database.with_name(f"{database.name}-wal")
    shm = database.with_name(f"{database.name}-shm")
    wal_exists = _require_regular_path(wal, root, allow_missing=True, label=label)
    shm_exists = _require_regular_path(shm, root, allow_missing=True, label=label)
    if wal_exists != shm_exists:
        raise sqlite3.OperationalError(
            f"Read-only {label} has an incomplete WAL coordinate"
        )
    uri = f"{database.as_uri()}?mode=ro&nofollow=1"
    return uri if wal_exists else f"{uri}&immutable=1"


def _require_regular_path(
    path: Path,
    repository_root: Path,
    *,
    allow_missing: bool,
    label: str,
) -> bool:
    try:
        relative = path.relative_to(repository_root)
    except ValueError as error:
        raise sqlite3.OperationalError(
            f"Read-only {label} path is unsafe"
        ) from error
    current = repository_root
    for index, component in enumerate(relative.parts):
        current /= component
        is_final = index == len(relative.parts) - 1
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing and is_final:
                return False
            raise sqlite3.OperationalError(
                f"Read-only {label} path is missing or unsafe"
            ) from None
        if stat.S_ISLNK(mode):
            raise sqlite3.OperationalError(
                f"Read-only {label} path is unsafe: symbolic link"
            )
        if is_final:
            if not stat.S_ISREG(mode):
                raise sqlite3.OperationalError(
                    f"Read-only {label} path is unsafe: not a regular file"
                )
        elif not stat.S_ISDIR(mode):
            raise sqlite3.OperationalError(
                f"Read-only {label} path is unsafe: parent is not a directory"
            )
    return True
