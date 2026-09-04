"""Atomic machine-local storage for the one effective ChangeSet."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
from codecortex.infrastructure.jsonio import write_json_atomic


class FreshnessStore:
    """Persist an effective ChangeSet without treating the cache as formal truth."""

    def __init__(self, cache_root: Path) -> None:
        self.cache_root = Path(cache_root)
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
        if change_set is None:
            self.freshness_path.unlink(missing_ok=True)
            if previous is not None:
                self.change_set_path(previous).unlink(missing_ok=True)
            return

        self.change_sets_path.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.change_set_path(change_set.change_set_id), _to_json(change_set))
        write_json_atomic(
            self.freshness_path,
            {"schema_version": 1, "effective_change_set_id": change_set.change_set_id},
        )
        if previous is not None and previous != change_set.change_set_id:
            self.change_set_path(previous).unlink(missing_ok=True)

    def load_effective(self) -> ChangeSet | None:
        change_set_id = self._effective_id_or_none()
        if change_set_id is None:
            return None
        path = self.change_set_path(change_set_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Effective ChangeSet cache payload is unreadable") from error
        if not isinstance(raw, Mapping):
            raise TypeError("Effective ChangeSet cache payload must be an object")
        return _from_json(raw)

    def _effective_id_or_none(self) -> str | None:
        if not self.freshness_path.exists():
            return None
        try:
            raw = json.loads(self.freshness_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
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
