"""Read-only AST extraction for managed Python source files."""

import ast
import copy
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Literal

from codecortex.domain.facts import SourceFileDigest
from codecortex.domain.ids import IdPrefix, new_id

EntityKind = Literal[
    "module", "class", "function", "async_function", "method", "async_method"
]
ParseStatus = Literal["parsed", "parse_error"]


@dataclass(frozen=True)
class ParseDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    message: str
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class EntityIdentityHint:
    uid: str
    address: str
    kind: EntityKind
    relative_path: str
    signature: str | None
    fingerprint: str


@dataclass(frozen=True)
class CodeEntityCandidate:
    uid: str
    address: str
    module_name: str
    qualname: str
    relative_path: str
    kind: EntityKind
    name: str
    parent_address: str | None
    start_line: int
    end_line: int
    signature: str | None
    decorators: tuple[str, ...]
    docstring_digest: str | None
    fingerprint: str


@dataclass(frozen=True)
class SyntacticRelation:
    relation_type: Literal["contains", "import_declaration", "declared_base"]
    source_address: str
    target_address: str | None
    relative_path: str
    start_line: int
    start_column: int
    normalized_expression: str
    confidence: Literal["syntactic"] = "syntactic"


@dataclass(frozen=True)
class ParsedFile:
    source: SourceFileDigest
    module_name: str
    parse_status: ParseStatus
    entities: tuple[CodeEntityCandidate, ...]
    relations: tuple[SyntacticRelation, ...]
    diagnostics: tuple[ParseDiagnostic, ...]

    def entity_by_address(self, address: str) -> CodeEntityCandidate:
        for entity in self.entities:
            if entity.address == address:
                return entity
        raise KeyError(f"No entity at address: {address}")


def parse_python_file(
    source: SourceFileDigest, previous: Sequence[EntityIdentityHint]
) -> ParsedFile:
    """Extract parse facts without importing or executing the target module."""
    module_name = _module_name(source.source.relative_path)
    try:
        tree = ast.parse(source.normalized_text, filename=source.source.relative_path)
    except SyntaxError as error:
        return ParsedFile(
            source=source,
            module_name=module_name,
            parse_status="parse_error",
            entities=(),
            relations=(),
            diagnostics=(
                ParseDiagnostic(
                    code="PYTHON_SYNTAX_ERROR",
                    severity="error",
                    message=error.msg,
                    line=error.lineno,
                    column=error.offset,
                ),
            ),
        )
    collector = _EntityCollector(source, module_name)
    collector.visit(tree)
    entities, diagnostics = _assign_identities(collector.entities, previous, collector.diagnostics)
    return ParsedFile(
        source=source,
        module_name=module_name,
        parse_status="parsed",
        entities=entities,
        relations=tuple(collector.relations),
        diagnostics=diagnostics,
    )


