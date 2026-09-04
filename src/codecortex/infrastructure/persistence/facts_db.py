"""SQLite-backed, bounded access to rebuildable Python code facts.

This module intentionally exposes a small repository API instead of allowing
callers to compose arbitrary SQL.  The cache is an implementation detail, so
all reads are paginated and all analyzer-facing connections are read-only.
"""

from __future__ import annotations

import ast
import base64
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from codecortex.infrastructure.python.parser import (
    EntityIdentityHint,
    ParsedFile,
    SyntacticRelation,
)

if TYPE_CHECKING:
    from codecortex.infrastructure.python.resolver import ResolvedRelation, SymbolIndex

_CACHE_SCHEMA_VERSION = 2
_BUSY_TIMEOUT_MS = 10_000
_ALLOWED_RELATION_TYPES = frozenset(
    {
        "contains",
        "import_declaration",
        "declared_base",
        "imports",
        "inherits",
        "tested_by",
        "calls",
    }
)

ALLOWED_RELATION_TYPES = frozenset(_ALLOWED_RELATION_TYPES)
"""Public read-only alias used by the application query layer (Task 8)."""


@dataclass(frozen=True)
class FactScope:
    """A deliberately narrow allowlisted filter for fact queries."""

    kind: str
    value: str

    @classmethod
    def module(cls, module_name: str) -> FactScope:
        if not isinstance(module_name, str) or not module_name:
            raise ValueError("Module scope must have a non-empty module name")
        return cls(kind="module", value=module_name)


@dataclass(frozen=True)
class Page[Item]:
    """One bounded, cursor-addressable page of cache results."""

    items: tuple[Item, ...]
    next_cursor: str | None
    truncated: bool


@dataclass(frozen=True)
class CodeEntity:
    """The query projection for a current code entity."""

    uid: str
    relative_path: str
    address: str
    module_name: str
    qualname: str
    kind: str
    name: str
    parent_uid: str | None
    start_line: int
    end_line: int
    signature: str | None
    docstring_digest: str | None
    fingerprint: str
    resolution_status: str


@dataclass(frozen=True)
class EntityReferenceResolution:
    """Strict bounded lookup result for one persisted entity reference.

    A fallback must match the reference's address *or* fingerprint while also
    matching kind and signature.  Returning an explicit ambiguous result keeps
    callers from silently choosing one of several same-fingerprint entities.
    """

    status: str
    entity: CodeEntity | None


@dataclass(frozen=True)
class CodeRelation:
    """The query projection for one current source declaration relation."""

    relation_id: int
    relation_type: str
    source_uid: str | None
    source_file_id: int
    target_uid: str | None
    target_module: str | None
    target_address: str | None
    raw_expression: str | None
    resolution_status: str
    confidence: str
    resolver_version: str
    relation_key: str


@dataclass(frozen=True)
class RelationDeclarationRef:
    """A stored declaration that can be re-resolved without parsing its source."""

    relation_key: str
    declaration: SyntacticRelation


@dataclass(frozen=True)
class RelationUpdateScope:
    """Changed and incoming declarations needing one narrowly scoped resolution pass."""

    changed_relation_keys: tuple[str, ...]
    incoming: tuple[RelationDeclarationRef, ...]


@dataclass(frozen=True)
class CacheMetadata:
    """The one cache-generation record used to reject mixed fact snapshots."""

    cache_schema_version: int
    parser_version: str
    digest_profile_version: int
    managed_source_set_version: int
    repository_source_digest: str
    graph_revision: int
    baseline_entity_snapshot_completeness: str
    index_generation: int
    built_at: str


@dataclass(frozen=True)
class FactEntitySnapshot:
    """A compact entity projection used for baseline-to-current comparison."""

    uid: str
    baseline_source_digest: str | None
    relative_path: str
    address: str
    kind: str
    signature: str | None
    fingerprint: str


@dataclass(frozen=True)
class SourceFileStatus:
    """Current parse state needed for conservative freshness decisions."""

    relative_path: str
    parse_status: str
    diagnostic_count: int


@dataclass(frozen=True)
class ModulePartition:
    """One package/module partition of the current managed-source facts."""

    module_name: str
    file_count: int
    entity_count: int
    diagnostic_count: int


@dataclass(frozen=True)
class FactTotals:
    """Whole-cache aggregate counts for the analysis-scope overview."""

    file_count: int
    entity_count: int
    diagnostic_count: int


@dataclass(frozen=True)
class FactDiagnostic:
    """One recorded source diagnostic with its optional file location."""

    diagnostic_id: int
    relative_path: str | None
    code: str
    severity: str
    message: str
    start_line: int | None
    end_line: int | None


