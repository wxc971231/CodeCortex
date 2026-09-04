"""Deterministic computation of the one formal-baseline-to-current ChangeSet."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from codecortex.domain.cognition import FormalState
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.freshness import (
    ChangeSet,
    DiffCompleteness,
    EntityChanges,
    FileChanges,
)
from codecortex.domain.ids import IdPrefix, new_id
from codecortex.infrastructure.persistence.facts_db import (
    FactEntitySnapshot,
    FactsDatabase,
)


@dataclass(frozen=True)
class _Baseline:
    source_digest: str
    files: dict[str, str]


class ChangeDetector:
    """Compare formal source baseline with one already-synchronized fact cache."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))

    def detect(self, formal: FormalState, facts: FactsDatabase) -> ChangeSet | None:
        """Return exactly one baseline-to-current ChangeSet, or ``None`` if fresh."""
        baseline = self._validated_baseline(formal)
        metadata = facts.cache_metadata()
        if (
            metadata.digest_profile_version != formal.manifest.digest_profile_version
            or metadata.managed_source_set_version
            != formal.manifest.managed_source_set_version
        ):
            raise CodeCortexError(
                ErrorCode.CACHE_REBUILD_REQUIRED,
                "Fact cache digest profile does not match formal source baseline",
                suggested_action="Run Fact Preflight again to rebuild local facts",
            )
        current = facts.source_file_digests()
        current_digest = metadata.repository_source_digest
        if not _is_digest(current_digest):
            raise CodeCortexError(
                ErrorCode.CACHE_REBUILD_REQUIRED,
                "Fact cache has no valid current source digest",
                suggested_action="Run Fact Preflight again to rebuild local facts",
            )
        if current_digest == baseline.source_digest:
            return None

        files = _file_changes(baseline.files, current)
        entities, entity_completeness = _entity_changes(
            facts, baseline.source_digest
        )
        created_at = _rfc3339(self._now())
        return ChangeSet(
            change_set_id=_change_set_id(
                baseline.source_digest, current_digest, created_at
            ),
            baseline_source_digest=baseline.source_digest,
            current_source_digest=current_digest,
            created_at=created_at,
            changed_files=files,
            changed_entities=entities,
            file_diff_completeness="complete",
            entity_diff_completeness=entity_completeness,
        )

    def _validated_baseline(self, formal: FormalState) -> _Baseline:
        manifest = formal.manifest
        baseline = formal.source_baseline
        if not manifest.cognition_initialized:
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "M1b freshness requires initialized cognition",
            )
        if (
            manifest.cognition_baseline is None
            or baseline.repository_source_digest is None
            or manifest.cognition_baseline != baseline.repository_source_digest
            or not _is_digest(manifest.cognition_baseline)
        ):
            raise _formal_corrupt("Manifest and source baseline digests disagree")
        if (
            baseline.digest_profile_version != manifest.digest_profile_version
            or baseline.managed_source_set_version
            != manifest.managed_source_set_version
        ):
            raise _formal_corrupt("Source baseline profile versions disagree with manifest")
        files: dict[str, str] = {}
        for record in baseline.files:
            path = record.get("relative_path")
            digest = record.get("content_digest")
            if (
                not isinstance(path, str)
                or not path
                or path in files
                or not isinstance(digest, str)
                or not _is_digest(digest)
            ):
                raise _formal_corrupt("Source baseline files are malformed")
            files[path] = digest
        if not files or tuple(files) != tuple(sorted(files)):
            raise _formal_corrupt("Source baseline files must be non-empty and sorted")
        if _repository_digest(files, manifest.digest_profile_version) != manifest.cognition_baseline:
            raise _formal_corrupt("Source baseline file digests do not match manifest digest")
        return _Baseline(manifest.cognition_baseline, files)