class _EntityCollector(ast.NodeVisitor):
    def __init__(self, source: SourceFileDigest, module_name: str) -> None:
        self._source = source
        self._module_name = module_name
        self._contexts: list[CodeEntityCandidate] = []
        self._address_counts: dict[str, int] = {}
        self.entities: list[CodeEntityCandidate] = []
        self.relations: list[SyntacticRelation] = []
        self.diagnostics: list[ParseDiagnostic] = []

    def visit_Module(self, node: ast.Module) -> None:
        module = self._make_entity(
            node=node,
            kind="module",
            name=self._module_name,
            qualname="",
            signature=None,
            decorators=(),
            parent=None,
            start_line=1,
            end_line=max(1, self._source.normalized_text.count("\n") + 1),
        )
        self._push(module, node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = self._contexts[-1]
        entity = self._make_entity(
            node=node,
            kind="class",
            name=node.name,
            qualname=_join_qualname(parent.qualname, node.name),
            signature=None,
            decorators=_decorators(node.decorator_list),
            parent=parent,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )
        for base in node.bases:
            self.relations.append(
                SyntacticRelation(
                    relation_type="declared_base",
                    source_address=entity.address,
                    target_address=None,
                    relative_path=self._source.source.relative_path,
                    start_line=base.lineno,
                    start_column=base.col_offset,
                    normalized_expression=ast.unparse(base),
                )
            )
        self._push(entity, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def visit_Import(self, node: ast.Import) -> None:
        self._add_import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._add_import(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool) -> None:
        parent = self._contexts[-1]
        direct_class_child = parent.kind == "class"
        kind: EntityKind
        if direct_class_child:
            kind = "async_method" if is_async else "method"
        else:
            kind = "async_function" if is_async else "function"
        entity = self._make_entity(
            node=node,
            kind=kind,
            name=node.name,
            qualname=_join_qualname(parent.qualname, node.name),
            signature=_signature(node),
            decorators=_decorators(node.decorator_list),
            parent=parent,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )
        self._push(entity, node.body)

    def _add_import(self, node: ast.Import | ast.ImportFrom) -> None:
        current = self._contexts[-1]
        self.relations.append(
            SyntacticRelation(
                relation_type="import_declaration",
                source_address=current.address,
                target_address=None,
                relative_path=self._source.source.relative_path,
                start_line=node.lineno,
                start_column=node.col_offset,
                normalized_expression=ast.unparse(node),
            )
        )

    def _push(self, entity: CodeEntityCandidate, body: list[ast.stmt]) -> None:
        self.entities.append(entity)
        if entity.parent_address is not None:
            self.relations.append(
                SyntacticRelation(
                    relation_type="contains",
                    source_address=entity.parent_address,
                    target_address=entity.address,
                    relative_path=self._source.source.relative_path,
                    start_line=entity.start_line,
                    start_column=0,
                    normalized_expression=entity.address,
                )
            )
        self._contexts.append(entity)
        for statement in body:
            self.visit(statement)
        self._contexts.pop()

    def _make_entity(
        self,
        *,
        node: ast.AST,
        kind: EntityKind,
        name: str,
        qualname: str,
        signature: str | None,
        decorators: tuple[str, ...],
        parent: CodeEntityCandidate | None,
        start_line: int,
        end_line: int,
    ) -> CodeEntityCandidate:
        base_address = f"{self._module_name}:{qualname}"
        occurrence = self._address_counts.get(base_address, 0) + 1
        self._address_counts[base_address] = occurrence
        address = base_address if occurrence == 1 else f"{base_address}#{occurrence}"
        return CodeEntityCandidate(
            uid="",
            address=address,
            module_name=self._module_name,
            qualname=qualname,
            relative_path=self._source.source.relative_path,
            kind=kind,
            name=name,
            parent_address=None if parent is None else parent.address,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            decorators=decorators,
            docstring_digest=_docstring_digest(node),
            fingerprint=_fingerprint(node, kind, signature, decorators),
        )


def _assign_identities(
    candidates: Sequence[CodeEntityCandidate],
    previous: Sequence[EntityIdentityHint],
    diagnostics: Sequence[ParseDiagnostic],
) -> tuple[tuple[CodeEntityCandidate, ...], tuple[ParseDiagnostic, ...]]:
    remaining = list(previous)
    assigned: list[CodeEntityCandidate] = []
    unresolved: list[CodeEntityCandidate] = []
    for candidate in candidates:
        matches = [hint for hint in remaining if hint.address == candidate.address and hint.kind == candidate.kind]
        if len(matches) == 1:
            assigned.append(replace(candidate, uid=matches[0].uid))
            remaining.remove(matches[0])
        else:
            unresolved.append(candidate)
    output_diagnostics = list(diagnostics)
    for candidate in unresolved:
        matches = [
            hint
            for hint in remaining
            if hint.kind == candidate.kind
            and hint.signature == candidate.signature
            and hint.fingerprint == candidate.fingerprint
        ]
        candidates_for_match = [
            other
            for other in unresolved
            if other.kind == candidate.kind
            and other.signature == candidate.signature
            and other.fingerprint == candidate.fingerprint
        ]
        if len(matches) == 1 and len(candidates_for_match) == 1:
            assigned.append(replace(candidate, uid=matches[0].uid))
            remaining.remove(matches[0])
        else:
            assigned.append(replace(candidate, uid=new_id(IdPrefix.ENTITY)))
            if matches and len(candidates_for_match) > 1:
                output_diagnostics.append(
                    ParseDiagnostic(
                        code="ENTITY_IDENTITY_AMBIGUOUS",
                        severity="warning",
                        message=f"Ambiguous identity match for {candidate.address}",
                        line=candidate.start_line,
                    )
                )
    order = {candidate.address: index for index, candidate in enumerate(candidates)}
    return (
        tuple(sorted(assigned, key=lambda entity: order[entity.address])),
        tuple(output_diagnostics),
    )


def _module_name(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "__init__"


def _join_qualname(parent: str, name: str) -> str:
    return name if not parent else f"{parent}.{name}"


def _decorators(decorator_list: list[ast.expr]) -> tuple[str, ...]:
    return tuple(ast.unparse(decorator) for decorator in decorator_list)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    signature = f"({ast.unparse(node.args)})"
    return signature if node.returns is None else f"{signature} -> {ast.unparse(node.returns)}"


def _docstring_digest(node: ast.AST) -> str | None:
    if not isinstance(
        node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    docstring = ast.get_docstring(node, clean=False)
    if docstring is None:
        return None
    return f"sha256:{hashlib.sha256(docstring.encode('utf-8')).hexdigest()}"


def _fingerprint(
    node: ast.AST,
    kind: EntityKind,
    signature: str | None,
    decorators: tuple[str, ...],
) -> str:
    normalized = copy.deepcopy(node)
    if isinstance(normalized, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        normalized.name = ""
    for child in ast.walk(normalized):
        for attribute in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
            if hasattr(child, attribute):
                delattr(child, attribute)
    payload = "\0".join((kind, signature or "", "\x1f".join(decorators), ast.dump(normalized)))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