class FactsDatabase:
    """A local SQLite fact cache with strict connection and query policies."""

    max_page_size = 100
    max_relation_entity_uids = 200
    schema_version = _CACHE_SCHEMA_VERSION

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def create_new(cls, path: Path) -> FactsDatabase:
        """Create the schema (or verify an existing compatible schema) at *path*."""
        database = cls(path)
        database.path.parent.mkdir(parents=True, exist_ok=True)
        with database.open_write() as connection:
            connection.executescript(_DDL)
            _ensure_task4_relation_columns(connection)
            connection.execute(
                "INSERT OR IGNORE INTO cache_metadata "
                "(singleton_id, cache_schema_version, parser_version, "
                "digest_profile_version, managed_source_set_version, "
                "repository_source_digest, graph_revision, "
                "baseline_entity_snapshot_completeness, index_generation, built_at) "
                "VALUES (1, ?, '', 1, 1, 'sha256:', 0, 'unknown', 0, '')",
                (_CACHE_SCHEMA_VERSION,),
            )
            connection.execute(
                "UPDATE cache_metadata SET cache_schema_version = ? WHERE singleton_id = 1",
                (_CACHE_SCHEMA_VERSION,),
            )
        return database

    def open_read(self) -> sqlite3.Connection:
        """Open a URI-mode read-only, query-only connection for bounded reads."""
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only = ON")
        return connection

    def open_write(self) -> sqlite3.Connection:
        """Open the only connection mode permitted to mutate the local cache."""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return connection

    def foreign_keys_enabled(self) -> bool:
        with self.open_read() as connection:
            return connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def has_index(self, table: str, columns: tuple[str, ...]) -> bool:
        """Return whether *table* has an index whose ordered columns match."""
        if table not in _SCHEMA_TABLES:
            return False
        with self.open_read() as connection:
            indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
            for index in indexes:
                index_name = index[1]
                actual = tuple(
                    row[2]
                    for row in connection.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                )
                if actual == columns:
                    return True
        return False

    def cache_metadata(self) -> CacheMetadata:
        """Load the sole cache metadata row without exposing SQL to callers."""
        with self.open_read() as connection:
            row = connection.execute(
                "SELECT cache_schema_version, parser_version, digest_profile_version, "
                "managed_source_set_version, repository_source_digest, graph_revision, "
                "baseline_entity_snapshot_completeness, index_generation, built_at "
                "FROM cache_metadata WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise ValueError("Fact cache metadata is missing")
        return CacheMetadata(
            cache_schema_version=row["cache_schema_version"],
            parser_version=row["parser_version"],
            digest_profile_version=row["digest_profile_version"],
            managed_source_set_version=row["managed_source_set_version"],
            repository_source_digest=row["repository_source_digest"],
            graph_revision=row["graph_revision"],
            baseline_entity_snapshot_completeness=row[
                "baseline_entity_snapshot_completeness"
            ],
            index_generation=row["index_generation"],
            built_at=row["built_at"],
        )

    def source_file_digests(self) -> dict[str, str]:
        """Return the current path-to-content-digest map in one local query."""
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT relative_path, content_digest FROM source_files "
                "ORDER BY relative_path"
            ).fetchall()
        return {row["relative_path"]: row["content_digest"] for row in rows}

    def current_entity_snapshots(self) -> tuple[FactEntitySnapshot, ...]:
        """Return all current entities in a stable, narrow comparison projection."""
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT e.uid, sf.relative_path, e.address, e.kind, e.signature, "
                "e.fingerprint FROM entities AS e JOIN source_files AS sf "
                "ON sf.file_id = e.file_id ORDER BY e.uid"
            ).fetchall()
        return tuple(
            FactEntitySnapshot(
                uid=row["uid"],
                baseline_source_digest=None,
                relative_path=row["relative_path"],
                address=row["address"],
                kind=row["kind"],
                signature=row["signature"],
                fingerprint=row["fingerprint"],
            )
            for row in rows
        )

    def baseline_entity_snapshots(self) -> tuple[FactEntitySnapshot, ...]:
        """Return the formal-baseline cache snapshot, never an implicit fallback."""
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT uid, baseline_source_digest, relative_path, address, kind, "
                "signature, fingerprint FROM baseline_entity_snapshots ORDER BY uid"
            ).fetchall()
        return tuple(
            FactEntitySnapshot(
                uid=row["uid"],
                baseline_source_digest=row["baseline_source_digest"],
                relative_path=row["relative_path"],
                address=row["address"],
                kind=row["kind"],
                signature=row["signature"],
                fingerprint=row["fingerprint"],
            )
            for row in rows
        )

    def source_file_statuses(self) -> tuple[SourceFileStatus, ...]:
        """Return every current source file's parse state in stable path order."""
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT relative_path, parse_status, diagnostic_count FROM source_files "
                "ORDER BY relative_path"
            ).fetchall()
        return tuple(
            SourceFileStatus(
                relative_path=row["relative_path"],
                parse_status=row["parse_status"],
                diagnostic_count=row["diagnostic_count"],
            )
            for row in rows
        )

    def diagnostic_codes_for_paths(
        self, relative_paths: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        """Return current parse/analysis diagnostic codes for explicit source paths."""
        paths = _validated_non_empty_strings(relative_paths, "Source path list")
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT sf.relative_path, d.code FROM diagnostics AS d "
                "JOIN source_files AS sf ON sf.file_id = d.file_id "
                f"WHERE sf.relative_path IN ({_placeholders(paths)}) "
                "ORDER BY sf.relative_path, d.code",
                paths,
            ).fetchall()
        codes: dict[str, list[str]] = {}
        for row in rows:
            codes.setdefault(row["relative_path"], []).append(row["code"])
        return {path: tuple(values) for path, values in codes.items()}

    def relations_touching_entity_uids(
        self, entity_uids: Sequence[str]
    ) -> tuple[CodeRelation, ...]:
        """Return one-hop local dependency facts touching the supplied entities."""
        uids = _validated_non_empty_strings(entity_uids, "Entity UID list")
        if len(uids) > self.max_relation_entity_uids:
            raise ValueError("Entity UID list exceeds the configured maximum")
        placeholders = _placeholders(uids)
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT relation_id, relation_type, source_uid, source_file_id, "
                "target_uid, target_module, target_address, raw_expression, "
                "resolution_status, confidence, resolver_version, relation_key "
                "FROM relations WHERE relation_type IN ('imports', 'inherits', 'calls') "
                f"AND (source_uid IN ({placeholders}) OR target_uid IN ({placeholders})) "
                "ORDER BY relation_id",
                (*uids, *uids),
            ).fetchall()
        return tuple(_code_relation_from_row(row) for row in rows)

    def replace_baseline_entity_snapshots(self, baseline_source_digest: str) -> None:
        """Copy every current entity into the baseline snapshot table.

        The whole replacement is one write transaction and the completeness
        flag in cache metadata flips to ``complete`` only after the copy, so
        readers never observe a partial baseline snapshot set.
        """
        if (
            not isinstance(baseline_source_digest, str)
            or not baseline_source_digest.startswith("sha256:")
            or len(baseline_source_digest) != 71
        ):
            raise ValueError("Baseline source digest must be SHA-256")
        with self.open_write() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM baseline_entity_snapshots")
            connection.execute(
                "INSERT INTO baseline_entity_snapshots "
                "(uid, baseline_source_digest, relative_path, address, "
                "module_name, qualname, kind, fingerprint, signature) "
                "SELECT e.uid, ?, sf.relative_path, e.address, e.module_name, "
                "e.qualname, e.kind, e.fingerprint, e.signature "
                "FROM entities AS e JOIN source_files AS sf "
                "ON sf.file_id = e.file_id ORDER BY e.uid",
                (baseline_source_digest,),
            )
            cursor = connection.execute(
                "UPDATE cache_metadata "
                "SET baseline_entity_snapshot_completeness = 'complete' "
                "WHERE singleton_id = 1"
            )
            if cursor.rowcount != 1:
                raise ValueError("Fact cache metadata is missing")

    def advance_graph_revision(self, graph_revision: int) -> None:
        """Point the unchanged fact cache at a newly committed graph revision.

        A cognitive apply changes no source fact, so only the visibility
        pointer moves; guarded readers keep seeing one consistent snapshot.
        """
        if (
            type(graph_revision) is not int
            or isinstance(graph_revision, bool)
            or graph_revision < 0
        ):
            raise ValueError("Graph revision must be a non-negative integer")
        with self.open_write() as connection:
            cursor = connection.execute(
                "UPDATE cache_metadata SET graph_revision = ? "
                "WHERE singleton_id = 1",
                (graph_revision,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Fact cache metadata is missing")

    def identity_hints(
        self, relative_paths: Sequence[str]
    ) -> tuple[EntityIdentityHint, ...]:
        """Load parser identity seeds for changed paths in one bounded query."""
        paths = tuple(sorted(set(relative_paths)))
        if not paths:
            return ()
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT e.uid, e.address, e.kind, sf.relative_path, e.signature, "
                "e.fingerprint FROM entities AS e JOIN source_files AS sf "
                "ON sf.file_id = e.file_id WHERE sf.relative_path IN "
                f"({_placeholders(paths)}) ORDER BY sf.relative_path, e.uid",
                paths,
            ).fetchall()
        return tuple(
            EntityIdentityHint(
                uid=row["uid"],
                address=row["address"],
                kind=row["kind"],
                relative_path=row["relative_path"],
                signature=row["signature"],
                fingerprint=row["fingerprint"],
            )
            for row in rows
        )

    def integrity_ok(self) -> bool:
        """Check that SQLite can safely serve as a replacement cache input."""
        try:
            with self.open_read() as connection:
                quick = connection.execute("PRAGMA quick_check").fetchone()[0]
                foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError:
            return False
        return quick == "ok" and not foreign

    def query_entities(
        self, scope: FactScope, cursor: str | None, limit: int
    ) -> Page[CodeEntity]:
        """Return a bounded page of entities in an allowlisted fact scope."""
        self._validate_limit(limit)
        if scope.kind != "module":
            raise ValueError("Entity query scope is not allowlisted")
        after_uid = _decode_cursor(cursor, "entity")
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT e.uid, sf.relative_path, e.address, e.module_name, e.qualname, "
                "e.kind, e.name, e.parent_uid, e.start_line, e.end_line, e.signature, "
                "e.docstring_digest, e.fingerprint, e.resolution_status "
                "FROM entities AS e "
                "JOIN source_files AS sf ON sf.file_id = e.file_id "
                "WHERE e.module_name = ? AND (? IS NULL OR e.uid > ?) "
                "ORDER BY e.uid ASC LIMIT ?",
                (scope.value, after_uid, after_uid, limit + 1),
            ).fetchall()
        return _entity_page(rows, limit)

    def query_relations(
        self,
        entity_uids: Sequence[str],
        relation_types: Sequence[str],
        limit: int,
        cursor: str | None = None,
    ) -> Page[CodeRelation]:
        """Return a bounded page of relations for explicit source entity UIDs."""
        self._validate_limit(limit)
        uids = _validated_non_empty_strings(entity_uids, "Entity UID list")
        if len(uids) > self.max_relation_entity_uids:
            raise ValueError("Entity UID list exceeds the configured maximum")
        types = _validated_non_empty_strings(relation_types, "Relation type list")
        if any(relation_type not in _ALLOWED_RELATION_TYPES for relation_type in types):
            raise ValueError("Relation types must be allowlisted")
        after_id = _decode_cursor(cursor, "relation")
        uid_placeholders = ", ".join("?" for _ in uids)
        type_placeholders = ", ".join("?" for _ in types)
        statement = (
            "SELECT relation_id, relation_type, source_uid, source_file_id, target_uid, "
            "target_module, target_address, raw_expression, resolution_status, confidence, "
            "resolver_version, relation_key FROM relations "
            f"WHERE source_uid IN ({uid_placeholders}) "
            f"AND relation_type IN ({type_placeholders}) "
            "AND (? IS NULL OR relation_id > ?) "
            "ORDER BY relation_id ASC LIMIT ?"
        )
        parameters = (*uids, *types, after_id, after_id, limit + 1)
        with self.open_read() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return _relation_page(rows, limit)

    def entity_by_uid(self, uid: str) -> CodeEntity | None:
        """Return the current entity for *uid*, or None when it is gone."""
        if not isinstance(uid, str) or not uid:
            raise ValueError("Entity UID must be a non-empty string")
        with self.open_read() as connection:
            row = connection.execute(
                f"{_ENTITY_SELECT} WHERE e.uid = ?", (uid,)
            ).fetchone()
        return None if row is None else _code_entity_from_row(row)

    def resolve_entity_reference(
        self,
        *,
        last_known_address: str,
        kind: str,
        signature: str | None,
        fingerprint: str,
    ) -> EntityReferenceResolution:
        """Resolve one formal entity reference without an unbounded scan.

        The UID is always the caller's first choice.  This method is only the
        conservative fallback for a cache rebuilt without that UID: the
        candidate must retain its kind and normalized signature, and match the
        old address or semantic fingerprint.  Two matches are deliberately
        reported as ``ambiguous`` rather than selected by ordering.
        """
        if not all(
            isinstance(value, str) and value
            for value in (last_known_address, kind, fingerprint)
        ):
            raise ValueError("Entity reference address, kind, and fingerprint are required")
        if signature is not None and not isinstance(signature, str):
            raise ValueError("Entity reference signature must be text or null")
        with self.open_read() as connection:
            rows = connection.execute(
                f"{_ENTITY_SELECT} WHERE e.kind = ? AND e.signature IS ? "
                "AND (e.address = ? OR e.fingerprint = ?) "
                "ORDER BY e.uid ASC LIMIT 2",
                (kind, signature, last_known_address, fingerprint),
            ).fetchall()
        if not rows:
            return EntityReferenceResolution("missing", None)
        if len(rows) > 1:
            return EntityReferenceResolution("ambiguous", None)
        return EntityReferenceResolution("resolved", _code_entity_from_row(rows[0]))

    def entities_at_path(
        self, relative_path: str, cursor: str | None, limit: int
    ) -> Page[CodeEntity]:
        """Return a bounded page of current entities declared in one file."""
        self._validate_limit(limit)
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("Entity path scope must be a non-empty relative path")
        after_uid = _decode_cursor(cursor, "entity")
        with self.open_read() as connection:
            rows = connection.execute(
                f"{_ENTITY_SELECT} WHERE sf.relative_path = ? "
                "AND (? IS NULL OR e.uid > ?) "
                "ORDER BY e.uid ASC LIMIT ?",
                (relative_path, after_uid, after_uid, limit + 1),
            ).fetchall()
        return _entity_page(rows, limit)

    def entities_by_address(
        self, address: str, cursor: str | None, limit: int
    ) -> Page[CodeEntity]:
        """Return a bounded page of current entities with one exact address."""
        self._validate_limit(limit)
        if not isinstance(address, str) or not address:
            raise ValueError("Entity address scope must be a non-empty string")
        after_uid = _decode_cursor(cursor, "entity")
        with self.open_read() as connection:
            rows = connection.execute(
                f"{_ENTITY_SELECT} WHERE e.address = ? "
                "AND (? IS NULL OR e.uid > ?) "
                "ORDER BY e.uid ASC LIMIT ?",
                (address, after_uid, after_uid, limit + 1),
            ).fetchall()
        return _entity_page(rows, limit)

    def analysis_totals(self) -> FactTotals:
        """Return whole-cache aggregate counts with three constant aggregates."""
        with self.open_read() as connection:
            row = connection.execute(
                "SELECT (SELECT COUNT(*) FROM source_files) AS file_count, "
                "(SELECT COUNT(*) FROM entities) AS entity_count, "
                "(SELECT COUNT(*) FROM diagnostics) AS diagnostic_count"
            ).fetchone()
        return FactTotals(
            file_count=row["file_count"],
            entity_count=row["entity_count"],
            diagnostic_count=row["diagnostic_count"],
        )

    def analysis_partitions(
        self, module: str | None, cursor: str | None, limit: int
    ) -> Page[ModulePartition]:
        """Return a bounded, keyset-paginated partition per package/module.

        *module* selects the exact module and its submodules.  Files without a
        module name are grouped under the empty-string partition.  Diagnostics
        without a file (global diagnostics) are not attributed to partitions.
        """
        self._validate_limit(limit)
        if module is not None and (not isinstance(module, str) or not module):
            raise ValueError("Analysis scope must be a non-empty module name")
        after_module = _decode_cursor(cursor, "partition")
        like_prefix = None if module is None else _escape_like_prefix(module)
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT m.module_name AS module_name, m.file_count AS file_count, "
                "COALESCE(e.entity_count, 0) AS entity_count, "
                "COALESCE(d.diagnostic_count, 0) AS diagnostic_count "
                "FROM (SELECT COALESCE(module_name, '') AS module_name, "
                "COUNT(*) AS file_count FROM source_files "
                "GROUP BY COALESCE(module_name, '')) AS m "
                "LEFT JOIN (SELECT module_name, COUNT(*) AS entity_count "
                "FROM entities GROUP BY module_name) AS e "
                "ON e.module_name = m.module_name "
                "LEFT JOIN (SELECT COALESCE(sf.module_name, '') AS module_name, "
                "COUNT(*) AS diagnostic_count FROM diagnostics AS d "
                "JOIN source_files AS sf ON sf.file_id = d.file_id "
                "GROUP BY COALESCE(sf.module_name, '')) AS d "
                "ON d.module_name = m.module_name "
                "WHERE (? IS NULL OR m.module_name = ? "
                "OR m.module_name LIKE (? || '.%') ESCAPE '\\') "
                "AND (? IS NULL OR m.module_name > ?) "
                "ORDER BY m.module_name ASC LIMIT ?",
                (module, module, like_prefix, after_module, after_module, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = tuple(
            ModulePartition(
                module_name=row["module_name"],
                file_count=row["file_count"],
                entity_count=row["entity_count"],
                diagnostic_count=row["diagnostic_count"],
            )
            for row in selected
        )
        truncated = len(rows) > limit
        return Page(
            items=items,
            next_cursor=(
                _encode_cursor("partition", items[-1].module_name)
                if truncated
                else None
            ),
            truncated=truncated,
        )

    def query_diagnostics(
        self, module: str | None, cursor: str | None, limit: int
    ) -> Page[FactDiagnostic]:
        """Return a bounded page of recorded diagnostics, newest cursor last.

        A *module* scope restricts the listing to that module and its
        submodules; global diagnostics (no owning file) are only listed when
        no module scope is given.
        """
        self._validate_limit(limit)
        if module is not None and (not isinstance(module, str) or not module):
            raise ValueError("Diagnostics scope must be a non-empty module name")
        after_id = _decode_cursor(cursor, "diagnostic")
        statement = (
            "SELECT d.diagnostic_id, sf.relative_path, d.code, d.severity, "
            "d.message, d.start_line, d.end_line "
            "FROM diagnostics AS d "
            "LEFT JOIN source_files AS sf ON sf.file_id = d.file_id "
            "WHERE (? IS NULL OR d.diagnostic_id > ?)"
        )
        parameters: tuple[object, ...] = (after_id, after_id)
        if module is not None:
            statement += (
                " AND (sf.module_name = ? "
                "OR sf.module_name LIKE (? || '.%') ESCAPE '\\')"
            )
            parameters = (*parameters, module, _escape_like_prefix(module))
        statement += " ORDER BY d.diagnostic_id ASC LIMIT ?"
        with self.open_read() as connection:
            rows = connection.execute(
                statement, (*parameters, limit + 1)
            ).fetchall()
        selected = rows[:limit]
        items = tuple(
            FactDiagnostic(
                diagnostic_id=row["diagnostic_id"],
                relative_path=row["relative_path"],
                code=row["code"],
                severity=row["severity"],
                message=row["message"],
                start_line=row["start_line"],
                end_line=row["end_line"],
            )
            for row in selected
        )
        truncated = len(rows) > limit
        return Page(
            items=items,
            next_cursor=(
                _encode_cursor("diagnostic", items[-1].diagnostic_id)
                if truncated
                else None
            ),
            truncated=truncated,
        )

    def replace_parsed_files(
        self,
        parsed: Sequence[ParsedFile],
        *,
        deleted_paths: Sequence[str] = (),
    ) -> RelationUpdateScope:
        """Replace changed file facts and return only declarations needing resolution.

        The caller parses files outside the SQLite transaction.  This method
        intentionally captures incoming declaration references before target
        entities disappear, then returns them after the replacement.  Nothing
        here rereads or reparses an unchanged importer.
        """
        with self.open_write() as connection:
            return self._replace_parsed_files(
                connection, parsed, deleted_paths=deleted_paths
            )

    def synchronize(
        self,
        parsed: Sequence[ParsedFile],
        *,
        deleted_paths: Sequence[str],
        metadata: CacheMetadata,
        global_diagnostics: Sequence[tuple[str, str, str]] = (),
    ) -> None:
        """Commit one complete current-facts generation in a short transaction.

        Parsing is deliberately performed by the caller before it takes the
        repository lock.  Once called, this method replaces affected file rows,
        re-resolves both changed and incoming declarations from stored facts,
        writes diagnostics, and updates metadata *last* in one SQLite commit.
        """
        from codecortex.infrastructure.python.resolver import resolve_relations

        with self.open_write() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                scope = self._replace_parsed_files(
                    connection, parsed, deleted_paths=deleted_paths
                )
                declarations = self._current_declarations(
                    connection,
                    (*scope.changed_relation_keys, *(item.relation_key for item in scope.incoming)),
                )
                resolved = resolve_relations(
                    declarations, self._symbol_index(connection)
                )
                for relation in resolved:
                    self._upsert_resolved_relation(connection, relation)
                connection.execute("DELETE FROM diagnostics WHERE file_id IS NULL")
                connection.executemany(
                    "INSERT INTO diagnostics "
                    "(file_id, code, severity, message, start_line, end_line) "
                    "VALUES (NULL, ?, ?, ?, NULL, NULL)",
                    ((code, severity, message) for code, severity, message in global_diagnostics),
                )
                self._update_metadata(connection, metadata)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _replace_parsed_files(
        self,
        connection: sqlite3.Connection,
        parsed: Sequence[ParsedFile],
        *,
        deleted_paths: Sequence[str] = (),
    ) -> RelationUpdateScope:
        """Connection-scoped implementation shared by incremental sync helpers."""
        paths = tuple(item.source.source.relative_path for item in parsed)
        if len(set(paths)) != len(paths):
            raise ValueError("Parsed file paths must be unique")
        replaced_paths = tuple(sorted({*paths, *deleted_paths}))
        old_rows = _source_rows(connection, replaced_paths)
        old_file_ids = tuple(row[0] for row in old_rows)
        old_uids = (
            tuple(
                row[0]
                for row in connection.execute(
                    "SELECT uid FROM entities WHERE file_id IN "
                    f"({_placeholders(old_file_ids)})",
                    old_file_ids,
                ).fetchall()
            )
            if old_file_ids
            else ()
        )
        old_modules = tuple(row[1] for row in old_rows if row[1] is not None)
        old_names = (
            tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM entities WHERE file_id IN "
                    f"({_placeholders(old_file_ids)})",
                    old_file_ids,
                ).fetchall()
            )
            if old_file_ids
            else ()
        )
        new_modules = tuple(item.module_name for item in parsed)
        new_names = tuple(entity.name for item in parsed for entity in item.entities)
        incoming = self._incoming_relation_sources(
            connection,
            old_uids,
            (*old_modules, *new_modules),
            (*old_names, *new_names),
        )
        if replaced_paths:
            connection.execute(
                "DELETE FROM source_files WHERE relative_path IN "
                f"({_placeholders(replaced_paths)})",
                replaced_paths,
            )
        changed_keys: list[str] = []
        for item in parsed:
            changed_keys.extend(self._insert_parsed_file(connection, item))
        return RelationUpdateScope(
            changed_relation_keys=tuple(sorted(set(changed_keys))), incoming=incoming
        )

    def incoming_relation_sources(
        self,
        old_target_uids: Sequence[str],
        module_names: Sequence[str],
        *,
        symbol_names: Sequence[str] = (),
    ) -> tuple[RelationDeclarationRef, ...]:
        """Return declarations that might change when targets/modules change."""
        with self.open_read() as connection:
            return self._incoming_relation_sources(
                connection, old_target_uids, module_names, symbol_names
            )

    def relation_declarations(
        self, relation_keys: Sequence[str]
    ) -> tuple[SyntacticRelation, ...]:
        """Load original declarations by stable key, without accessing source files."""
        with self.open_read() as connection:
            return self._current_declarations(connection, relation_keys)

    def symbol_index(self) -> SymbolIndex:
        """Build the resolver's in-memory projection in one bounded local query."""
        with self.open_read() as connection:
            return self._symbol_index(connection)

    def replace_resolved_relations(self, relations: Sequence[ResolvedRelation]) -> None:
        """Atomically replace resolution/evidence fields for declaration-stable keys."""
        with self.open_write() as connection:
            for relation in relations:
                self._upsert_resolved_relation(connection, relation)

    def relations_for_path(self, relative_path: str) -> tuple[CodeRelation, ...]:
        """A small internal test/incremental-sync projection for one known file."""
        with self.open_read() as connection:
            rows = connection.execute(
                "SELECT relation_id, relation_type, source_uid, source_file_id, target_uid, "
                "target_module, target_address, raw_expression, resolution_status, confidence, "
                "resolver_version, relation_key FROM relations AS r "
                "JOIN source_files AS sf ON sf.file_id = r.source_file_id "
                "WHERE sf.relative_path = ? AND r.relation_type IN "
                "('imports', 'inherits', 'calls', 'tested_by') ORDER BY r.relation_id",
                (relative_path,),
            ).fetchall()
        return tuple(_code_relation_from_row(row) for row in rows)

    def _incoming_relation_sources(
        self,
        connection: sqlite3.Connection,
        old_target_uids: Sequence[str],
        module_names: Sequence[str],
        symbol_names: Sequence[str] = (),
    ) -> tuple[RelationDeclarationRef, ...]:
        uids = tuple(sorted(set(old_target_uids)))
        modules = tuple(sorted({item for item in module_names if item}))
        names = tuple(sorted({item for item in symbol_names if item}))
        clauses: list[str] = []
        parameters: list[str] = []
        if uids:
            clauses.append(f"target_uid IN ({_placeholders(uids)})")
            parameters.extend(uids)
        if modules:
            clauses.append(f"target_module IN ({_placeholders(modules)})")
            parameters.extend(modules)
        if names:
            clauses.append(
                "(resolution_status = 'unresolved' AND target_symbol IN "
                f"({_placeholders(names)}))"
            )
            parameters.extend(names)
        if not clauses:
            return ()
        rows = connection.execute(
            "SELECT relation_key, declaration_type, source_address, relative_path, "
            "declaration_line, declaration_column, raw_expression FROM relations "
            f"WHERE {' OR '.join(clauses)} ORDER BY relation_key",
            parameters,
        ).fetchall()
        seen: set[str] = set()
        output: list[RelationDeclarationRef] = []
        for row in rows:
            key = row["relation_key"].removesuffix(":tested_by")
            if key in seen:
                continue
            seen.add(key)
            output.append(
                RelationDeclarationRef(key, _declaration_from_row(row))
            )
        return tuple(output)

    def _current_declarations(
        self, connection: sqlite3.Connection, relation_keys: Sequence[str]
    ) -> tuple[SyntacticRelation, ...]:
        """Read only declaration rows whose source files still exist."""
        keys = tuple(sorted({key.removesuffix(":tested_by") for key in relation_keys}))
        if not keys:
            return ()
        rows = connection.execute(
            "SELECT relation_key, declaration_type, source_address, relative_path, "
            "declaration_line, declaration_column, raw_expression "
            "FROM relations WHERE relation_key IN "
            f"({_placeholders(keys)}) ORDER BY relation_key",
            keys,
        ).fetchall()
        return tuple(_declaration_from_row(row) for row in rows)

    def _symbol_index(self, connection: sqlite3.Connection) -> SymbolIndex:
        """Build a resolver index from the connection's consistent transaction view."""
        from codecortex.infrastructure.python.resolver import Symbol, SymbolIndex

        rows = connection.execute(
            "SELECT e.uid, e.address, e.module_name, e.qualname, sf.relative_path, "
            "e.kind, e.name FROM entities AS e "
            "JOIN source_files AS sf ON sf.file_id = e.file_id "
            "ORDER BY e.address, e.uid"
        ).fetchall()
        return SymbolIndex.from_symbols(
            Symbol(
                uid=row["uid"],
                address=row["address"],
                module_name=row["module_name"],
                qualname=row["qualname"],
                relative_path=row["relative_path"],
                kind=row["kind"],
                name=row["name"],
            )
            for row in rows
        )

    def _update_metadata(
        self, connection: sqlite3.Connection, metadata: CacheMetadata
    ) -> None:
        """Write cache visibility metadata last, after every fact table mutation."""
        connection.execute(
            "UPDATE cache_metadata SET cache_schema_version = ?, parser_version = ?, "
            "digest_profile_version = ?, managed_source_set_version = ?, "
            "repository_source_digest = ?, graph_revision = ?, "
            "baseline_entity_snapshot_completeness = ?, index_generation = ?, "
            "built_at = ? WHERE singleton_id = 1",
            (
                metadata.cache_schema_version,
                metadata.parser_version,
                metadata.digest_profile_version,
                metadata.managed_source_set_version,
                metadata.repository_source_digest,
                metadata.graph_revision,
                metadata.baseline_entity_snapshot_completeness,
                metadata.index_generation,
                metadata.built_at,
            ),
        )

    def _insert_parsed_file(
        self, connection: sqlite3.Connection, parsed: ParsedFile
    ) -> tuple[str, ...]:
        source = parsed.source
        relative_path = source.source.relative_path
        connection.execute(
            "INSERT INTO source_files "
            "(relative_path, module_name, content_digest, size_bytes, parse_status, is_test, "
            "diagnostic_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                relative_path,
                parsed.module_name,
                source.content_digest,
                source.size_bytes,
                parsed.parse_status,
                int(_is_test_path(relative_path)),
                len(parsed.diagnostics),
            ),
        )
        file_id = connection.execute(
            "SELECT file_id FROM source_files WHERE relative_path = ?", (relative_path,)
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO diagnostics "
            "(file_id, code, severity, message, start_line, end_line) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    file_id,
                    diagnostic.code,
                    diagnostic.severity,
                    diagnostic.message,
                    diagnostic.line,
                    diagnostic.line,
                )
                for diagnostic in parsed.diagnostics
            ),
        )
        addresses_to_uids = {entity.address: entity.uid for entity in parsed.entities}
        for entity in parsed.entities:
            connection.execute(
                "INSERT INTO entities "
                "(uid, file_id, address, module_name, qualname, kind, name, parent_uid, "
                "start_line, end_line, signature, docstring_digest, fingerprint, resolution_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'resolved')",
                (
                    entity.uid,
                    file_id,
                    entity.address,
                    entity.module_name,
                    entity.qualname,
                    entity.kind,
                    entity.name,
                    None if entity.parent_address is None else addresses_to_uids[entity.parent_address],
                    entity.start_line,
                    entity.end_line,
                    entity.signature,
                    entity.docstring_digest,
                    entity.fingerprint,
                ),
            )
        from codecortex.infrastructure.python.resolver import relation_key

        keys: list[str] = []
        resolver_declarations = tuple(
            relation
            for relation in parsed.relations
            if relation.relation_type
            in {"import_declaration", "declared_base", "call_declaration"}
        )
        for declaration in resolver_declarations:
            key = relation_key(declaration)
            keys.append(key)
            connection.execute(
                "INSERT INTO relations "
                "(relation_type, declaration_type, source_uid, source_file_id, source_address, "
                "relative_path, declaration_line, declaration_column, raw_expression, "
                "target_symbol, resolution_status, confidence, resolver_version, relation_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved', 'low', '', ?)",
                (
                    declaration.relation_type,
                    declaration.relation_type,
                    addresses_to_uids.get(declaration.source_address),
                    file_id,
                    declaration.source_address,
                    declaration.relative_path,
                    declaration.start_line,
                    declaration.start_column,
                    declaration.normalized_expression,
                    _target_symbol(declaration),
                    key,
                ),
            )
        return tuple(keys)

    def _upsert_resolved_relation(
        self, connection: sqlite3.Connection, relation: ResolvedRelation
    ) -> None:
        file_row = connection.execute(
            "SELECT file_id FROM source_files WHERE relative_path = ?",
            (relation.declaration.relative_path,),
        ).fetchone()
        if file_row is None:
            raise ValueError("Relation declaration source file does not exist")
        connection.execute(
            "INSERT INTO relations "
            "(relation_type, declaration_type, source_uid, source_file_id, source_address, "
            "relative_path, declaration_line, declaration_column, target_uid, target_module, "
            "target_address, raw_expression, target_symbol, resolution_status, confidence, resolver_version, relation_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(relation_key) DO UPDATE SET "
            "relation_type=excluded.relation_type, source_uid=excluded.source_uid, "
            "target_uid=excluded.target_uid, target_module=excluded.target_module, "
            "target_address=excluded.target_address, resolution_status=excluded.resolution_status, "
            "confidence=excluded.confidence, resolver_version=excluded.resolver_version",
            (
                relation.relation_type,
                relation.declaration.relation_type,
                relation.source_uid,
                file_row[0],
                relation.declaration.source_address,
                relation.declaration.relative_path,
                relation.declaration.start_line,
                relation.declaration.start_column,
                relation.target_uid,
                relation.target_module,
                relation.target_address,
                relation.declaration.normalized_expression,
                _target_symbol(relation.declaration),
                relation.resolution_status,
                relation.confidence,
                relation.resolver_version,
                relation.relation_key,
            ),
        )
        relation_id = connection.execute(
            "SELECT relation_id FROM relations WHERE relation_key = ?", (relation.relation_key,)
        ).fetchone()[0]
        connection.execute("DELETE FROM relation_evidence WHERE relation_id = ?", (relation_id,))
        connection.executemany(
            "INSERT INTO relation_evidence "
            "(relation_id, relative_path, start_line, end_line, evidence_kind, snippet_digest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    relation_id,
                    evidence.relative_path,
                    evidence.start_line,
                    evidence.end_line,
                    evidence.evidence_kind,
                    evidence.snippet_digest,
                )
                for evidence in relation.evidence
            ),
        )

    def insert_test_entities(self, *, module_name: str, count: int) -> None:
        """Populate deterministic fixtures; this is intentionally test-only API."""
        if not isinstance(count, int) or count < 0:
            raise ValueError("Test entity count must be non-negative")
        with self.open_write() as connection:
            source_path = f"__test__/{module_name.replace('.', '/')}.py"
            connection.execute(
                "INSERT OR IGNORE INTO source_files "
                "(relative_path, module_name, content_digest, size_bytes, parse_status, is_test) "
                "VALUES (?, ?, ?, 0, 'parsed', 1)",
                (source_path, module_name, "sha256:" + "0" * 64),
            )
            file_id = connection.execute(
                "SELECT file_id FROM source_files WHERE relative_path = ?", (source_path,)
            ).fetchone()[0]
            connection.executemany(
                "INSERT OR IGNORE INTO entities "
                "(uid, file_id, address, module_name, qualname, kind, name, "
                "start_line, end_line, fingerprint, resolution_status) "
                "VALUES (?, ?, ?, ?, ?, 'function', ?, 1, 1, ?, 'resolved')",
                (
                    (
                        f"test_ent_{index:05d}",
                        file_id,
                        f"{module_name}:test_{index:05d}",
                        module_name,
                        f"test_{index:05d}",
                        f"test_{index:05d}",
                        "sha256:" + f"{index:064x}",
                    )
                    for index in range(count)
                ),
            )

    def insert_test_relations(self, *, source_uid: str, count: int) -> None:
        """Populate deterministic relation fixtures; this is intentionally test-only."""
        if not isinstance(count, int) or count < 0:
            raise ValueError("Test relation count must be non-negative")
        with self.open_write() as connection:
            row = connection.execute(
                "SELECT file_id FROM entities WHERE uid = ?", (source_uid,)
            ).fetchone()
            if row is None:
                raise ValueError("Test relation source entity does not exist")
            connection.executemany(
                "INSERT OR IGNORE INTO relations "
                "(relation_type, source_uid, source_file_id, resolution_status, confidence, "
                "resolver_version, relation_key) VALUES "
                "('calls', ?, ?, 'unresolved', 'inferred', 'test-v1', ?)",
                (
                    (source_uid, row[0], f"test_relation_{source_uid}_{index:05d}")
                    for index in range(count)
                ),
            )

    def _validate_limit(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("Query limit must be positive")
        if limit > self.max_page_size:
            raise ValueError("Query limit exceeds the configured maximum")


def _validated_non_empty_strings(values: Sequence[str], label: str) -> tuple[str, ...]:
    materialized = tuple(values)
    if not materialized:
        raise ValueError(f"{label} must contain at least one value")
    if any(not isinstance(value, str) or not value for value in materialized):
        raise ValueError(f"{label} must contain non-empty strings")
    return materialized


def _encode_cursor(kind: str, value: str | int) -> str:
    payload = json.dumps({"kind": kind, "value": value}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, expected_kind: str) -> str | int | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Query cursor is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Query cursor is invalid") from error
    if (
        not isinstance(parsed, dict)
        or parsed.get("kind") != expected_kind
        or "value" not in parsed
    ):
        raise ValueError("Query cursor is invalid")
    value = parsed["value"]
    if expected_kind == "entity" and isinstance(value, str) and value:
        return value
    if expected_kind == "partition" and isinstance(value, str):
        return value
    if expected_kind in ("relation", "diagnostic") and isinstance(value, int) and value >= 0:
        return value
    raise ValueError("Query cursor is invalid")


_ENTITY_SELECT = (
    "SELECT e.uid, sf.relative_path, e.address, e.module_name, e.qualname, "
    "e.kind, e.name, e.parent_uid, e.start_line, e.end_line, e.signature, "
    "e.docstring_digest, e.fingerprint, e.resolution_status "
    "FROM entities AS e JOIN source_files AS sf ON sf.file_id = e.file_id "
)


def _code_entity_from_row(row: sqlite3.Row) -> CodeEntity:
    return CodeEntity(
        uid=row["uid"],
        relative_path=row["relative_path"],
        address=row["address"],
        module_name=row["module_name"],
        qualname=row["qualname"],
        kind=row["kind"],
        name=row["name"],
        parent_uid=row["parent_uid"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        signature=row["signature"],
        docstring_digest=row["docstring_digest"],
        fingerprint=row["fingerprint"],
        resolution_status=row["resolution_status"],
    )


def _escape_like_prefix(value: str) -> str:
    """Escape one module prefix for a LIKE pattern with the backslash escape."""
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _entity_page(rows: Sequence[sqlite3.Row], limit: int) -> Page[CodeEntity]:
    selected = rows[:limit]
    items = tuple(
        CodeEntity(
            uid=row["uid"],
            relative_path=row["relative_path"],
            address=row["address"],
            module_name=row["module_name"],
            qualname=row["qualname"],
            kind=row["kind"],
            name=row["name"],
            parent_uid=row["parent_uid"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            signature=row["signature"],
            docstring_digest=row["docstring_digest"],
            fingerprint=row["fingerprint"],
            resolution_status=row["resolution_status"],
        )
        for row in selected
    )
    truncated = len(rows) > limit
    return Page(
        items=items,
        next_cursor=_encode_cursor("entity", items[-1].uid) if truncated else None,
        truncated=truncated,
    )


def _relation_page(rows: Sequence[sqlite3.Row], limit: int) -> Page[CodeRelation]:
    selected = rows[:limit]
    items = tuple(
        _code_relation_from_row(row)
        for row in selected
    )
    truncated = len(rows) > limit
    return Page(
        items=items,
        next_cursor=_encode_cursor("relation", items[-1].relation_id) if truncated else None,
        truncated=truncated,
    )


def _code_relation_from_row(row: sqlite3.Row) -> CodeRelation:
    return CodeRelation(
        relation_id=row["relation_id"],
        relation_type=row["relation_type"],
        source_uid=row["source_uid"],
        source_file_id=row["source_file_id"],
        target_uid=row["target_uid"],
        target_module=row["target_module"],
        target_address=row["target_address"],
        raw_expression=row["raw_expression"],
        resolution_status=row["resolution_status"],
        confidence=row["confidence"],
        resolver_version=row["resolver_version"],
        relation_key=row["relation_key"],
    )


def _declaration_from_row(row: sqlite3.Row) -> SyntacticRelation:
    return SyntacticRelation(
        relation_type=row["declaration_type"],
        source_address=row["source_address"],
        target_address=None,
        relative_path=row["relative_path"],
        start_line=row["declaration_line"],
        start_column=row["declaration_column"],
        normalized_expression=row["raw_expression"],
    )


def _source_rows(
    connection: sqlite3.Connection, relative_paths: Sequence[str]
) -> tuple[sqlite3.Row, ...]:
    if not relative_paths:
        return ()
    return tuple(
        connection.execute(
            "SELECT file_id, module_name FROM source_files WHERE relative_path IN "
            f"({_placeholders(relative_paths)})",
            tuple(relative_paths),
        ).fetchall()
    )


def _placeholders(values: Sequence[object]) -> str:
    if not values:
        raise ValueError("SQL placeholder list must not be empty")
    return ", ".join("?" for _ in values)


def _is_test_path(relative_path: str) -> bool:
    filename = relative_path.rsplit("/", 1)[-1]
    return filename.startswith("test_") or "/tests/" in f"/{relative_path}"


def _target_symbol(declaration: SyntacticRelation) -> str | None:
    """Extract an identifier hint used only to revisit unresolved declarations."""
    try:
        statement = ast.parse(declaration.normalized_expression).body[0]
    except SyntaxError:
        return None
    if isinstance(statement, ast.ImportFrom) and len(statement.names) == 1:
        return statement.names[0].name
    if isinstance(statement, ast.Import) and len(statement.names) == 1:
        return statement.names[0].name.rsplit(".", 1)[-1]
    try:
        expression = ast.parse(declaration.normalized_expression, mode="eval").body
    except SyntaxError:
        return None
    while isinstance(expression, ast.Attribute):
        expression = expression.value
    return expression.id if isinstance(expression, ast.Name) else None


def _ensure_task4_relation_columns(connection: sqlite3.Connection) -> None:
    """Upgrade the disposable Task 3 cache schema without touching formal state."""
    actual = {
        row[1] for row in connection.execute("PRAGMA table_info(relations)").fetchall()
    }
    additions = (
        ("declaration_type", "TEXT NOT NULL DEFAULT 'import_declaration'"),
        ("source_address", "TEXT NOT NULL DEFAULT ''"),
        ("relative_path", "TEXT NOT NULL DEFAULT ''"),
        ("declaration_line", "INTEGER NOT NULL DEFAULT 0"),
        ("declaration_column", "INTEGER NOT NULL DEFAULT 0"),
        ("target_address", "TEXT"),
        ("target_symbol", "TEXT"),
    )
    for name, definition in additions:
        if name not in actual:
            connection.execute(f"ALTER TABLE relations ADD COLUMN {name} {definition}")


_SCHEMA_TABLES = frozenset(
    {
        "cache_metadata",
        "source_files",
        "entities",
        "relations",
        "relation_evidence",
        "diagnostics",
        "baseline_entity_snapshots",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS cache_metadata (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  cache_schema_version INTEGER NOT NULL,
  parser_version TEXT NOT NULL,
  digest_profile_version INTEGER NOT NULL,
  managed_source_set_version INTEGER NOT NULL,
  repository_source_digest TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  baseline_entity_snapshot_completeness TEXT NOT NULL,
  index_generation INTEGER NOT NULL,
  built_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
  file_id INTEGER PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  module_name TEXT,
  content_digest TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  parse_status TEXT NOT NULL,
  is_test INTEGER NOT NULL CHECK (is_test IN (0, 1)),
  diagnostic_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entities (
  uid TEXT PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES source_files(file_id) ON DELETE CASCADE,
  address TEXT NOT NULL,
  module_name TEXT NOT NULL,
  qualname TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  parent_uid TEXT REFERENCES entities(uid) ON DELETE CASCADE,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  signature TEXT,
  docstring_digest TEXT,
  fingerprint TEXT NOT NULL,
  resolution_status TEXT NOT NULL,
  UNIQUE(address, kind)
);

CREATE TABLE IF NOT EXISTS relations (
  relation_id INTEGER PRIMARY KEY,
  relation_type TEXT NOT NULL,
  declaration_type TEXT NOT NULL DEFAULT 'import_declaration',
  source_uid TEXT REFERENCES entities(uid) ON DELETE CASCADE,
  source_file_id INTEGER NOT NULL REFERENCES source_files(file_id) ON DELETE CASCADE,
  source_address TEXT NOT NULL DEFAULT '',
  relative_path TEXT NOT NULL DEFAULT '',
  declaration_line INTEGER NOT NULL DEFAULT 0,
  declaration_column INTEGER NOT NULL DEFAULT 0,
  target_uid TEXT REFERENCES entities(uid) ON DELETE SET NULL,
  target_module TEXT,
  target_address TEXT,
  raw_expression TEXT,
  target_symbol TEXT,
  resolution_status TEXT NOT NULL,
  confidence TEXT NOT NULL,
  resolver_version TEXT NOT NULL,
  relation_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS relation_evidence (
  evidence_id INTEGER PRIMARY KEY,
  relation_id INTEGER NOT NULL REFERENCES relations(relation_id) ON DELETE CASCADE,
  relative_path TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  evidence_kind TEXT NOT NULL,
  snippet_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostics (
  diagnostic_id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES source_files(file_id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER
);

CREATE TABLE IF NOT EXISTS baseline_entity_snapshots (
  uid TEXT PRIMARY KEY,
  baseline_source_digest TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  address TEXT NOT NULL,
  module_name TEXT NOT NULL,
  qualname TEXT NOT NULL,
  kind TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  signature TEXT,
  UNIQUE(baseline_source_digest, address, kind)
);

CREATE INDEX IF NOT EXISTS ix_source_files_module_name ON source_files(module_name);
CREATE INDEX IF NOT EXISTS ix_source_files_content_digest ON source_files(content_digest);
CREATE INDEX IF NOT EXISTS ix_entities_file_id ON entities(file_id);
CREATE INDEX IF NOT EXISTS ix_entities_module_name ON entities(module_name);
CREATE INDEX IF NOT EXISTS ix_entities_parent_uid ON entities(parent_uid);
CREATE INDEX IF NOT EXISTS ix_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS ix_entities_fingerprint ON entities(fingerprint);
CREATE INDEX IF NOT EXISTS ix_entities_address ON entities(address);
CREATE INDEX IF NOT EXISTS ix_relations_source_uid_type ON relations(source_uid, relation_type);
CREATE INDEX IF NOT EXISTS ix_relations_target_uid_type ON relations(target_uid, relation_type);
CREATE INDEX IF NOT EXISTS ix_relations_target_module_type ON relations(target_module, relation_type);
CREATE INDEX IF NOT EXISTS ix_diagnostics_file_severity ON diagnostics(file_id, severity);
CREATE INDEX IF NOT EXISTS ix_baseline_snapshots_relative_path ON baseline_entity_snapshots(relative_path);
CREATE INDEX IF NOT EXISTS ix_baseline_snapshots_module_fingerprint
  ON baseline_entity_snapshots(module_name, fingerprint);
"""
