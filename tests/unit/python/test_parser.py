"""AST-only Python entity extraction and identity behavior."""

from pathlib import Path

import pytest

from codecortex.domain.facts import SourceFileInput
from codecortex.infrastructure.python.digest import digest_source_file
from codecortex.infrastructure.python.parser import (
    EntityIdentityHint,
    parse_python_file,
)


def source(tmp_path: Path, relative: str, text: str) -> SourceFileInput:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return SourceFileInput(relative, path)


def parse(tmp_path: Path, relative: str, text: str, previous=()):
    return parse_python_file(
        digest_source_file(source(tmp_path, relative, text)), previous=previous
    )


def test_parser_extracts_nested_async_decorated_entities_and_syntactic_relations(
    tmp_path: Path,
) -> None:
    result = parse(
        tmp_path,
        "pkg/mod.py",
        """import os\nfrom base import Base\n\n@service\nclass Service(Base):\n    \"\"\"Service docs.\"\"\"\n\n    @route(\"/run\")\n    async def run(self, item: str, /, *, retry: int = 3) -> None:\n        async def inner() -> int:\n            return 1\n        return await inner()\n""",
    )

    entity = result.entity_by_address("pkg.mod:Service.run.inner")
    assert entity.kind == "async_function"
    assert entity.parent_address == "pkg.mod:Service.run"
    assert (entity.start_line, entity.end_line) == (10, 11)
    assert result.entity_by_address("pkg.mod:Service").decorators == ("service",)
    assert result.entity_by_address("pkg.mod:Service.run").signature == (
        "(self, item: str, /, *, retry: int=3) -> None"
    )
    assert {relation.relation_type for relation in result.relations} == {
        "call_declaration",
        "contains",
        "declared_base",
        "import_declaration",
    }
    assert {relation.normalized_expression for relation in result.relations} >= {
        "import os",
        "from base import Base",
        "Base",
    }


def test_parser_maps_package_init_to_its_package_module(tmp_path: Path) -> None:
    result = parse(tmp_path, "pkg/__init__.py", "class API: pass\n")

    assert result.module_name == "pkg"
    assert result.entity_by_address("pkg:API").module_name == "pkg"


def test_syntax_error_becomes_a_file_diagnostic(tmp_path: Path) -> None:
    result = parse(tmp_path, "broken.py", "def broken(:\n")

    assert result.parse_status == "parse_error"
    assert result.entities == ()
    assert result.diagnostics[0].code == "PYTHON_SYNTAX_ERROR"
    assert result.diagnostics[0].line == 1


@pytest.mark.parametrize(
    "fixture_name",
    ("py39.py", "py310.py", "py311.py", "py312.py", "py313.py", "py314.py"),
)
def test_parser_accepts_supported_python_syntax_fixtures(fixture_name: str) -> None:
    fixture = Path("tests/fixtures/python_syntax") / fixture_name
    result = parse_python_file(
        digest_source_file(SourceFileInput(fixture.as_posix(), fixture)), previous=()
    )

    assert result.parse_status == "parsed"
    assert result.diagnostics == ()


def test_parser_preserves_uid_for_exact_address_and_unique_rename(tmp_path: Path) -> None:
    original = parse(tmp_path / "one", "pkg/service.py", "class OldName:\n    pass\n")
    old = original.entity_by_address("pkg.service:OldName")
    previous = (
        EntityIdentityHint(
            uid=old.uid,
            address=old.address,
            kind=old.kind,
            relative_path=old.relative_path,
            signature=old.signature,
            fingerprint=old.fingerprint,
        ),
    )

    exact = parse(
        tmp_path / "two", "pkg/service.py", "class OldName:\n    pass\n", previous
    )
    renamed = parse(
        tmp_path / "three", "pkg/service.py", "class NewName:\n    pass\n", previous
    )

    assert exact.entity_by_address("pkg.service:OldName").uid == old.uid
    assert renamed.entity_by_address("pkg.service:NewName").uid == old.uid


def test_parser_does_not_transfer_ambiguous_rename_identity(tmp_path: Path) -> None:
    original = parse(tmp_path / "one", "pkg/service.py", "class OldName:\n    pass\n")
    old = original.entity_by_address("pkg.service:OldName")
    previous = (
        EntityIdentityHint(
            uid=old.uid,
            address=old.address,
            kind=old.kind,
            relative_path=old.relative_path,
            signature=old.signature,
            fingerprint=old.fingerprint,
        ),
    )

    result = parse(
        tmp_path / "two",
        "pkg/service.py",
        "class First:\n    pass\n\nclass Second:\n    pass\n",
        previous,
    )

    assert old.uid not in {entity.uid for entity in result.entities}
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "ENTITY_IDENTITY_AMBIGUOUS"
    }


def test_parser_distinguishes_overload_like_duplicate_definitions(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        "pkg/overloads.py",
        """from typing import overload\n\n@overload\ndef parse(value: int) -> int: ...\n\n@overload\ndef parse(value: str) -> str: ...\n""",
    )

    functions = [entity for entity in result.entities if entity.name == "parse"]
    assert len(functions) == 2
    assert len({entity.address for entity in functions}) == 2
