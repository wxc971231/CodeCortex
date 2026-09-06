"""Deterministic full and incremental synchronization of Python code facts.

The service deliberately performs expensive discovery, hashing, and AST parsing
before it takes the repository's exclusive lock.  It then rechecks the exact
managed-source snapshot under that lock; drift discards the work and retries.
Only a short SQLite transaction makes a cache generation visible.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from codecortex.application.ports import FormalStorePort, RepositoryLockPort
from codecortex.domain.facts import (
    DigestProfile,
    SourceConfig,
    SourceDiagnostic,
    SourceFileDigest,
)
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.facts_db import CacheMetadata, FactsDatabase
from codecortex.infrastructure.python.digest import (
    SourceDigestError,
    digest_source_file,
    repository_digest,
)
from codecortex.infrastructure.python.discovery import discover_python_source_set
from codecortex.infrastructure.python.parser import (
    EntityIdentityHint,
    ParsedFile,
    parse_python_file,
)
from codecortex.infrastructure.repository import Repository

SyncMode = Literal["auto", "full"]


class FactSyncError(ValueError):
    """Fact synchronization could not construct one complete source snapshot."""

    def __init__(self, message: str, diagnostics: Sequence[SourceDiagnostic] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


@dataclass(frozen=True)
class FactSyncResult:
    """The committed cache-generation outcome for one managed-source snapshot."""

    repository_source_digest: str
    graph_revision: int
    index_generation: int
    parsed_files: int
    added_files: int
    changed_files: int
    deleted_files: int
    retry_count: int
    rebuilt: bool
    diagnostics: tuple[SourceDiagnostic, ...]


@dataclass(frozen=True)
class _SourceSnapshot:
    files: tuple[SourceFileDigest, ...]
    repository_source_digest: str
    diagnostics: tuple[SourceDiagnostic, ...]

    @property
    def digests_by_path(self) -> dict[str, str]:
        return {item.source.relative_path: item.content_digest for item in self.files}


class FactSyncService:
    """Keep the disposable SQLite cache exact for the current Python sources."""

    parser_version = "python-ast-v1"

    def __init__(
        self,
        repository: Repository,
        *,
        database: FactsDatabase | None = None,
        repository_lock: RepositoryLockPort | None = None,
        formal_store: FormalStorePort | None = None,
        source_config: SourceConfig | None = None,
        digest_profile: DigestProfile | None = None,
        managed_source_set_version: int = 1,
        lock_timeout_seconds: float = 10,
        max_retries: int = 3,
        parse_file: Callable[[SourceFileDigest, Sequence[EntityIdentityHint]], ParsedFile] = parse_python_file,
    ) -> None:
        if type(managed_source_set_version) is not int or managed_source_set_version < 1:
            raise ValueError("Managed source set version must be a positive integer")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("Fact Sync max retries must be a non-negative integer")
        if (
            type(lock_timeout_seconds) not in (int, float)
            or lock_timeout_seconds < 0
        ):
            raise ValueError("Fact Sync lock timeout must be non-negative")
        self.repository = repository
        self.database = database or FactsDatabase(
            repository.root / ".codecortex" / ".cache" / "facts.sqlite3"
        )
        self.repository_lock = repository_lock or RepositoryLock(repository.root)
        self.formal_store = formal_store
        self.source_config = SourceConfig() if source_config is None else source_config
        self.digest_profile = DigestProfile() if digest_profile is None else digest_profile
        self.managed_source_set_version = managed_source_set_version
        self.lock_timeout_seconds = lock_timeout_seconds
        self.max_retries = max_retries
        self._parse_file = parse_file

    def sync(
        self,
        mode: SyncMode = "auto",
        *,
        identity_hints: Sequence[EntityIdentityHint] = (),
    ) -> FactSyncResult:
        """Synchronize facts, retrying if source content changes during parsing."""
        if mode not in ("auto", "full"):
            raise ValueError("Fact Sync mode must be 'auto' or 'full'")
        if any(not isinstance(hint, EntityIdentityHint) for hint in identity_hints):
            raise TypeError("Fact Sync identity hints must be EntityIdentityHint records")

        for retry_count in range(self.max_retries + 1):
            snapshot = self._source_snapshot()
            cache = self._cache_metadata_or_none()
            current_digests = self._cache_digests_or_empty(cache)
            full_rebuild = mode == "full" or not self._can_increment(
                cache, self._graph_revision()
            )
            parsed, deleted_paths, counts = self._parse_required_files(
                snapshot,
                current_digests,
                full_rebuild=full_rebuild,
                identity_hints=identity_hints,
            )

            with self.repository_lock.acquire(
                "exclusive", self.lock_timeout_seconds
            ):
                verified = self._source_snapshot()
                if not self._same_snapshot(snapshot, verified):
                    if retry_count == self.max_retries:
                        raise FactSyncError(
                            "Managed Python sources changed repeatedly during Fact Sync",
                            verified.diagnostics,
                        )
                    continue

                graph_revision = self._graph_revision()
                cache = self._cache_metadata_or_none()
                current_digests = self._cache_digests_or_empty(cache)
                cache_compatible = self._can_increment(cache, graph_revision)
                full_rebuild = mode == "full" or not cache_compatible
                if full_rebuild and not self._parsed_all(parsed, verified):
                    # A concurrent cache writer completed before us.  Reparse all
                    # sources outside the lock in the next loop rather than hold it.
                    if retry_count == self.max_retries:
                        raise FactSyncError("Fact cache changed repeatedly during rebuild")
                    continue

                changed_paths, deleted_paths, counts = self._changes(
                    verified, current_digests, full_rebuild=full_rebuild
                )
                if not full_rebuild and not changed_paths and not deleted_paths:
                    assert cache is not None
                    return FactSyncResult(
                        repository_source_digest=verified.repository_source_digest,
                        graph_revision=graph_revision,
                        index_generation=cache.index_generation,
                        parsed_files=0,
                        added_files=0,
                        changed_files=0,
                        deleted_files=0,
                        retry_count=retry_count,
                        rebuilt=False,
                        diagnostics=verified.diagnostics,
                    )

                if full_rebuild:
                    metadata = self._metadata(
                        verified, graph_revision, cache, facts_changed=not self._same_fact_input(cache, verified)
                    )
                    self._replace_with_full_snapshot(
                        parsed,
                        metadata,
                        verified.diagnostics,
                        checkpoint_existing=cache is not None,
                    )
                else:
                    # The first parse set was derived from the verified snapshot;
                    # it is safe only because the digest check above succeeded.
                    selected = tuple(
                        item
                        for item in parsed
                        if item.source.source.relative_path in set(changed_paths)
                    )
                    metadata = self._metadata(
                        verified, graph_revision, cache, facts_changed=bool(changed_paths or deleted_paths)
                    )
                    self.database.synchronize(
                        selected,
                        deleted_paths=deleted_paths,
                        metadata=metadata,
                        global_diagnostics=_global_diagnostics(verified.diagnostics),
                    )
                return FactSyncResult(
                    repository_source_digest=verified.repository_source_digest,
                    graph_revision=graph_revision,
                    index_generation=metadata.index_generation,
                    parsed_files=len(parsed) if full_rebuild else len(changed_paths),
                    added_files=counts["added"],
                    changed_files=counts["changed"],
                    deleted_files=counts["deleted"],
                    retry_count=retry_count,
                    rebuilt=full_rebuild,
                    diagnostics=verified.diagnostics,
                )
        raise AssertionError("Fact Sync retry loop unexpectedly exhausted")

    def probe_source_digest(self) -> str:
        """Hash the live managed source set without reading or writing cache state."""
        return self._source_snapshot().repository_source_digest

    def _source_snapshot(self) -> _SourceSnapshot:
        discovered = discover_python_source_set(self.repository, self.source_config)
        files: list[SourceFileDigest] = []
        diagnostics = list(discovered.diagnostics)
        for source in discovered.sources:
            try:
                files.append(digest_source_file(source))
            except SourceDigestError as error:
                diagnostics.append(
                    SourceDiagnostic(
                        relative_path=source.relative_path,
                        code="PYTHON_SOURCE_DECODE_ERROR",
                        message=str(error),
                    )
                )
        if len(files) != len(discovered.sources):
            raise FactSyncError(
                "One or more managed Python files cannot be decoded", diagnostics
            )
        return _SourceSnapshot(
            files=tuple(files),
            repository_source_digest=repository_digest(files, self.digest_profile),
            diagnostics=tuple(diagnostics),
        )

    def _parse_required_files(
        self,
        snapshot: _SourceSnapshot,
        current_digests: dict[str, str],
        *,
        full_rebuild: bool,
        identity_hints: Sequence[EntityIdentityHint],
    ) -> tuple[tuple[ParsedFile, ...], tuple[str, ...], dict[str, int]]:
        changed_paths, deleted_paths, counts = self._changes(
            snapshot, current_digests, full_rebuild=full_rebuild
        )
        if full_rebuild and identity_hints:
            hints = tuple(
                hint
                for hint in identity_hints
                if hint.relative_path in set(changed_paths)
            )
        else:
            try:
                hints = (
                    self.database.identity_hints((*changed_paths, *deleted_paths))
                    if self.database.path.exists()
                    else ()
                )
            except (OSError, sqlite3.DatabaseError):
                hints = ()
        hints_by_path: dict[str, list[EntityIdentityHint]] = {}
        for hint in hints:
            hints_by_path.setdefault(hint.relative_path, []).append(hint)
        by_path = {item.source.relative_path: item for item in snapshot.files}
        old_paths_by_digest: dict[str, list[str]] = {}
        for path in deleted_paths:
            old_paths_by_digest.setdefault(current_digests[path], []).append(path)
        new_paths_by_digest: dict[str, list[str]] = {}
        for path in changed_paths:
            if path not in current_digests:
                new_paths_by_digest.setdefault(by_path[path].content_digest, []).append(path)
        renamed_from: dict[str, str] = {}
        for digest, new_paths in new_paths_by_digest.items():
            old_paths = old_paths_by_digest.get(digest, [])
            if len(new_paths) == 1 and len(old_paths) == 1:
                renamed_from[new_paths.pop()] = old_paths.pop()
        parsed = tuple(
            self._parse_file(
                by_path[path],
                tuple(hints_by_path.get(renamed_from.get(path, path), ())),
            )
            for path in changed_paths
        )
        return parsed, deleted_paths, counts

    def _changes(
        self,
        snapshot: _SourceSnapshot,
        current_digests: dict[str, str],
        *,
        full_rebuild: bool,
    ) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, int]]:
        current = snapshot.digests_by_path
        if full_rebuild:
            changed = tuple(sorted(current))
        else:
            changed = tuple(
                path
                for path in sorted(current)
                if current_digests.get(path) != current[path]
            )
        deleted = tuple(sorted(set(current_digests) - set(current)))
        return (
            changed,
            deleted,
            {
                "added": sum(path not in current_digests for path in changed),
                "changed": sum(path in current_digests for path in changed),
                "deleted": len(deleted),
            },
        )

    def _cache_metadata_or_none(self) -> CacheMetadata | None:
        if not self.database.path.exists() or not self.database.integrity_ok():
            return None
        try:
            return self.database.cache_metadata()
        except (OSError, sqlite3.DatabaseError, ValueError):
            return None

    def _cache_digests_or_empty(self, cache: CacheMetadata | None) -> dict[str, str]:
        if cache is None:
            return {}
        try:
            return self.database.source_file_digests()
        except (OSError, sqlite3.DatabaseError):
            return {}

    def _can_increment(
        self,
        cache: CacheMetadata | None,
        graph_revision: int,
    ) -> bool:
        return cache is not None and (
            cache.cache_schema_version == FactsDatabase.schema_version
            and cache.parser_version == self.parser_version
            and cache.digest_profile_version == self.digest_profile.version
            and cache.managed_source_set_version == self.managed_source_set_version
            and cache.graph_revision == graph_revision
        )

    def _same_fact_input(
        self, cache: CacheMetadata | None, snapshot: _SourceSnapshot
    ) -> bool:
        return cache is not None and (
            cache.parser_version == self.parser_version
            and cache.digest_profile_version == self.digest_profile.version
            and cache.managed_source_set_version == self.managed_source_set_version
            and cache.repository_source_digest == snapshot.repository_source_digest
        )

    def _metadata(
        self,
        snapshot: _SourceSnapshot,
        graph_revision: int,
        previous: CacheMetadata | None,
        *,
        facts_changed: bool,
    ) -> CacheMetadata:
        previous_generation = 0 if previous is None else previous.index_generation
        return CacheMetadata(
            cache_schema_version=FactsDatabase.schema_version,
            parser_version=self.parser_version,
            digest_profile_version=self.digest_profile.version,
            managed_source_set_version=self.managed_source_set_version,
            repository_source_digest=snapshot.repository_source_digest,
            graph_revision=graph_revision,
            baseline_entity_snapshot_completeness=(
                "unknown"
                if previous is None
                else previous.baseline_entity_snapshot_completeness
            ),
            index_generation=previous_generation + int(facts_changed),
            built_at=_utc_now(),
        )

    def _replace_with_full_snapshot(
        self,
        parsed: Sequence[ParsedFile],
        metadata: CacheMetadata,
        diagnostics: Sequence[SourceDiagnostic],
        *,
        checkpoint_existing: bool,
    ) -> None:
        path = self.database.path
        path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        staging = FactsDatabase.create_new(staging_path)
        try:
            staging.synchronize(
                parsed,
                deleted_paths=(),
                metadata=metadata,
                global_diagnostics=_global_diagnostics(diagnostics),
            )
            if not staging.integrity_ok() or staging.cache_metadata() != metadata:
                raise FactSyncError("Full fact-cache replacement failed validation")
            self._checkpoint_cache(staging, "Staging")
            if checkpoint_existing:
                self._checkpoint_cache(self.database, "Existing")
            os.replace(staging_path, path)
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            _fsync_directory(path.parent)
        finally:
            for candidate in (staging_path, Path(f"{staging_path}-wal"), Path(f"{staging_path}-shm")):
                if candidate.exists():
                    candidate.unlink()

    @staticmethod
    def _checkpoint_cache(database: FactsDatabase, label: str) -> None:
        """Merge and close one healthy WAL coordinate before replacement."""
        with database.open_write() as connection:
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise FactSyncError(f"{label} fact-cache checkpoint remained busy")

    def _graph_revision(self) -> int:
        if self.formal_store is None:
            return 0
        return self.formal_store.load().manifest.graph_revision

    @staticmethod
    def _same_snapshot(left: _SourceSnapshot, right: _SourceSnapshot) -> bool:
        return (
            left.repository_source_digest == right.repository_source_digest
            and left.diagnostics == right.diagnostics
        )

    @staticmethod
    def _parsed_all(parsed: Sequence[ParsedFile], snapshot: _SourceSnapshot) -> bool:
        return {
            item.source.source.relative_path for item in parsed
        } == set(snapshot.digests_by_path)


def _global_diagnostics(
    diagnostics: Sequence[SourceDiagnostic],
) -> tuple[tuple[str, str, str], ...]:
    return tuple((item.code, "warning", item.message) for item in diagnostics)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
