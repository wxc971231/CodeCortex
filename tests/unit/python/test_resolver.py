"""Deterministic local relation resolution without importing project code."""

from pathlib import Path

from codecortex.domain.facts import SourceFileDigest, SourceFileInput
from codecortex.infrastructure.python.parser import parse_python_file
from codecortex.infrastructure.python.resolver import (
    SymbolIndex,
    relation_key,
    resolve_relations,
)


def parsed(tmp_path: Path, relative_path: str, text: str):
    source = SourceFileInput(relative_path, tmp_path / relative_path)
    return parse_python_file(
        SourceFileDigest(
            source=source,
            normalized_text=text,
            content_digest="sha256:" + "a" * 64,
            size_bytes=len(text.encode("utf-8")),
            encoding="utf-8",
        ),
        previous=(),
    )


def test_relation_key_is_declaration_stable_across_resolution(tmp_path: Path) -> None:
    consumer = parsed(tmp_path, "consumer.py", "from target import Service\n")
    declaration = consumer.relations[0]
    unresolved = resolve_relations((declaration,), SymbolIndex.empty())[0]
    target = parsed(tmp_path, "target.py", "class Service: pass\n")
    resolved = resolve_relations(
        (declaration,), SymbolIndex.from_parsed_files((consumer, target))
    )[0]

    assert relation_key(declaration) == unresolved.relation_key == resolved.relation_key
    assert unresolved.resolution_status == "unresolved"
    assert resolved.resolution_status == "resolved"
    assert resolved.target_address == "target:Service"


def test_resolver_resolves_local_import_inheritance_calls_and_test_relation(
    tmp_path: Path,
) -> None:
    target = parsed(
        tmp_path,
        "pkg/target.py",
        "class Base:\n    pass\n\ndef service():\n    pass\n",
    )
    consumer = parsed(
        tmp_path,
        "pkg/test_consumer.py",
        "from pkg.target import Base, service\n\nclass Child(Base):\n    pass\n\ndef test_service():\n    service()\n",
    )

    relations = resolve_relations(
        (*consumer.relations, *target.relations),
        SymbolIndex.from_parsed_files((consumer, target)),
    )

    by_type = {relation.relation_type: relation for relation in relations}
    imports = [item.target_address for item in relations if item.relation_type == "imports"]
    assert imports == ["pkg.target:Base", "pkg.target:service"]
    assert by_type["inherits"].target_address == "pkg.target:Base"
    assert by_type["calls"].target_address == "pkg.target:service"
    assert by_type["tested_by"].target_address == "pkg.target:service"
    assert by_type["calls"].confidence == "medium"
    assert by_type["tested_by"].evidence[0].evidence_kind == "static_call"
    assert all(relation.resolver_version == "python-local-v1" for relation in relations)


def test_unresolved_best_effort_relation_retains_evidence(tmp_path: Path) -> None:
    consumer = parsed(tmp_path, "consumer.py", "def run():\n    dynamic()\n")
    call = next(item for item in consumer.relations if item.relation_type == "call_declaration")

    resolved = resolve_relations((call,), SymbolIndex.empty())

    assert len(resolved) == 1
    assert resolved[0].relation_type == "calls"
    assert resolved[0].resolution_status == "unresolved"
    assert resolved[0].confidence == "low"
    assert resolved[0].evidence[0].snippet_digest.startswith("sha256:")


def test_resolver_handles_relative_import_from_package_initializer(tmp_path: Path) -> None:
    target = parsed(tmp_path, "pkg/target.py", "class Service: pass\n")
    package = parsed(tmp_path, "pkg/__init__.py", "from .target import Service\n")

    resolved = resolve_relations(
        package.relations, SymbolIndex.from_parsed_files((package, target))
    )

    assert resolved[0].target_address == "pkg.target:Service"
