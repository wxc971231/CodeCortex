"""SQLite-backed, bounded access to rebuildable Python code facts.

This module intentionally exposes a small repository API instead of allowing
callers to compose arbitrary SQL.  The cache is an implementation detail, so
all reads are paginated and all analyzer-facing connections are read-only.
"""

import base64
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_CACHE_SCHEMA_VERSION = 1
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
class CodeRelation:
    """The query projection for one current source declaration relation."""

    relation_id: int
    relation_type: str
    source_uid: str | None
    source_file_id: int
    target_uid: str | None
    target_module: str | None
    raw_expression: str | None
    resolution_status: str
    confidence: str
    resolver_version: str
    relation_key: str


class FactsDatabase:
    """A local SQLite fact cache with strict connection and query policies."""

    max_page_size = 100
    max_relation_entity_uids = 200

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def create_new(cls, path: Path) -> FactsDatabase:
        """Create the schema (or verify an existing compatible schema) at *path*."""
        database = cls(path)
        database.path.parent.mkdir(parents=True, exist_ok=True)
        with database.open_write() as connection:
            connection.executescript(_DDL)
            connection.execute(
                "INSERT OR IGNORE INTO cache_metadata "
                "(singleton_id, cache_schema_version, parser_version, "
                "digest_profile_version, managed_source_set_version, "
                "repository_source_digest, graph_revision, "
                "baseline_entity_snapshot_completeness, index_generation, built_at) "
                "VALUES (1, ?, '', 1, 1, 'sha256:', 0, 'unknown', 0, '')",
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
            "target_module, raw_expression, resolution_status, confidence, "
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
    if expected_kind == "relation" and isinstance(value, int) and value >= 0:
        return value
    raise ValueError("Query cursor is invalid")


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
        CodeRelation(
            relation_id=row["relation_id"],
            relation_type=row["relation_type"],
            source_uid=row["source_uid"],
            source_file_id=row["source_file_id"],
            target_uid=row["target_uid"],
            target_module=row["target_module"],
            raw_expression=row["raw_expression"],
            resolution_status=row["resolution_status"],
            confidence=row["confidence"],
            resolver_version=row["resolver_version"],
            relation_key=row["relation_key"],
        )
        for row in selected
    )
    truncated = len(rows) > limit
    return Page(
        items=items,
        next_cursor=_encode_cursor("relation", items[-1].relation_id) if truncated else None,
        truncated=truncated,
    )


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
  source_uid TEXT REFERENCES entities(uid) ON DELETE CASCADE,
  source_file_id INTEGER NOT NULL REFERENCES source_files(file_id) ON DELETE CASCADE,
  target_uid TEXT REFERENCES entities(uid) ON DELETE SET NULL,
  target_module TEXT,
  raw_expression TEXT,
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
