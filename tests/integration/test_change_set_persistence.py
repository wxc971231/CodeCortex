"""Persistence boundaries for the one effective machine-local ChangeSet."""

from __future__ import annotations

import json

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.freshness import ChangeSet, EntityChanges, FileChanges
from codecortex.infrastructure.persistence.freshness import FreshnessStore


def _change_set(identifier: str, current: str) -> ChangeSet:
    return ChangeSet(
        change_set_id=identifier,
        baseline_source_digest="sha256:" + "a" * 64,
        current_source_digest=current,
        created_at="2026-09-04T00:00:00Z",
        changed_files=FileChanges(modified=("a.py",)),
        changed_entities=EntityChanges(),
        file_diff_completeness="complete",
        entity_diff_completeness="complete",
    )


def test_replacing_effective_change_set_removes_prior_file(tmp_path) -> None:
    store = FreshnessStore(tmp_path / ".codecortex" / ".cache")
    first = _change_set("chg_01J00000000000000000000001", "sha256:" + "b" * 64)
    second = _change_set("chg_01J00000000000000000000002", "sha256:" + "c" * 64)

    store.replace_effective(first)
    assert store.load_effective() == first

    store.replace_effective(second)

    assert not store.change_set_path(first.change_set_id).exists()
    assert store.load_effective() == second


def test_clearing_effective_change_set_removes_pointer_and_payload(tmp_path) -> None:
    store = FreshnessStore(tmp_path / ".codecortex" / ".cache")
    change_set = _change_set(
        "chg_01J00000000000000000000001", "sha256:" + "b" * 64
    )
    store.replace_effective(change_set)

    store.replace_effective(None)

    assert not store.freshness_path.exists()
    assert not store.change_set_path(change_set.change_set_id).exists()
    assert store.load_effective() is None


@pytest.mark.parametrize(
    "identifier",
    (
        "chg_",
        "chg_01J0000000000000000000000/",
        "chg_/../../outside",
        "chg_01J0000000000000000000000a",
    ),
)
def test_change_set_path_rejects_malformed_or_path_like_identifier(
    tmp_path, identifier: str
) -> None:
    store = FreshnessStore(tmp_path / ".codecortex" / ".cache")

    with pytest.raises(CodeCortexError) as raised:
        store.change_set_path(identifier)

    assert raised.value.code is ErrorCode.INVALID_ID


def test_corrupt_pointer_cannot_escape_change_set_directory(tmp_path) -> None:
    store = FreshnessStore(tmp_path / ".codecortex" / ".cache")
    outside = store.cache_root / "outside.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("must not be read or removed", encoding="utf-8")
    store.freshness_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effective_change_set_id": "chg_/../../outside",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CodeCortexError) as raised:
        store.replace_effective(None)

    assert raised.value.code is ErrorCode.INVALID_ID
    assert outside.read_text(encoding="utf-8") == "must not be read or removed"
