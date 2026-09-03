"""Best-effort, non-executing resolution of Python relation declarations.

Resolution is intentionally local and conservative.  It never imports the
target repository; all answers are derived from the current parsed entity and
declaration facts.  A declaration key identifies the source statement, while
the mutable resolution fields describe its current target (if any).
"""

import ast
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from codecortex.infrastructure.python.parser import ParsedFile, SyntacticRelation

RESOLVER_VERSION = "python-local-v1"


@dataclass(frozen=True)
class Symbol:
    """The minimum stable entity projection needed for local resolution."""

    uid: str
    address: str
    module_name: str
    qualname: str
    relative_path: str
    kind: str
    name: str


@dataclass(frozen=True)
class RelationEvidence:
    """A compact source-location proof for a best-effort relation."""

    relative_path: str
    start_line: int
    end_line: int
    evidence_kind: str
    snippet_digest: str


@dataclass(frozen=True)
class ResolvedRelation:
    """One resolution result keyed by a stable source declaration identity."""

    declaration: SyntacticRelation
    relation_type: str
    relation_key: str
    source_uid: str | None
    target_uid: str | None
    target_module: str | None
    target_address: str | None
    resolution_status: str
    confidence: str
    resolver_version: str
    evidence: tuple[RelationEvidence, ...]


class SymbolIndex:
    """In-memory index of existing declarations; it contains no executable code."""

    def __init__(self, symbols: Iterable[Symbol] = ()) -> None:
        materialized = tuple(symbols)
        self._by_address = {symbol.address: symbol for symbol in materialized}
        self._modules: dict[str, Symbol] = {}
        self._by_module_name: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self._by_name: dict[str, list[Symbol]] = defaultdict(list)
        self._source_by_address: dict[str, Symbol] = self._by_address
        for symbol in materialized:
            self._by_module_name[(symbol.module_name, symbol.name)].append(symbol)
            self._by_name[symbol.name].append(symbol)
            if symbol.qualname == "":
                self._modules[symbol.module_name] = symbol

    @classmethod
    def empty(cls) -> SymbolIndex:
        return cls()

    @classmethod
    def from_parsed_files(cls, parsed_files: Sequence[ParsedFile]) -> SymbolIndex:
        return cls(
            Symbol(
                uid=entity.uid,
                address=entity.address,
                module_name=entity.module_name,
                qualname=entity.qualname,
                relative_path=entity.relative_path,
                kind=entity.kind,
                name=entity.name,
            )
            for parsed in parsed_files
            for entity in parsed.entities
        )

    @classmethod
    def from_symbols(cls, symbols: Iterable[Symbol]) -> SymbolIndex:
        return cls(symbols)

    def source(self, address: str) -> Symbol | None:
        return self._source_by_address.get(address)

    def module(self, module_name: str) -> Symbol | None:
        return self._modules.get(module_name)

    def member(self, module_name: str, name: str) -> Symbol | None:
        candidates = self._by_module_name[(module_name, name)]
        return candidates[0] if len(candidates) == 1 else None

    def unique_name(self, name: str) -> Symbol | None:
        candidates = self._by_name[name]
        return candidates[0] if len(candidates) == 1 else None