def _file_changes(baseline: Mapping[str, str], current: Mapping[str, str]) -> FileChanges:
    added = tuple(sorted(set(current) - set(baseline)))
    deleted = tuple(sorted(set(baseline) - set(current)))
    modified = tuple(
        sorted(
            path
            for path in set(baseline) & set(current)
            if baseline[path] != current[path]
        )
    )
    renamed, added, deleted = _classify_unique_digest_renames(
        added, deleted, current, baseline
    )
    return FileChanges(added=added, modified=modified, deleted=deleted, renamed=renamed)


def _classify_unique_digest_renames(
    added: tuple[str, ...],
    deleted: tuple[str, ...],
    current: Mapping[str, str],
    baseline: Mapping[str, str],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[str, ...]]:
    additions_by_digest: dict[str, list[str]] = {}
    deletions_by_digest: dict[str, list[str]] = {}
    for path in added:
        additions_by_digest.setdefault(current[path], []).append(path)
    for path in deleted:
        deletions_by_digest.setdefault(baseline[path], []).append(path)
    pairs: list[tuple[str, str]] = []
    for digest, new_paths in additions_by_digest.items():
        old_paths = deletions_by_digest.get(digest, [])
        if len(new_paths) == 1 and len(old_paths) == 1:
            pairs.append((old_paths[0], new_paths[0]))
    renamed = tuple(sorted(pairs))
    renamed_old = {old for old, _ in renamed}
    renamed_new = {new for _, new in renamed}
    return (
        renamed,
        tuple(path for path in added if path not in renamed_new),
        tuple(path for path in deleted if path not in renamed_old),
    )


def _entity_changes(
    facts: FactsDatabase, baseline_digest: str
) -> tuple[EntityChanges, DiffCompleteness]:
    metadata = facts.cache_metadata()
    if metadata.baseline_entity_snapshot_completeness != "complete":
        return EntityChanges(), "partial"
    baseline = facts.baseline_entity_snapshots()
    if any(item.baseline_source_digest != baseline_digest for item in baseline):
        return EntityChanges(), "partial"
    current = facts.current_entity_snapshots()
    before = {item.uid: item for item in baseline}
    after = {item.uid: item for item in current}
    added = tuple(sorted(set(after) - set(before)))
    missing = tuple(sorted(set(before) - set(after)))
    moved = tuple(
        sorted(
            (uid, before[uid].relative_path, after[uid].relative_path)
            for uid in set(before) & set(after)
            if before[uid].relative_path != after[uid].relative_path
        )
    )
    modified = tuple(
        sorted(
            uid
            for uid in set(before) & set(after)
            if _entity_changed(before[uid], after[uid])
        )
    )
    return EntityChanges(added=added, modified=modified, missing=missing, moved=moved), "complete"


def _entity_changed(before: FactEntitySnapshot, after: FactEntitySnapshot) -> bool:
    return (
        before.address != after.address
        or before.kind != after.kind
        or before.signature != after.signature
        or before.fingerprint != after.fingerprint
    )


def _repository_digest(files: Mapping[str, str], profile_version: int) -> str:
    payload = bytearray(str(profile_version).encode("ascii"))
    payload.extend(b"\0")
    for path in sorted(files, key=lambda item: item.encode("utf-8")):
        payload.extend(path.encode("utf-8"))
        payload.extend(b"\0")
        payload.extend(files[path].encode("ascii"))
        payload.extend(b"\n")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _change_set_id(baseline: str, current: str, created_at: str) -> str:
    timestamp = int(datetime.fromisoformat(created_at).timestamp() * 1000)
    entropy = hashlib.sha256(f"{baseline}\0{current}\0{created_at}".encode()).digest()[:10]
    return new_id(IdPrefix.CHANGE_SET, now_ms=timestamp, random_bytes=entropy)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_digest(value: str) -> bool:
    return (
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _formal_corrupt(message: str) -> CodeCortexError:
    return CodeCortexError(ErrorCode.FORMAL_STATE_CORRUPT, message)
