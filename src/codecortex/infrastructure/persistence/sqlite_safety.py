"""No-follow path validation for Analyzer SQLite read handles."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
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


def read_write_sqlite_uri(
    database_path: Path, repository_root: Path, *, label: str
) -> str:
    """Return a no-follow write URI for one safe repository cache coordinate."""
    database, root = _normalized_coordinate(database_path, repository_root, label)
    _require_regular_path(database, root, allow_missing=True, label=label)
    for suffix in ("-wal", "-shm"):
        _require_regular_path(
            database.with_name(f"{database.name}{suffix}"),
            root,
            allow_missing=True,
            label=label,
        )
    return f"{database.as_uri()}?mode=rwc&nofollow=1"


def remove_sqlite_coordinate(
    database_path: Path, repository_root: Path, *, label: str
) -> None:
    """Remove only regular files from an anchored repository cache directory."""
    database, root = _normalized_coordinate(database_path, repository_root, label)
    try:
        relative_parent = database.parent.relative_to(root)
    except ValueError as error:
        raise _unsafe_path(label) from error
    with _open_directory(root, relative_parent.parts, label=label) as directory_fd:
        for name in (
            database.name,
            f"{database.name}-wal",
            f"{database.name}-shm",
        ):
            try:
                mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _unsafe_path(label) from error
            if not stat.S_ISREG(mode):
                raise _unsafe_path(label)
            os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)


def _normalized_coordinate(
    database_path: Path, repository_root: Path, label: str
) -> tuple[Path, Path]:
    if ".." in database_path.parts:
        raise sqlite3.OperationalError(
            f"Writable {label} path is unsafe: parent traversal"
        )
    return Path(os.path.abspath(database_path)), Path(os.path.abspath(repository_root))


@contextmanager
def _open_directory(
    root: Path, components: tuple[str, ...], *, label: str
) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, flags))
        for component in components:
            descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
        yield descriptors[-1]
    except sqlite3.OperationalError:
        raise
    except OSError as error:
        raise _unsafe_path(label) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unsafe_path(label: str) -> sqlite3.OperationalError:
    return sqlite3.OperationalError(f"Writable {label} path is unsafe")


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