def relation_key(declaration: SyntacticRelation) -> str:
    """Hash only immutable source-declaration data, never cache identity or target."""
    stable = (
        declaration.relative_path,
        declaration.source_address,
        declaration.relation_type,
        declaration.start_line,
        declaration.start_column,
        declaration.normalized_expression,
    )
    payload = json.dumps(stable, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def resolve_relations(
    declarations: Sequence[SyntacticRelation], symbol_index: SymbolIndex
) -> tuple[ResolvedRelation, ...]:
    """Resolve supplied declarations against one current symbol snapshot.

    Declarations are deliberately the input boundary: callers can re-resolve a
    narrow incoming set after a target changes, without reparsing its sources.
    """
    imports = _import_bindings(declarations, symbol_index)
    resolved: list[ResolvedRelation] = []
    for declaration in declarations:
        if declaration.relation_type == "import_declaration":
            resolved.append(_resolve_import(declaration, symbol_index))
        elif declaration.relation_type == "declared_base":
            resolved.append(_resolve_expression(declaration, symbol_index, imports, "inherits"))
        elif declaration.relation_type == "call_declaration":
            call = _resolve_expression(declaration, symbol_index, imports, "calls")
            resolved.append(call)
            if _is_test_source(declaration.relative_path, declaration.source_address):
                resolved.append(_as_tested_by(call))
    return tuple(resolved)


def _resolve_import(
    declaration: SyntacticRelation, symbol_index: SymbolIndex
) -> ResolvedRelation:
    target_module, target_name = _import_target(declaration)
    target = (
        symbol_index.member(target_module, target_name)
        if target_module is not None and target_name is not None
        else symbol_index.module(target_module) if target_module is not None else None
    )
    return _result(
        declaration,
        "imports",
        symbol_index.source(declaration.source_address),
        target,
        target_module,
        "high",
        "import_declaration",
    )


def _resolve_expression(
    declaration: SyntacticRelation,
    symbol_index: SymbolIndex,
    imports: dict[tuple[str, str], Symbol],
    relation_type: str,
) -> ResolvedRelation:
    expression = _expression_root(declaration.normalized_expression)
    source = symbol_index.source(declaration.source_address)
    target: Symbol | None = None
    target_module: str | None = None
    if expression is not None:
        target = imports.get((declaration.relative_path, expression))
        if target is None and source is not None:
            target = symbol_index.member(source.module_name, expression)
        if target is None:
            target = symbol_index.unique_name(expression)
        if target is not None:
            target_module = target.module_name
    return _result(
        declaration,
        relation_type,
        source,
        target,
        target_module,
        "medium" if target is not None else "low",
        "static_call" if relation_type == "calls" else "declared_base",
    )


def _result(
    declaration: SyntacticRelation,
    relation_type: str,
    source: Symbol | None,
    target: Symbol | None,
    target_module: str | None,
    resolved_confidence: str,
    evidence_kind: str,
) -> ResolvedRelation:
    evidence = RelationEvidence(
        relative_path=declaration.relative_path,
        start_line=declaration.start_line,
        end_line=declaration.start_line,
        evidence_kind=evidence_kind,
        snippet_digest=(
            "sha256:"
            + hashlib.sha256(declaration.normalized_expression.encode("utf-8")).hexdigest()
        ),
    )
    is_resolved = target is not None
    return ResolvedRelation(
        declaration=declaration,
        relation_type=relation_type,
        relation_key=relation_key(declaration),
        source_uid=None if source is None else source.uid,
        target_uid=None if target is None else target.uid,
        target_module=target.module_name if target is not None else target_module,
        target_address=None if target is None else target.address,
        resolution_status="resolved" if is_resolved else "unresolved",
        confidence=resolved_confidence,
        resolver_version=RESOLVER_VERSION,
        evidence=(evidence,),
    )


def _as_tested_by(call: ResolvedRelation) -> ResolvedRelation:
    return ResolvedRelation(
        declaration=call.declaration,
        relation_type="tested_by",
        relation_key=f"{call.relation_key}:tested_by",
        source_uid=call.source_uid,
        target_uid=call.target_uid,
        target_module=call.target_module,
        target_address=call.target_address,
        resolution_status=call.resolution_status,
        confidence=call.confidence,
        resolver_version=call.resolver_version,
        evidence=call.evidence,
    )


def _import_bindings(
    declarations: Sequence[SyntacticRelation], symbol_index: SymbolIndex
) -> dict[tuple[str, str], Symbol]:
    bindings: dict[tuple[str, str], Symbol] = {}
    for declaration in declarations:
        if declaration.relation_type != "import_declaration":
            continue
        target_module, target_name = _import_target(declaration)
        if target_module is None:
            continue
        target = (
            symbol_index.member(target_module, target_name)
            if target_name is not None
            else symbol_index.module(target_module)
        )
        if target is None:
            continue
        bound_name = _bound_name(declaration, target_name)
        if bound_name is not None:
            bindings[(declaration.relative_path, bound_name)] = target
    return bindings


def _import_target(declaration: SyntacticRelation) -> tuple[str | None, str | None]:
    try:
        node = ast.parse(declaration.normalized_expression).body[0]
    except SyntaxError:
        return None, None
    source_module = declaration.source_address.partition(":")[0]
    if isinstance(node, ast.Import) and len(node.names) == 1:
        return node.names[0].name, None
    if isinstance(node, ast.ImportFrom) and len(node.names) == 1:
        module = _absolute_from_module(
            source_module,
            declaration.relative_path.endswith("/__init__.py")
            or declaration.relative_path == "__init__.py",
            node.module,
            node.level,
        )
        return module, node.names[0].name
    return None, None


def _bound_name(declaration: SyntacticRelation, target_name: str | None) -> str | None:
    try:
        node = ast.parse(declaration.normalized_expression).body[0]
    except SyntaxError:
        return None
    if isinstance(node, ast.Import) and len(node.names) == 1:
        alias = node.names[0]
        return alias.asname or alias.name.partition(".")[0]
    if isinstance(node, ast.ImportFrom) and len(node.names) == 1 and target_name is not None:
        alias = node.names[0]
        return alias.asname or target_name
    return None


def _absolute_from_module(
    source_module: str, is_package: bool, module: str | None, level: int
) -> str | None:
    if level == 0:
        return module
    package = source_module.split(".")
    if not is_package:
        package.pop()
    if level > len(package) + 1:
        return None
    prefix = package[: len(package) - level + 1]
    return ".".join((*prefix, *(module.split(".") if module else ()))) or None


def _expression_root(expression: str) -> str | None:
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_test_source(relative_path: str, source_address: str) -> bool:
    file_name = relative_path.rsplit("/", 1)[-1]
    qualname = source_address.partition(":")[2]
    return file_name.startswith("test_") or qualname.startswith("test_")
