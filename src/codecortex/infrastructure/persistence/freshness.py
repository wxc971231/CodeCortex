"""Atomic machine-local storage for the one effective ChangeSet."""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from codecortex.domain.freshness import (
    ChangeSet,
    DiffCompleteness,
    EntityChanges,
    FileChanges,
    ScopeConfidence,
)
from codecortex.domain.ids import IdPrefix, validate_id
from codecortex.infrastructure.jsonio import canonical_json_bytes


class FreshnessStore:
    """Persist an effective ChangeSet without treating the cache as formal truth."""

    def __init__(
        self, cache_root: Path, *, repository_root: Path | None = None
    ) -> None:
        self.cache_root = Path(cache_root)
        self._repository_root = (
            self.cache_root.parent.parent
            if repository_root is None
            else Path(repository_root)
        )
        self.freshness_path = self.cache_root / "freshness.json"
        self.change_sets_path = self.cache_root / "change_sets"

    def change_set_path(self, change_set_id: str) -> Path:
        """Return a payload path only for a strict, non-path ChangeSet ID."""
        identifier = validate_id(change_set_id, IdPrefix.CHANGE_SET)
        return self.change_sets_path / f"{identifier}.json"

    def replace_effective(self, change_set: ChangeSet | None) -> None:
        """Atomically repoint freshness to one payload, or clear it when fresh.

        The new payload is durable before the pointer moves.  A crash after the
        pointer move may leave an orphaned old cache payload, which is harmless:
        only the pointer identifies the effective ChangeSet and the next write
        removes the prior target.
        """
        previous = self._effective_id_or_none()
        previous_path = (
            None if previous is None else self.change_set_path(previous)
        )
        if change_set is None:
            if previous is None:
                return
            with self._open_cache_directories(create=True) as (cache_fd, change_sets_fd):
                assert previous_path is not None
                _require_regular_entry(change_sets_fd, previous_path.name)
                _require_regular_entry(cache_fd, self.freshness_path.name)
                _unlink_regular_at(cache_fd, self.freshness_path.name)
                _unlink_regular_at(change_sets_fd, previous_path.name)
            return

        with self._open_cache_directories(create=True) as (cache_fd, change_sets_fd):
            if previous_path is not None and previous != change_set.change_set_id:
                _require_regular_entry(change_sets_fd, previous_path.name)
            _write_json_atomic_at(
                change_sets_fd,
                self.change_set_path(change_set.change_set_id).name,
                _to_json(change_set),
            )
            _write_json_atomic_at(
                cache_fd,
                self.freshness_path.name,
                {
                    "schema_version": 1,
                    "effective_change_set_id": change_set.change_set_id,
                },
            )
            if previous is not None and previous != change_set.change_set_id:
                assert previous_path is not None
                _unlink_regular_at(change_sets_fd, previous_path.name)

    def load_effective(self) -> ChangeSet | None:
        change_set_id = self._effective_id_or_none()
        if change_set_id is None:
            return None
        path = self.change_set_path(change_set_id)
        try:
            raw = json.loads(_read_text_no_follow(path, self._repository_root))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Effective ChangeSet cache payload is unreadable") from error
        if not isinstance(raw, Mapping):
            raise TypeError("Effective ChangeSet cache payload must be an object")
        return _from_json(raw)

    def _effective_id_or_none(self) -> str | None:
        try:
            encoded = _read_text_no_follow(
                self.freshness_path, self._repository_root
            )
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            raise ValueError("Freshness cache pointer is unreadable") from error
        try:
            raw = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError("Freshness cache pointer is unreadable") from error
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "effective_change_set_id",
        }:
            raise ValueError("Freshness cache pointer has an invalid shape")
        if raw["schema_version"] != 1 or not isinstance(
            raw["effective_change_set_id"], str
        ):
            raise ValueError("Freshness cache pointer is unsupported")
        return raw["effective_change_set_id"]

    @contextmanager
    def _open_cache_directories(self, *, create: bool) -> Iterator[tuple[int, int]]:
        """Open cache and payload directories through an anchored descriptor chain."""
        root = Path(os.path.abspath(self._repository_root))
        cache = Path(os.path.abspath(self.cache_root))
        try:
            relative = cache.relative_to(root)
        except ValueError as error:
            raise OSError("Freshness cache path escapes the repository") from error
        if not relative.parts:
            raise OSError("Freshness cache path does not name a directory")

        directories: list[int] = []
        try:
            if create and not root.exists():
                root.mkdir(parents=True, exist_ok=True)
            directories.append(os.open(root, _DIRECTORY_FLAGS))
            for component in relative.parts:
                parent = directories[-1]
                try:
                    descriptor = os.open(
                        component, _DIRECTORY_FLAGS, dir_fd=parent
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=parent)
                    descriptor = os.open(
                        component, _DIRECTORY_FLAGS, dir_fd=parent
                    )
                directories.append(descriptor)
            cache_fd = directories[-1]
            try:
                change_sets_fd = os.open(
                    "change_sets", _DIRECTORY_FLAGS, dir_fd=cache_fd
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir("change_sets", 0o755, dir_fd=cache_fd)
                change_sets_fd = os.open(
                    "change_sets", _DIRECTORY_FLAGS, dir_fd=cache_fd
                )
            try:
                directories.append(change_sets_fd)
                yield cache_fd, change_sets_fd
            finally:
                os.close(change_sets_fd)
                directories.pop()
        finally:
            for descriptor in reversed(directories):
                os.close(descriptor)


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _read_text_no_follow(path: Path, repository_root: Path) -> str:
    """Read one regular repository file through an anchored descriptor chain."""
    if ".." in path.parts:
        raise OSError("Freshness cache path contains parent traversal")
    root = Path(os.path.abspath(repository_root))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise OSError("Freshness cache path escapes the repository") from error
    if not relative.parts:
        raise OSError("Freshness cache path does not name a file")

    directories: list[int] = []
    descriptor: int | None = None
    try:
        directories.append(os.open(root, _DIRECTORY_FLAGS))
        for component in relative.parts[:-1]:
            directories.append(
                os.open(component, _DIRECTORY_FLAGS, dir_fd=directories[-1])
            )
        descriptor = os.open(
            relative.parts[-1], _READ_FLAGS, dir_fd=directories[-1]
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Freshness cache target is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return stream.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def _write_json_atomic_at(directory_fd: int, filename: str, value: object) -> None:
    """Durably replace one regular JSON entry within an already-open directory."""
    payload = canonical_json_bytes(value)
    temporary_name: str | None = None
    temporary_fd: int | None = None
    for _ in range(10):
        candidate = f".{filename}.{secrets.token_hex(8)}.tmp"
        try:
            temporary_fd = os.open(
                candidate,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        temporary_name = candidate
        break
    if temporary_name is None or temporary_fd is None:
        raise OSError(errno.EEXIST, "Could not allocate freshness cache temp file")

    installed = False
    try:
        with os.fdopen(temporary_fd, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        _require_regular_entry(directory_fd, filename)
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        installed = True
        os.fsync(directory_fd)
    finally:
        if not installed:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _require_regular_entry(directory_fd: int, filename: str) -> None:
    try:
        mode = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise OSError("Freshness cache target is not a regular file")


def _unlink_regular_at(directory_fd: int, filename: str) -> None:
    try:
        mode = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise OSError("Freshness cache target is not a regular file")
    os.unlink(filename, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _to_json(change_set: ChangeSet) -> dict[str, object]:
    return {
        "schema_version": change_set.schema_version,
        "change_set_id": change_set.change_set_id,
        "baseline_source_digest": change_set.baseline_source_digest,
        "current_source_digest": change_set.current_source_digest,
        "created_at": change_set.created_at,
        "changed_files": {
            "added": list(change_set.changed_files.added),
            "modified": list(change_set.changed_files.modified),
            "deleted": list(change_set.changed_files.deleted),
            "renamed": [list(pair) for pair in change_set.changed_files.renamed],
        },
        "changed_entities": {
            "added": list(change_set.changed_entities.added),
            "modified": list(change_set.changed_entities.modified),
            "missing": list(change_set.changed_entities.missing),
            "moved": [list(record) for record in change_set.changed_entities.moved],
        },
        "file_diff_completeness": change_set.file_diff_completeness,
        "entity_diff_completeness": change_set.entity_diff_completeness,
        "affected_nodes": list(change_set.affected_nodes),
        "affected_flows": list(change_set.affected_flows),
        "affected_entities": list(change_set.affected_entities),
        "scope_confidence": change_set.scope_confidence,
        "unmapped_changes": list(change_set.unmapped_changes),
        "diagnostics": list(change_set.diagnostics),
    }


def _from_json(raw: Mapping[str, object]) -> ChangeSet:
    expected = {
        "schema_version", "change_set_id", "baseline_source_digest",
        "current_source_digest", "created_at", "changed_files", "changed_entities",
        "file_diff_completeness", "entity_diff_completeness", "affected_nodes",
        "affected_flows", "affected_entities", "scope_confidence", "unmapped_changes",
        "diagnostics",
    }
    if set(raw) != expected:
        raise ValueError("ChangeSet cache payload has an invalid shape")
    files = _object(raw["changed_files"], "changed_files")
    entities = _object(raw["changed_entities"], "changed_entities")
    return ChangeSet(
        schema_version=_integer(raw["schema_version"], "schema_version"),
        change_set_id=_string(raw["change_set_id"], "change_set_id"),
        baseline_source_digest=_string(raw["baseline_source_digest"], "baseline_source_digest"),
        current_source_digest=_string(raw["current_source_digest"], "current_source_digest"),
        created_at=_string(raw["created_at"], "created_at"),
        changed_files=FileChanges(
            added=_strings(files.get("added"), "changed_files.added"),
            modified=_strings(files.get("modified"), "changed_files.modified"),
            deleted=_strings(files.get("deleted"), "changed_files.deleted"),
            renamed=_pairs(files.get("renamed"), "changed_files.renamed"),
        ),
        changed_entities=EntityChanges(
            added=_strings(entities.get("added"), "changed_entities.added"),
            modified=_strings(entities.get("modified"), "changed_entities.modified"),
            missing=_strings(entities.get("missing"), "changed_entities.missing"),
            moved=_triples(entities.get("moved"), "changed_entities.moved"),
        ),
        file_diff_completeness=_diff_completeness(
            raw["file_diff_completeness"], "file_diff_completeness"
        ),
        entity_diff_completeness=_diff_completeness(
            raw["entity_diff_completeness"], "entity_diff_completeness"
        ),
        affected_nodes=_strings(raw["affected_nodes"], "affected_nodes"),
        affected_flows=_strings(raw["affected_flows"], "affected_flows"),
        affected_entities=_strings(raw["affected_entities"], "affected_entities"),
        scope_confidence=_scope_confidence(raw["scope_confidence"]),
        unmapped_changes=tuple(
            _object(item, "unmapped_changes item")
            for item in _sequence(raw["unmapped_changes"], "unmapped_changes")
        ),
        diagnostics=_strings(raw["diagnostics"], "diagnostics"),
    )


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    return tuple(value)


def _strings(value: object, name: str) -> tuple[str, ...]:
    items = _sequence(value, name)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{name} must contain strings")
    return cast(tuple[str, ...], tuple(items))


def _pairs(value: object, name: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in _sequence(value, name):
        values = _strings(item, f"{name} item")
        if len(values) != 2:
            raise ValueError(f"{name} items must contain exactly two strings")
        pairs.append((values[0], values[1]))
    return tuple(pairs)


def _triples(value: object, name: str) -> tuple[tuple[str, str, str], ...]:
    triples: list[tuple[str, str, str]] = []
    for item in _sequence(value, name):
        values = _strings(item, f"{name} item")
        if len(values) != 3:
            raise ValueError(f"{name} items must contain exactly three strings")
        triples.append((values[0], values[1], values[2]))
    return tuple(triples)


def _diff_completeness(value: object, name: str) -> DiffCompleteness:
    text = _string(value, name)
    if text not in ("complete", "partial"):
        raise ValueError(f"{name} is invalid")
    return cast(DiffCompleteness, text)


def _scope_confidence(value: object) -> ScopeConfidence:
    text = _string(value, "scope_confidence")
    if text not in ("complete", "partial", "unknown"):
        raise ValueError("scope_confidence is invalid")
    return cast(ScopeConfidence, text)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value
