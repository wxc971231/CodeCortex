"""Immutable domain records for baseline-to-current source freshness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DiffCompleteness = Literal["complete", "partial"]
ScopeConfidence = Literal["complete", "partial", "unknown"]


def _sorted_unique(values: tuple[str, ...], name: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{name} must contain non-empty strings")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{name} must be sorted and unique")


@dataclass(frozen=True)
class FileChanges:
    """Complete path-level difference between formal baseline and current facts."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name, values in (
            ("added file paths", self.added),
            ("modified file paths", self.modified),
            ("deleted file paths", self.deleted),
        ):
            _sorted_unique(values, name)
        if set(self.added) & set(self.modified) or set(self.added) & set(self.deleted):
            raise ValueError("File change paths cannot appear in multiple categories")
        if set(self.modified) & set(self.deleted):
            raise ValueError("File change paths cannot appear in multiple categories")
        if self.renamed != tuple(sorted(set(self.renamed))):
            raise ValueError("Renamed file pairs must be sorted and unique")
        for old_path, new_path in self.renamed:
            if not all(isinstance(path, str) and path for path in (old_path, new_path)):
                raise ValueError("Renamed file pairs require non-empty paths")
            if old_path == new_path:
                raise ValueError("A file cannot be renamed to itself")


@dataclass(frozen=True)
class EntityChanges:
    """Best-effort entity difference, explicitly marked complete or partial elsewhere."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    moved: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        for name, values in (
            ("added entity IDs", self.added),
            ("modified entity IDs", self.modified),
            ("missing entity IDs", self.missing),
        ):
            _sorted_unique(values, name)
        if self.moved != tuple(sorted(set(self.moved))):
            raise ValueError("Moved entity records must be sorted and unique")
        for uid, old_path, new_path in self.moved:
            if not all(isinstance(value, str) and value for value in (uid, old_path, new_path)):
                raise ValueError("Moved entity records require non-empty values")
            if old_path == new_path:
                raise ValueError("Moved entity paths must differ")


@dataclass(frozen=True)
class ChangeSet:
    """The one effective difference from accepted cognition baseline to now."""

    change_set_id: str
    baseline_source_digest: str
    current_source_digest: str
    created_at: str
    changed_files: FileChanges
    changed_entities: EntityChanges
    file_diff_completeness: DiffCompleteness
    entity_diff_completeness: DiffCompleteness
    affected_nodes: tuple[str, ...] = ()
    affected_flows: tuple[str, ...] = ()
    affected_entities: tuple[str, ...] = ()
    scope_confidence: ScopeConfidence = "unknown"
    unmapped_changes: tuple[dict[str, object], ...] = ()
    diagnostics: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ChangeSet schema version is unsupported")
        if not self.change_set_id.startswith("chg_"):
            raise ValueError("ChangeSet ID must use the chg_ namespace")
        for name, digest in (
            ("baseline source digest", self.baseline_source_digest),
            ("current source digest", self.current_source_digest),
        ):
            if not isinstance(digest, str) or not _is_digest(digest):
                raise ValueError(f"{name} must be a SHA-256 digest")
        if self.baseline_source_digest == self.current_source_digest:
            raise ValueError("A ChangeSet requires distinct baseline and current digests")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("ChangeSet creation time must be non-empty text")
        if self.file_diff_completeness not in ("complete", "partial"):
            raise ValueError("File diff completeness is invalid")
        if self.entity_diff_completeness not in ("complete", "partial"):
            raise ValueError("Entity diff completeness is invalid")
        if self.scope_confidence not in ("complete", "partial", "unknown"):
            raise ValueError("Scope confidence is invalid")
        for name, values in (
            ("affected node IDs", self.affected_nodes),
            ("affected flow IDs", self.affected_flows),
            ("affected entity IDs", self.affected_entities),
            ("diagnostics", self.diagnostics),
        ):
            _sorted_unique(values, name)
        if not all(isinstance(value, dict) for value in self.unmapped_changes):
            raise ValueError("Unmapped changes must be object records")


def _is_digest(value: str) -> bool:
    return (
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )
