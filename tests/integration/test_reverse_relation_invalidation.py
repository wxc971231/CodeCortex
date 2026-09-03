"""Incremental target changes re-resolve declarations without reparsing importers."""

from collections.abc import Iterable
from pathlib import Path

import pytest

from codecortex.domain.facts import SourceFileDigest, SourceFileInput
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.python.parser import ParsedFile, parse_python_file
from codecortex.infrastructure.python.resolver import resolve_relations


class IncrementalFacts:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
        self.parse_counts: dict[str, int] = {}

    def sync(self, changed: dict[str, str], deleted: Iterable[str] = ()) -> None:
        parsed = tuple(self._parse(relative, text) for relative, text in changed.items())
        scope = self.database.replace_parsed_files(parsed, deleted_paths=tuple(deleted))
        declarations = self.database.relation_declarations(
            (*scope.changed_relation_keys, *(item.relation_key for item in scope.incoming))
        )
        self.database.replace_resolved_relations(
            resolve_relations(declarations, self.database.symbol_index())
        )

    def relation(self, source_path: str):
        return self.database.relations_for_path(source_path)[0]

    def _parse(self, relative_path: str, text: str) -> ParsedFile:
        self.parse_counts[relative_path] = self.parse_counts.get(relative_path, 0) + 1
        source = SourceFileInput(relative_path, self.root / relative_path)
        return parse_python_file(
            SourceFileDigest(
                source=source,
                normalized_text=text,
                content_digest="sha256:" + ("a" * 63) + str(len(text) % 10),
                size_bytes=len(text.encode("utf-8")),
                encoding="utf-8",
            ),
            previous=(),
        )


@pytest.mark.parametrize(
    ("initial", "replacement_path", "replacement", "expected", "deleted"),
    [
        ("", "target.py", "class Service: pass\n", "target:Service", ()),
        ("class Service: pass\n", "target.py", "", None, ("target.py",)),
        ("class Service: pass\n", "target.py", "class Renamed: pass\n", None, ()),
        ("class Service: pass\n", "moved.py", "class Service: pass\n", None, ("target.py",)),
    ],
    ids=("add", "delete", "rename", "move"),
)
def test_target_changes_reresolve_unchanged_importer(
    tmp_path: Path,
    initial: str,
    replacement_path: str,
    replacement: str,
    expected: str | None,
    deleted: tuple[str, ...],
) -> None:
    facts = IncrementalFacts(tmp_path)
    facts.sync({"consumer.py": "from target import Service\n"})
    if initial:
        facts.sync({"target.py": initial})
    assert facts.relation("consumer.py").resolution_status == (
        "resolved" if initial else "unresolved"
    )

    facts.sync({replacement_path: replacement}, deleted=deleted)

    relation = facts.relation("consumer.py")
    assert relation.target_address == expected
    assert relation.resolution_status == ("resolved" if expected else "unresolved")
    assert facts.parse_counts["consumer.py"] == 1


def test_incremental_result_equals_clean_rebuild_after_target_add(tmp_path: Path) -> None:
    incremental = IncrementalFacts(tmp_path / "incremental")
    incremental.sync({"consumer.py": "from target import Service\n"})
    incremental.sync({"target.py": "class Service: pass\n"})

    clean = IncrementalFacts(tmp_path / "clean")
    clean.sync(
        {
            "consumer.py": "from target import Service\n",
            "target.py": "class Service: pass\n",
        }
    )

    old = incremental.relation("consumer.py")
    rebuilt = clean.relation("consumer.py")
    assert (old.relation_key, old.target_address, old.resolution_status) == (
        rebuilt.relation_key,
        rebuilt.target_address,
        rebuilt.resolution_status,
    )


def test_adding_same_named_target_reresolves_unresolved_call_without_reparse(
    tmp_path: Path,
) -> None:
    facts = IncrementalFacts(tmp_path)
    facts.sync({"consumer.py": "def consume():\n    Service()\n"})
    assert facts.relation("consumer.py").resolution_status == "unresolved"

    facts.sync({"target.py": "class Service: pass\n"})

    relation = facts.relation("consumer.py")
    assert relation.target_address == "target:Service"
    assert facts.parse_counts["consumer.py"] == 1
