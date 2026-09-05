"""SQLite cognitive query replica for bounded M1a graph reads.

The replica is a local, disposable query projection of the formal cognitive
graph.  It never writes formal state: ``rebuild`` imports one fully validated
``CognitiveGraph`` snapshot in a single all-or-nothing SQLite transaction, and
``search``/``context`` are bounded read-only queries that refuse to answer
when the replica revision does not match the caller's expectation.

Design authority: ``docs/CodeCortex_M1a_Detailed_Design.md`` section 7.

Documented design decisions:

- The typed ``CognitiveGraph`` carries only the referenced entity UID set, so
  ``entity_reference_index`` rows (from formal ``entity_refs.json``) and
  ``history_event_index`` rows (from formal history events) arrive through
  constructor providers evaluated at rebuild time.
- Flow-level evidence is imported under its owning behavior node
  (``owner_kind="node"``), because section 7 fixes the polymorphic evidence
  owner kinds to node/flow_step/edge/mapping and a logical flow's primary key
  is its behavior.
- Search weights are fixed: title 50, exact alias 40, alias 30, summary 20,
  observed 10.  Queries reuse the same NFKC + casefold term extraction as the
  importer, plus the full normalized query string so multi-word exact aliases
  can match.  No FTS5 dependency.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.graph import (
    Approval,
    CognitiveGraph,
    Evidence,
    validate_cognitive_graph,
)

_REPLICA_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 10_000

_LATIN_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(
    "[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]+"
)

_NODE_KINDS = frozenset({"responsibility", "behavior", "capability"})

NODE_KINDS = frozenset(_NODE_KINDS)
"""Public read-only alias used by the application query layer (Task 8)."""

_FIELD_WEIGHTS = {
    "title": 50,
    "exact_alias": 40,
    "alias": 30,
    "summary": 20,
    "observed": 10,
}

_REPLICA_TABLES = frozenset(
    {
        "replica_metadata",
        "cognitive_nodes",
        "cognitive_aliases",
        "cognitive_edges",
        "logical_flows",
        "flow_steps",
        "flow_step_capabilities",
        "entity_reference_index",
        "implementation_mappings",
        "cognitive_evidence",
        "history_event_index",
        "cognitive_search_terms",
    }
)

_DELETE_ORDER = (
    "cognitive_search_terms",
    "cognitive_aliases",
    "cognitive_evidence",
    "implementation_mappings",
    "flow_step_capabilities",
    "flow_steps",
    "logical_flows",
    "cognitive_edges",
    "cognitive_nodes",
    "entity_reference_index",
    "history_event_index",
)

_DDL = """
CREATE TABLE IF NOT EXISTS replica_metadata (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  replica_schema_version INTEGER NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cognitive_nodes (
  node_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('responsibility', 'behavior', 'capability')),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  epistemic_status TEXT NOT NULL
    CHECK (epistemic_status IN ('established', 'inferred', 'uncertain')),
  intent TEXT,
  observed TEXT NOT NULL,
  node_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cognitive_aliases (
  node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  PRIMARY KEY (node_id, position),
  UNIQUE (node_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS cognitive_edges (
  edge_id TEXT PRIMARY KEY,
  edge_type TEXT NOT NULL CHECK (edge_type IN ('contains', 'uses', 'depends_on')),
  source_node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  target_node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  epistemic_status TEXT NOT NULL
    CHECK (epistemic_status IN ('established', 'inferred', 'uncertain')),
  edge_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  UNIQUE (edge_type, source_node_id, target_node_id)
);

CREATE TABLE IF NOT EXISTS logical_flows (
  behavior_id TEXT PRIMARY KEY REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  materialization_status TEXT NOT NULL
    CHECK (materialization_status IN
      ('unmaterialized', 'materialized', 'not_applicable')),
  flow_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS flow_steps (
  step_id TEXT PRIMARY KEY,
  behavior_id TEXT NOT NULL REFERENCES logical_flows(behavior_id) ON DELETE CASCADE,
  step_order INTEGER NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  UNIQUE (behavior_id, step_order)
);

CREATE TABLE IF NOT EXISTS flow_step_capabilities (
  step_id TEXT NOT NULL REFERENCES flow_steps(step_id) ON DELETE CASCADE,
  capability_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (step_id, capability_id),
  UNIQUE (step_id, position)
);

CREATE TABLE IF NOT EXISTS entity_reference_index (
  entity_uid TEXT PRIMARY KEY,
  last_known_address TEXT NOT NULL,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  signature TEXT,
  fingerprint TEXT NOT NULL,
  resolution_status TEXT NOT NULL
    CHECK (resolution_status IN ('resolved', 'missing', 'ambiguous')),
  graph_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS implementation_mappings (
  mapping_id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('node', 'flow_step')),
  subject_id TEXT NOT NULL,
  entity_uid TEXT NOT NULL REFERENCES entity_reference_index(entity_uid),
  role TEXT NOT NULL CHECK (role IN ('primary', 'supporting')),
  resolution_status TEXT NOT NULL
    CHECK (resolution_status IN ('resolved', 'missing', 'ambiguous')),
  evidence_note TEXT NOT NULL,
  mapping_revision INTEGER NOT NULL,
  approval_event_id TEXT NOT NULL,
  graph_revision INTEGER NOT NULL,
  UNIQUE (subject_kind, subject_id, entity_uid, role)
);

CREATE TABLE IF NOT EXISTS cognitive_evidence (
  evidence_id TEXT PRIMARY KEY,
  owner_kind TEXT NOT NULL
    CHECK (owner_kind IN ('node', 'edge', 'flow_step', 'mapping')),
  owner_id TEXT NOT NULL,
  evidence_kind TEXT NOT NULL
    CHECK (evidence_kind IN ('code_entity', 'repository_document')),
  entity_uid TEXT REFERENCES entity_reference_index(entity_uid),
  relative_path TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  observation TEXT NOT NULL,
  graph_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS history_event_index (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  resulting_graph_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  relative_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cognitive_search_terms (
  term TEXT NOT NULL,
  node_id TEXT NOT NULL REFERENCES cognitive_nodes(node_id) ON DELETE CASCADE,
  field TEXT NOT NULL
    CHECK (field IN ('title', 'exact_alias', 'alias', 'summary', 'observed')),
  weight INTEGER NOT NULL,
  PRIMARY KEY (term, node_id, field)
);

CREATE INDEX IF NOT EXISTS idx_cognitive_nodes_kind ON cognitive_nodes(kind, node_id);
CREATE INDEX IF NOT EXISTS idx_cognitive_aliases_normalized
  ON cognitive_aliases(normalized_alias, node_id);
CREATE INDEX IF NOT EXISTS idx_cognitive_edges_source ON cognitive_edges(source_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_cognitive_edges_target ON cognitive_edges(target_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_logical_flows_status
  ON logical_flows(materialization_status, behavior_id);
CREATE INDEX IF NOT EXISTS idx_flow_steps_behavior ON flow_steps(behavior_id, step_order);
CREATE INDEX IF NOT EXISTS idx_mappings_subject ON implementation_mappings(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS idx_mappings_entity ON implementation_mappings(entity_uid, resolution_status);
CREATE INDEX IF NOT EXISTS idx_evidence_owner ON cognitive_evidence(owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS idx_evidence_entity ON cognitive_evidence(entity_uid);
CREATE INDEX IF NOT EXISTS idx_evidence_path ON cognitive_evidence(relative_path);
CREATE INDEX IF NOT EXISTS idx_history_revision
  ON history_event_index(resulting_graph_revision, event_type);
CREATE INDEX IF NOT EXISTS idx_search_terms_term ON cognitive_search_terms(term, weight, node_id);
"""

_POLYMORPHIC_MAPPING_CHECK = """
SELECT mapping_id FROM implementation_mappings AS m
WHERE (m.subject_kind = 'node' AND NOT EXISTS (
         SELECT 1 FROM cognitive_nodes AS n WHERE n.node_id = m.subject_id))
   OR (m.subject_kind = 'flow_step' AND NOT EXISTS (
         SELECT 1 FROM flow_steps AS s WHERE s.step_id = m.subject_id))
"""

_POLYMORPHIC_EVIDENCE_CHECK = """
SELECT evidence_id FROM cognitive_evidence AS e
WHERE (e.owner_kind = 'node' AND NOT EXISTS (
         SELECT 1 FROM cognitive_nodes AS n WHERE n.node_id = e.owner_id))
   OR (e.owner_kind = 'edge' AND NOT EXISTS (
         SELECT 1 FROM cognitive_edges AS d WHERE d.edge_id = e.owner_id))
   OR (e.owner_kind = 'flow_step' AND NOT EXISTS (
         SELECT 1 FROM flow_steps AS s WHERE s.step_id = e.owner_id))
   OR (e.owner_kind = 'mapping' AND NOT EXISTS (
         SELECT 1 FROM implementation_mappings AS m WHERE m.mapping_id = e.owner_id))
"""


@dataclass(frozen=True, slots=True)
class EntityRefRecord:
    """One formal ``entity_refs.json`` record consumed by the importer."""

    uid: str
    last_known_address: str
    kind: str
    relative_path: str
    signature: str | None
    fingerprint: str
    resolution_status: str


@dataclass(frozen=True, slots=True)
class HistoryEventRecord:
    """The history-event fields the replica indexes for provenance lookups."""

    event_id: str
    event_type: str
    resulting_graph_revision: int
    created_at: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class ReplicaMetadata:
    """The single replica-generation row used to reject mixed snapshots."""

    replica_schema_version: int
    graph_revision: int


@dataclass(frozen=True, slots=True)
class GraphHit:
    """One deterministic search hit ordered by score, kind, and node ID."""

    node_id: str
    kind: str
    title: str
    score: int
    matched_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """A bounded discussion-context traversal request.

    ``expected_source_digest`` is validated by the application-layer cache
    guard (Task 8), not by this replica; the replica only enforces the graph
    revision when ``expected_graph_revision`` is provided.
    """

    node_ids: tuple[str, ...] = ()
    entity_uids: tuple[str, ...] = ()
    depth: int = 2
    max_nodes: int = 40
    max_entities: int = 80
    max_evidence: int = 80
    expected_graph_revision: int | None = None
    expected_source_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ContextNode:
    node_id: str
    kind: str
    title: str
    aliases: tuple[str, ...]
    summary: str
    intent: str | None
    observed: str
    epistemic_status: str
    node_revision: int


@dataclass(frozen=True, slots=True)
class ContextEdge:
    edge_id: str
    edge_type: str
    source_node_id: str
    target_node_id: str
    epistemic_status: str


@dataclass(frozen=True, slots=True)
class ContextStep:
    step_id: str
    order: int
    title: str
    summary: str
    uses_capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextFlow:
    behavior_id: str
    materialization_status: str
    steps: tuple[ContextStep, ...]


@dataclass(frozen=True, slots=True)
class ContextMapping:
    mapping_id: str
    subject_kind: str
    subject_id: str
    entity_uid: str
    role: str
    resolution_status: str
    evidence_note: str


@dataclass(frozen=True, slots=True)
class ContextEntity:
    entity_uid: str
    last_known_address: str
    kind: str
    relative_path: str
    signature: str | None
    fingerprint: str
    resolution_status: str


@dataclass(frozen=True, slots=True)
class ContextEvidence:
    evidence_id: str
    owner_kind: str
    owner_id: str
    evidence_kind: str
    entity_uid: str | None
    relative_path: str
    start_line: int | None
    end_line: int | None
    observation: str


@dataclass(frozen=True, slots=True)
class DiscussionContext:
    """One bounded, deterministic discussion-context projection."""

    graph_revision: int
    nodes: tuple[ContextNode, ...]
    edges: tuple[ContextEdge, ...]
    flows: tuple[ContextFlow, ...]
    mappings: tuple[ContextMapping, ...]
    entities: tuple[ContextEntity, ...]
    evidence: tuple[ContextEvidence, ...]
    truncated: bool
    continuation: str | None


@dataclass(frozen=True, slots=True)
class _ContextBundle:
    """The materialized traversal result before revision/continuation."""

    nodes: tuple[ContextNode, ...]
    edges: tuple[ContextEdge, ...]
    flows: tuple[ContextFlow, ...]
    mappings: tuple[ContextMapping, ...]
    entities: tuple[ContextEntity, ...]
    evidence: tuple[ContextEvidence, ...]
    truncated: bool


def _ngrams(run: str, size: int) -> tuple[str, ...]:
    if size < 1 or size > len(run):
        return ()
    return tuple(run[index : index + size] for index in range(len(run) - size + 1))


def normalize_search_terms(text: str) -> tuple[str, ...]:
    """Extract the deterministic search term set shared by import and query.

    Latin alphanumeric tokens come straight from the NFKC + casefolded text;
    every CJK run contributes its full form plus its bigrams and trigrams.
    """

    normalized = unicodedata.normalize("NFKC", text).casefold()
    latin = _LATIN_TOKEN.findall(normalized)
    cjk = [
        term
        for run in _CJK_RUN.findall(normalized)
        for size in (len(run), 2, 3)
        for term in _ngrams(run, size)
    ]
    return tuple(sorted({*latin, *cjk}))


def _normalize_alias(alias: str) -> str:
    return unicodedata.normalize("NFKC", alias).casefold().strip()


def _query_terms(query: str) -> tuple[str, ...]:
    """Query-side terms: import terms plus the full normalized query string.

    The full-string term lets multi-word aliases match the ``exact_alias``
    field, mirroring how the importer indexes the complete normalized alias.
    """

    normalized = unicodedata.normalize("NFKC", query).casefold().strip()
    terms = set(normalize_search_terms(query))
    if normalized:
        terms.add(normalized)
    return tuple(sorted(terms))


def _event_id(approval: Approval | None, owner: str) -> str:
    if approval is None:
        # validate_cognitive_graph rejects missing approvals before import.
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            f"Formal object {owner} is missing its approval event",
        )
    return approval.approval_event_id


def _encode_continuation(node_ids: Sequence[str]) -> str:
    payload = json.dumps(
        {"kind": "context", "value": sorted(node_ids)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _in_clause(values: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    materialized = tuple(values)
    return ", ".join("?" for _ in materialized), materialized


class GraphReplica:
    """A local SQLite query replica of the formal cognitive graph."""

    max_limit = 100
    max_depth = 10
    max_context_nodes = 200
    max_context_entities = 500
    max_context_evidence = 500
    max_anchors = 100
    schema_version = _REPLICA_SCHEMA_VERSION

    def __init__(
        self,
        path: Path,
        *,
        read_only: bool = False,
        entity_refs: Iterable[EntityRefRecord]
        | Callable[[], Iterable[EntityRefRecord]] = (),
        history_events: Iterable[HistoryEventRecord]
        | Callable[[], Iterable[HistoryEventRecord]] = (),
    ) -> None:
        self.path = Path(path)
        self._read_only = read_only
        self._entity_refs = (
            entity_refs if callable(entity_refs) else lambda: entity_refs
        )
        self._history_events = (
            history_events if callable(history_events) else lambda: history_events
        )

    @classmethod
    def create_new(cls, path: Path, **providers: object) -> GraphReplica:
        """Create the replica schema (or verify a compatible one) at *path*."""
        replica = cls(path, **providers)  # type: ignore[arg-type]
        try:
            replica._create_schema()
        except sqlite3.DatabaseError as error:
            if not _is_corrupt_sqlite(error):
                raise
            # This database is a wholly disposable local projection.  A
            # damaged file must never prevent the formal-state recovery path
            # from starting on a new machine.
            replica.reset()
        return replica

    def reset(self) -> None:
        """Discard a damaged local replica and recreate only its empty schema."""
        if self._read_only:
            raise sqlite3.OperationalError(
                "Read-only cognitive replica cannot be reset"
            )
        for path in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            path.unlink(missing_ok=True)
        self._create_schema()

    def _create_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.open_write() as connection:
            connection.executescript(_DDL)

    def open_read(self) -> sqlite3.Connection:
        """Open a URI-mode read-only, query-only connection for bounded reads."""
        connection = sqlite3.connect(self._read_uri(), uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only = ON")
        return connection

    def open_write(self) -> sqlite3.Connection:
        """Open the only connection mode permitted to mutate the replica."""
        if self._read_only:
            raise sqlite3.OperationalError(
                "Read-only cognitive replica cannot be modified"
            )
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return connection

    def _read_uri(self) -> str:
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        if not self._read_only:
            return uri
        wal_exists = Path(f"{self.path}-wal").exists()
        shm_exists = Path(f"{self.path}-shm").exists()
        if shm_exists and not wal_exists:
            raise sqlite3.OperationalError(
                "Read-only cognitive replica has an incomplete WAL coordinate"
            )
        return uri if wal_exists else f"{uri}&immutable=1"

    def foreign_keys_enabled(self) -> bool:
        with self.open_read() as connection:
            return connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def has_index(self, table: str, columns: tuple[str, ...]) -> bool:
        """Return whether *table* has an index whose ordered columns match."""
        if table not in _REPLICA_TABLES:
            return False
        with self.open_read() as connection:
            for index in connection.execute(f"PRAGMA index_list({table})"):
                actual = tuple(
                    row[2]
                    for row in connection.execute(f"PRAGMA index_info({index[1]})")
                )
                if actual == columns:
                    return True
        return False

    def metadata(self) -> ReplicaMetadata:
        """Load the sole replica metadata row, requiring a usable replica."""
        with self.open_read() as connection:
            row = connection.execute(
                "SELECT replica_schema_version, graph_revision "
                "FROM replica_metadata WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise self._rebuild_required("Cognitive replica has not been built")
        if row["replica_schema_version"] != _REPLICA_SCHEMA_VERSION:
            raise self._rebuild_required("Cognitive replica schema is incompatible")
        return ReplicaMetadata(row["replica_schema_version"], row["graph_revision"])

    def rebuild(self, graph: CognitiveGraph, graph_revision: int) -> None:
        """Replace the whole replica with one validated graph snapshot.

        The import validates the graph, the entity-ref coverage, and the
        polymorphic subject/evidence references, then imports every section in
        a single transaction.  Metadata is written last, so any failure keeps
        the previous replica fully visible and marked at its old revision.
        """

        if type(graph_revision) is not int or graph_revision < 0:
            raise ValueError("Graph revision must be a non-negative integer")
        if graph.graph_revision != graph_revision:
            raise CodeCortexError(
                ErrorCode.GRAPH_REVISION_CONFLICT,
                "Graph snapshot revision does not match the rebuild argument",
                details={
                    "snapshot_graph_revision": graph.graph_revision,
                    "requested_graph_revision": graph_revision,
                },
            )
        violations = validate_cognitive_graph(graph)
        if violations:
            raise CodeCortexError(
                ErrorCode.FORMAL_STATE_CORRUPT,
                "Cognitive graph violates formal invariants and cannot be indexed",
                details={
                    "violations": [
                        {
                            "code": violation.code,
                            "location": violation.location,
                            "message": violation.message,
                        }
                        for violation in violations
                    ]
                },
            )
        entity_refs = self._validated_entity_refs(graph)
        history_events = self._validated_history_events()

        with self.open_write() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in _DELETE_ORDER:
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DELETE FROM replica_metadata")
            self._import_nodes(connection, graph)
            self._import_edges(connection, graph)
            self._import_flows(connection, graph)
            self._import_entity_refs(connection, entity_refs, graph_revision)
            self._import_mappings(connection, graph)
            self._import_evidence(connection, graph)
            self._import_history(connection, history_events)
            self._import_search_terms(connection, graph)
            self._check_import_integrity(connection)
            connection.execute(
                "INSERT INTO replica_metadata "
                "(singleton_id, replica_schema_version, graph_revision) "
                "VALUES (1, ?, ?)",
                (_REPLICA_SCHEMA_VERSION, graph_revision),
            )

    @staticmethod
    def _rebuild_required(message: str) -> CodeCortexError:
        return CodeCortexError(
            ErrorCode.CACHE_REBUILD_REQUIRED,
            message,
            retryable=True,
            suggested_action="Rebuild the cognitive query replica from formal state",
        )

    def _current_revision(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT replica_schema_version, graph_revision "
            "FROM replica_metadata WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise self._rebuild_required("Cognitive replica has not been built")
        if row["replica_schema_version"] != _REPLICA_SCHEMA_VERSION:
            raise self._rebuild_required("Cognitive replica schema is incompatible")
        return row["graph_revision"]

    def _require_revision(
        self, connection: sqlite3.Connection, expected_revision: int
    ) -> None:
        if (
            type(expected_revision) is not int
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("Expected graph revision must be a non-negative integer")
        actual = self._current_revision(connection)
        if actual != expected_revision:
            raise CodeCortexError(
                ErrorCode.CACHE_REBUILD_REQUIRED,
                "Cognitive replica revision does not match the expected revision",
                retryable=True,
                details={
                    "expected_graph_revision": expected_revision,
                    "actual_graph_revision": actual,
                },
                suggested_action="Rebuild the cognitive query replica from formal state",
            )

    def _validated_entity_refs(
        self, graph: CognitiveGraph
    ) -> tuple[EntityRefRecord, ...]:
        records = tuple(self._entity_refs())
        by_uid: dict[str, EntityRefRecord] = {}
        for record in records:
            if not isinstance(record.uid, str) or not record.uid:
                raise ValueError("Entity reference records require a non-empty UID")
            if record.uid in by_uid:
                raise CodeCortexError(
                    ErrorCode.FORMAL_STATE_CORRUPT,
                    "Formal entity references contain a duplicate UID",
                    details={"entity_uid": record.uid},
                )
            by_uid[record.uid] = record
        missing = sorted(set(graph.entity_uids) - set(by_uid))
        if missing:
            raise CodeCortexError(
                ErrorCode.FORMAL_STATE_CORRUPT,
                "Formal entity references do not cover the graph's entity UIDs",
                details={"missing_entity_uids": missing},
            )
        return records

    def _validated_history_events(self) -> tuple[HistoryEventRecord, ...]:
        records = tuple(self._history_events())
        seen: set[str] = set()
        for record in records:
            if not record.event_id or not record.event_type or not record.relative_path:
                raise ValueError("History event records require identity fields")
            if (
                type(record.resulting_graph_revision) is not int
                or record.resulting_graph_revision < 0
            ):
                raise ValueError("History event revision must be a non-negative integer")
            if record.event_id in seen:
                raise CodeCortexError(
                    ErrorCode.FORMAL_STATE_CORRUPT,
                    "Formal history contains a duplicate event ID",
                    details={"event_id": record.event_id},
                )
            seen.add(record.event_id)
        return records

    def _import_nodes(self, connection: sqlite3.Connection, graph: CognitiveGraph) -> None:
        for node in graph.nodes:
            connection.execute(
                "INSERT INTO cognitive_nodes "
                "(node_id, kind, title, summary, epistemic_status, intent, observed, "
                "node_revision, approval_event_id, graph_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.id,
                    str(node.kind),
                    node.title,
                    node.summary or "",
                    str(node.epistemic_status),
                    node.intent,
                    node.observed or "",
                    node.node_revision,
                    _event_id(node.approval, node.id),
                    graph.graph_revision,
                ),
            )
            for position, alias in enumerate(node.aliases):
                connection.execute(
                    "INSERT INTO cognitive_aliases "
                    "(node_id, position, alias, normalized_alias) VALUES (?, ?, ?, ?)",
                    (node.id, position, alias, _normalize_alias(alias)),
                )

    def _import_edges(self, connection: sqlite3.Connection, graph: CognitiveGraph) -> None:
        for edge in graph.semantic_edges:
            connection.execute(
                "INSERT INTO cognitive_edges "
                "(edge_id, edge_type, source_node_id, target_node_id, "
                "epistemic_status, edge_revision, approval_event_id, graph_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    edge.id,
                    str(edge.type),
                    edge.source_id,
                    edge.target_id,
                    str(edge.epistemic_status),
                    edge.edge_revision,
                    _event_id(edge.approval, edge.id),
                    graph.graph_revision,
                ),
            )

    def _import_flows(self, connection: sqlite3.Connection, graph: CognitiveGraph) -> None:
        for flow in graph.logical_flows:
            connection.execute(
                "INSERT INTO logical_flows "
                "(behavior_id, materialization_status, flow_revision, "
                "approval_event_id, graph_revision) VALUES (?, ?, ?, ?, ?)",
                (
                    flow.behavior_id,
                    str(flow.materialization_status),
                    flow.flow_revision,
                    _event_id(flow.approval, flow.behavior_id),
                    graph.graph_revision,
                ),
            )
            for step in flow.steps:
                connection.execute(
                    "INSERT INTO flow_steps "
                    "(step_id, behavior_id, step_order, title, summary, "
                    "approval_event_id, graph_revision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        step.id,
                        flow.behavior_id,
                        step.order,
                        step.title,
                        step.summary or "",
                        _event_id(step.approval, step.id),
                        graph.graph_revision,
                    ),
                )
                for position, capability_id in enumerate(step.uses_capabilities):
                    connection.execute(
                        "INSERT INTO flow_step_capabilities "
                        "(step_id, capability_id, position) VALUES (?, ?, ?)",
                        (step.id, capability_id, position),
                    )

    def _import_entity_refs(
        self,
        connection: sqlite3.Connection,
        entity_refs: tuple[EntityRefRecord, ...],
        graph_revision: int,
    ) -> None:
        for record in entity_refs:
            connection.execute(
                "INSERT INTO entity_reference_index "
                "(entity_uid, last_known_address, kind, relative_path, signature, "
                "fingerprint, resolution_status, graph_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.uid,
                    record.last_known_address,
                    record.kind,
                    record.relative_path,
                    record.signature,
                    record.fingerprint,
                    record.resolution_status,
                    graph_revision,
                ),
            )

    def _import_mappings(
        self, connection: sqlite3.Connection, graph: CognitiveGraph
    ) -> None:
        for mapping in graph.implementation_mappings:
            connection.execute(
                "INSERT INTO implementation_mappings "
                "(mapping_id, subject_kind, subject_id, entity_uid, role, "
                "resolution_status, evidence_note, mapping_revision, "
                "approval_event_id, graph_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mapping.id,
                    str(mapping.subject_kind),
                    mapping.subject_id,
                    mapping.entity_uid,
                    str(mapping.role),
                    str(mapping.resolution_status),
                    mapping.evidence_note or "",
                    mapping.mapping_revision,
                    _event_id(mapping.approval, mapping.id),
                    graph.graph_revision,
                ),
            )

    def _import_evidence(
        self, connection: sqlite3.Connection, graph: CognitiveGraph
    ) -> None:
        owners: list[tuple[str, str, tuple[Evidence, ...]]] = []
        for node in graph.nodes:
            owners.append(("node", node.id, node.evidence))
        for edge in graph.semantic_edges:
            owners.append(("edge", edge.id, edge.evidence))
        for flow in graph.logical_flows:
            # Section 7 fixes four evidence owner kinds; a flow's primary key is
            # its behavior, so flow-level evidence attaches to that node.
            owners.append(("node", flow.behavior_id, flow.evidence))
            for step in flow.steps:
                owners.append(("flow_step", step.id, step.evidence))
        for mapping in graph.implementation_mappings:
            owners.append(("mapping", mapping.id, mapping.evidence))
        for owner_kind, owner_id, items in owners:
            for item in items:
                connection.execute(
                    "INSERT INTO cognitive_evidence "
                    "(evidence_id, owner_kind, owner_id, evidence_kind, entity_uid, "
                    "relative_path, start_line, end_line, observation, graph_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.id,
                        owner_kind,
                        owner_id,
                        item.kind,
                        item.entity_uid,
                        item.relative_path or "",
                        item.start_line,
                        item.end_line,
                        item.observation or "",
                        graph.graph_revision,
                    ),
                )

    def _import_history(
        self,
        connection: sqlite3.Connection,
        history_events: tuple[HistoryEventRecord, ...],
    ) -> None:
        for record in history_events:
            connection.execute(
                "INSERT INTO history_event_index "
                "(event_id, event_type, resulting_graph_revision, created_at, "
                "relative_path) VALUES (?, ?, ?, ?, ?)",
                (
                    record.event_id,
                    record.event_type,
                    record.resulting_graph_revision,
                    record.created_at,
                    record.relative_path,
                ),
            )

    def _import_search_terms(
        self, connection: sqlite3.Connection, graph: CognitiveGraph
    ) -> None:
        rows: set[tuple[str, str, str, int]] = set()
        for node in graph.nodes:
            for term in normalize_search_terms(node.title):
                rows.add((term, node.id, "title", _FIELD_WEIGHTS["title"]))
            for alias in node.aliases:
                normalized = _normalize_alias(alias)
                if normalized:
                    rows.add((normalized, node.id, "exact_alias", _FIELD_WEIGHTS["exact_alias"]))
                for term in normalize_search_terms(alias):
                    if term == normalized:
                        continue  # already indexed as the exact alias
                    rows.add((term, node.id, "alias", _FIELD_WEIGHTS["alias"]))
            if node.summary:
                for term in normalize_search_terms(node.summary):
                    rows.add((term, node.id, "summary", _FIELD_WEIGHTS["summary"]))
            if node.observed:
                for term in normalize_search_terms(node.observed):
                    rows.add((term, node.id, "observed", _FIELD_WEIGHTS["observed"]))
        connection.executemany(
            "INSERT INTO cognitive_search_terms (term, node_id, field, weight) "
            "VALUES (?, ?, ?, ?)",
            sorted(rows),
        )

    def _check_import_integrity(self, connection: sqlite3.Connection) -> None:
        """Enforce FK integrity and the polymorphic references SQLite cannot."""
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        dangling_mappings = connection.execute(_POLYMORPHIC_MAPPING_CHECK).fetchall()
        dangling_evidence = connection.execute(_POLYMORPHIC_EVIDENCE_CHECK).fetchall()
        if violations or dangling_mappings or dangling_evidence:
            raise CodeCortexError(
                ErrorCode.FORMAL_STATE_CORRUPT,
                "Cognitive graph import failed referential validation",
                details={
                    "foreign_key_violations": len(violations),
                    "dangling_mapping_subjects": sorted(
                        row[0] for row in dangling_mappings
                    ),
                    "dangling_evidence_owners": sorted(
                        row[0] for row in dangling_evidence
                    ),
                },
            )

    def search(
        self,
        query: str,
        kinds: Sequence[str],
        limit: int,
        expected_revision: int,
    ) -> tuple[GraphHit, ...]:
        """Return deterministic weighted hits, bounded by *limit*.

        Recall is by normalized term; ranking accumulates the fixed field
        weights and breaks ties by kind, then by stable node ID.  A revision
        mismatch raises ``CACHE_REBUILD_REQUIRED`` instead of returning data.
        """

        if type(limit) is not int or isinstance(limit, bool) or limit <= 0:
            raise ValueError("Search limit must be positive")
        if limit > self.max_limit:
            raise ValueError("Search limit exceeds the configured maximum")
        kind_filter = tuple(kinds)
        if any(kind not in _NODE_KINDS for kind in kind_filter):
            raise ValueError("Search kind filter contains an unknown node kind")
        terms = _query_terms(query)
        with self.open_read() as connection:
            self._require_revision(connection, expected_revision)
            if not terms:
                return ()
            term_placeholders, term_values = _in_clause(terms)
            statement = (
                "SELECT t.node_id AS node_id, n.kind AS kind, n.title AS title, "
                "SUM(t.weight) AS score "
                "FROM cognitive_search_terms AS t "
                "JOIN cognitive_nodes AS n ON n.node_id = t.node_id "
                f"WHERE t.term IN ({term_placeholders})"
            )
            parameters: tuple[object, ...] = term_values
            if kind_filter:
                kind_placeholders, kind_values = _in_clause(kind_filter)
                statement += f" AND n.kind IN ({kind_placeholders})"
                parameters = (*parameters, *kind_values)
            statement += (
                " GROUP BY t.node_id"
                " ORDER BY score DESC, n.kind ASC, t.node_id ASC LIMIT ?"
            )
            rows = connection.execute(statement, (*parameters, limit)).fetchall()
            if not rows:
                return ()
            hit_ids = tuple(row["node_id"] for row in rows)
            field_placeholders, field_values = _in_clause(hit_ids)
            field_rows = connection.execute(
                "SELECT node_id, field, MAX(weight) AS weight "
                "FROM cognitive_search_terms "
                f"WHERE node_id IN ({field_placeholders}) "
                f"AND term IN ({term_placeholders}) "
                "GROUP BY node_id, field",
                (*field_values, *term_values),
            ).fetchall()
        fields_by_node: dict[str, list[tuple[int, str]]] = {}
        for field_row in field_rows:
            fields_by_node.setdefault(field_row["node_id"], []).append(
                (field_row["weight"], field_row["field"])
            )
        return tuple(
            GraphHit(
                node_id=row["node_id"],
                kind=row["kind"],
                title=row["title"],
                score=row["score"],
                matched_fields=tuple(
                    field
                    for _, field in sorted(
                        fields_by_node.get(row["node_id"], []),
                        key=lambda item: (-item[0], item[1]),
                    )
                ),
            )
            for row in rows
        )

    def node_kind_counts(self, expected_revision: int) -> dict[str, int]:
        """Count indexed nodes per kind at exactly *expected_revision*."""
        with self.open_read() as connection:
            self._require_revision(connection, expected_revision)
            rows = connection.execute(
                "SELECT kind, COUNT(*) AS node_count FROM cognitive_nodes "
                "GROUP BY kind"
            ).fetchall()
        return {row["kind"]: row["node_count"] for row in rows}

    def entity_refs_for(
        self, entity_uids: Sequence[str], expected_revision: int
    ) -> tuple[ContextEntity, ...]:
        """Return formal entity-reference rows for *entity_uids* in one query."""
        uids = _validated_anchors(entity_uids, "Entity UID")
        if not uids:
            return ()
        if len(uids) > self.max_anchors:
            raise ValueError("Entity UID list exceeds the anchor maximum")
        with self.open_read() as connection:
            self._require_revision(connection, expected_revision)
            placeholders, values = _in_clause(uids)
            rows = connection.execute(
                "SELECT entity_uid, last_known_address, kind, relative_path, "
                "signature, fingerprint, resolution_status "
                "FROM entity_reference_index "
                f"WHERE entity_uid IN ({placeholders}) "
                "ORDER BY entity_uid ASC",
                values,
            ).fetchall()
        return tuple(
            ContextEntity(
                entity_uid=row["entity_uid"],
                last_known_address=row["last_known_address"],
                kind=row["kind"],
                relative_path=row["relative_path"],
                signature=row["signature"],
                fingerprint=row["fingerprint"],
                resolution_status=row["resolution_status"],
            )
            for row in rows
        )

    def mappings_for_entities(
        self, entity_uids: Sequence[str], limit: int, expected_revision: int
    ) -> tuple[tuple[ContextMapping, ...], bool]:
        """Return bounded implementation mappings for *entity_uids*.

        The boolean reports whether more mappings exist beyond *limit*.
        """
        uids = _validated_anchors(entity_uids, "Entity UID")
        checked_limit = _bounded_int(limit, 1, self.max_context_entities, "limit")
        if not uids:
            return (), False
        if len(uids) > self.max_anchors:
            raise ValueError("Entity UID list exceeds the anchor maximum")
        with self.open_read() as connection:
            self._require_revision(connection, expected_revision)
            placeholders, values = _in_clause(uids)
            rows = connection.execute(
                "SELECT mapping_id, subject_kind, subject_id, entity_uid, role, "
                "resolution_status, evidence_note "
                "FROM implementation_mappings "
                f"WHERE entity_uid IN ({placeholders}) "
                "ORDER BY mapping_id ASC LIMIT ?",
                (*values, checked_limit + 1),
            ).fetchall()
        selected = rows[:checked_limit]
        return tuple(
            ContextMapping(
                mapping_id=row["mapping_id"],
                subject_kind=row["subject_kind"],
                subject_id=row["subject_id"],
                entity_uid=row["entity_uid"],
                role=row["role"],
                resolution_status=row["resolution_status"],
                evidence_note=row["evidence_note"],
            )
            for row in selected
        ), len(rows) > checked_limit

    def context(self, request: ContextRequest) -> DiscussionContext:
        """Traverse a bounded neighborhood and materialize it as DTOs.

        Depth and the node/entity/evidence maxima are applied during
        traversal, before any DTO is materialized; every step uses a fixed
        number of batch queries regardless of the result size.
        """

        node_anchors = _validated_anchors(request.node_ids, "Node anchor")
        entity_anchors = _validated_anchors(request.entity_uids, "Entity anchor")
        if not node_anchors and not entity_anchors:
            raise ValueError("Context request requires at least one anchor")
        if len(node_anchors) + len(entity_anchors) > self.max_anchors:
            raise ValueError("Context request exceeds the anchor maximum")
        depth = _bounded_int(request.depth, 0, self.max_depth, "depth")
        max_nodes = _bounded_int(request.max_nodes, 1, self.max_context_nodes, "max_nodes")
        max_entities = _bounded_int(
            request.max_entities, 1, self.max_context_entities, "max_entities"
        )
        max_evidence = _bounded_int(
            request.max_evidence, 1, self.max_context_evidence, "max_evidence"
        )

        with self.open_read() as connection:
            actual_revision = self._current_revision(connection)
            if request.expected_graph_revision is not None:
                self._require_revision(connection, request.expected_graph_revision)
            anchors = self._resolve_anchors(connection, node_anchors, entity_anchors)
            included, cut = self._traverse(connection, anchors, depth, max_nodes)
            bundle = self._materialize(connection, included, max_entities, max_evidence)
        truncated = bool(cut) or bundle.truncated
        continuation = _encode_continuation(cut) if cut else None
        return DiscussionContext(
            graph_revision=actual_revision,
            nodes=bundle.nodes,
            edges=bundle.edges,
            flows=bundle.flows,
            mappings=bundle.mappings,
            entities=bundle.entities,
            evidence=bundle.evidence,
            truncated=truncated,
            continuation=continuation,
        )

    def _resolve_anchors(
        self,
        connection: sqlite3.Connection,
        node_ids: tuple[str, ...],
        entity_uids: tuple[str, ...],
    ) -> list[str]:
        anchors: set[str] = set(node_ids)
        if node_ids:
            placeholders, values = _in_clause(node_ids)
            found = {
                row[0]
                for row in connection.execute(
                    f"SELECT node_id FROM cognitive_nodes WHERE node_id IN ({placeholders})",
                    values,
                )
            }
            unknown = sorted(set(node_ids) - found)
            if unknown:
                raise CodeCortexError(
                    ErrorCode.INVALID_ID,
                    "Context anchor node does not exist",
                    details={"node_ids": unknown},
                    suggested_action="Search the cognitive graph for a valid node ID",
                )
        if entity_uids:
            placeholders, values = _in_clause(entity_uids)
            found = {
                row[0]
                for row in connection.execute(
                    "SELECT entity_uid FROM entity_reference_index "
                    f"WHERE entity_uid IN ({placeholders})",
                    values,
                )
            }
            unknown = sorted(set(entity_uids) - found)
            if unknown:
                raise CodeCortexError(
                    ErrorCode.INVALID_ID,
                    "Context anchor entity does not exist",
                    details={"entity_uids": unknown},
                    suggested_action="Resolve the entity through repository facts first",
                )
            mapping_rows = connection.execute(
                "SELECT subject_kind, subject_id FROM implementation_mappings "
                f"WHERE entity_uid IN ({placeholders})",
                values,
            ).fetchall()
            evidence_rows = connection.execute(
                "SELECT owner_kind, owner_id FROM cognitive_evidence "
                f"WHERE entity_uid IN ({placeholders})",
                values,
            ).fetchall()
            step_ids = {
                row["subject_id"]
                for row in mapping_rows
                if row["subject_kind"] == "flow_step"
            } | {
                row["owner_id"]
                for row in evidence_rows
                if row["owner_kind"] == "flow_step"
            }
            anchors.update(
                row["subject_id"]
                for row in mapping_rows
                if row["subject_kind"] == "node"
            )
            anchors.update(
                row["owner_id"]
                for row in evidence_rows
                if row["owner_kind"] == "node"
            )
            mapping_ids = tuple(
                row["owner_id"]
                for row in evidence_rows
                if row["owner_kind"] == "mapping"
            )
            mapping_step_ids: set[str] = set()
            if mapping_ids:
                map_placeholders, map_values = _in_clause(mapping_ids)
                subject_rows = connection.execute(
                    "SELECT subject_kind, subject_id FROM implementation_mappings "
                    f"WHERE mapping_id IN ({map_placeholders})",
                    map_values,
                ).fetchall()
                anchors.update(
                    row["subject_id"]
                    for row in subject_rows
                    if row["subject_kind"] == "node"
                )
                mapping_step_ids = {
                    row["subject_id"]
                    for row in subject_rows
                    if row["subject_kind"] == "flow_step"
                }
            step_ids |= mapping_step_ids
            if step_ids:
                step_placeholders, step_values = _in_clause(sorted(step_ids))
                anchors.update(
                    row["behavior_id"]
                    for row in connection.execute(
                        "SELECT step_id, behavior_id FROM flow_steps "
                        f"WHERE step_id IN ({step_placeholders})",
                        step_values,
                    )
                )
            edge_ids = tuple(
                row["owner_id"]
                for row in evidence_rows
                if row["owner_kind"] == "edge"
            )
            if edge_ids:
                edge_placeholders, edge_values = _in_clause(edge_ids)
                for row in connection.execute(
                    "SELECT source_node_id, target_node_id FROM cognitive_edges "
                    f"WHERE edge_id IN ({edge_placeholders})",
                    edge_values,
                ):
                    anchors.add(row["source_node_id"])
                    anchors.add(row["target_node_id"])
        return sorted(anchors)

    def _traverse(
        self,
        connection: sqlite3.Connection,
        anchors: list[str],
        depth: int,
        max_nodes: int,
    ) -> tuple[list[str], list[str]]:
        """Breadth-first expansion applying depth before the node maximum."""

        included: dict[str, None] = {}
        cut: list[str] = []
        frontier: list[str] = []
        for node_id in anchors:
            if len(included) >= max_nodes:
                cut.append(node_id)
            else:
                included[node_id] = None
                frontier.append(node_id)
        for _level in range(depth):
            if not frontier:
                break
            placeholders, values = _in_clause(frontier)
            rows = connection.execute(
                "SELECT source_node_id, target_node_id FROM cognitive_edges "
                f"WHERE source_node_id IN ({placeholders}) "
                f"OR target_node_id IN ({placeholders})",
                (*values, *values),
            ).fetchall()
            neighbors = sorted(
                {
                    neighbor
                    for row in rows
                    for neighbor in (row["source_node_id"], row["target_node_id"])
                }
                - set(included)
            )
            frontier = []
            for neighbor in neighbors:
                if len(included) >= max_nodes:
                    cut.append(neighbor)
                else:
                    included[neighbor] = None
                    frontier.append(neighbor)
        return list(included), sorted(set(cut))

    def _materialize(
        self,
        connection: sqlite3.Connection,
        included: list[str],
        max_entities: int,
        max_evidence: int,
    ) -> _ContextBundle:
        """Materialize the bounded neighborhood with batch queries only."""

        node_placeholders, node_values = _in_clause(included)
        node_rows = connection.execute(
            "SELECT node_id, kind, title, summary, intent, observed, "
            "epistemic_status, node_revision FROM cognitive_nodes "
            f"WHERE node_id IN ({node_placeholders}) ORDER BY node_id",
            node_values,
        ).fetchall()
        alias_rows = connection.execute(
            "SELECT node_id, alias FROM cognitive_aliases "
            f"WHERE node_id IN ({node_placeholders}) ORDER BY node_id, position",
            node_values,
        ).fetchall()
        edge_rows = connection.execute(
            "SELECT edge_id, edge_type, source_node_id, target_node_id, "
            "epistemic_status FROM cognitive_edges "
            f"WHERE source_node_id IN ({node_placeholders}) "
            f"AND target_node_id IN ({node_placeholders}) ORDER BY edge_id",
            (*node_values, *node_values),
        ).fetchall()

        behavior_ids = tuple(row["node_id"] for row in node_rows if row["kind"] == "behavior")
        flow_rows: list[sqlite3.Row] = []
        step_rows: list[sqlite3.Row] = []
        capability_rows: list[sqlite3.Row] = []
        if behavior_ids:
            behavior_placeholders, behavior_values = _in_clause(behavior_ids)
            flow_rows = connection.execute(
                "SELECT behavior_id, materialization_status FROM logical_flows "
                f"WHERE behavior_id IN ({behavior_placeholders}) ORDER BY behavior_id",
                behavior_values,
            ).fetchall()
            step_rows = connection.execute(
                "SELECT step_id, behavior_id, step_order, title, summary "
                "FROM flow_steps "
                f"WHERE behavior_id IN ({behavior_placeholders}) "
                "ORDER BY behavior_id, step_order",
                behavior_values,
            ).fetchall()
            step_ids = tuple(row["step_id"] for row in step_rows)
            if step_ids:
                step_placeholders, step_values = _in_clause(step_ids)
                capability_rows = connection.execute(
                    "SELECT step_id, capability_id FROM flow_step_capabilities "
                    f"WHERE step_id IN ({step_placeholders}) "
                    "ORDER BY step_id, position",
                    step_values,
                ).fetchall()

        step_ids = tuple(row["step_id"] for row in step_rows)
        step_placeholders, step_values = _in_clause(step_ids)
        mapping_rows = connection.execute(
            "SELECT mapping_id, subject_kind, subject_id, entity_uid, role, "
            "resolution_status, evidence_note FROM implementation_mappings "
            f"WHERE (subject_kind = 'node' AND subject_id IN ({node_placeholders})) "
            + (
                f"OR (subject_kind = 'flow_step' AND subject_id IN ({step_placeholders})) "
                if step_ids
                else ""
            )
            + "ORDER BY mapping_id",
            (*node_values, *step_values) if step_ids else node_values,
        ).fetchall()

        edge_ids = tuple(row["edge_id"] for row in edge_rows)
        mapping_ids = tuple(row["mapping_id"] for row in mapping_rows)
        evidence_sql = (
            "SELECT evidence_id, owner_kind, owner_id, evidence_kind, entity_uid, "
            "relative_path, start_line, end_line, observation FROM cognitive_evidence "
            f"WHERE (owner_kind = 'node' AND owner_id IN ({node_placeholders})) "
        )
        evidence_params: tuple[object, ...] = node_values
        for owner_kind, ids in (
            ("edge", edge_ids),
            ("flow_step", step_ids),
            ("mapping", mapping_ids),
        ):
            if ids:
                placeholders, values = _in_clause(ids)
                evidence_sql += (
                    f"OR (owner_kind = '{owner_kind}' AND owner_id IN ({placeholders})) "
                )
                evidence_params = (*evidence_params, *values)
        evidence_sql += "ORDER BY evidence_id LIMIT ?"
        evidence_rows = connection.execute(
            evidence_sql, (*evidence_params, max_evidence + 1)
        ).fetchall()
        truncated = len(evidence_rows) > max_evidence
        evidence_rows = evidence_rows[:max_evidence]

        entity_uids = sorted(
            {row["entity_uid"] for row in mapping_rows}
            | {
                row["entity_uid"]
                for row in evidence_rows
                if row["entity_uid"] is not None
            }
        )
        if len(entity_uids) > max_entities:
            truncated = True
            entity_uids = entity_uids[:max_entities]
        entity_rows: list[sqlite3.Row] = []
        if entity_uids:
            entity_placeholders, entity_values = _in_clause(entity_uids)
            entity_rows = connection.execute(
                "SELECT entity_uid, last_known_address, kind, relative_path, "
                "signature, fingerprint, resolution_status FROM entity_reference_index "
                f"WHERE entity_uid IN ({entity_placeholders}) ORDER BY entity_uid",
                entity_values,
            ).fetchall()

        aliases_by_node: dict[str, list[str]] = {}
        for row in alias_rows:
            aliases_by_node.setdefault(row["node_id"], []).append(row["alias"])
        capabilities_by_step: dict[str, list[str]] = {}
        for row in capability_rows:
            capabilities_by_step.setdefault(row["step_id"], []).append(
                row["capability_id"]
            )
        steps_by_behavior: dict[str, list[ContextStep]] = {}
        for row in step_rows:
            steps_by_behavior.setdefault(row["behavior_id"], []).append(
                ContextStep(
                    step_id=row["step_id"],
                    order=row["step_order"],
                    title=row["title"],
                    summary=row["summary"],
                    uses_capabilities=tuple(
                        capabilities_by_step.get(row["step_id"], ())
                    ),
                )
            )
        return _ContextBundle(
            truncated=truncated,
            nodes=tuple(
                ContextNode(
                    node_id=row["node_id"],
                    kind=row["kind"],
                    title=row["title"],
                    aliases=tuple(aliases_by_node.get(row["node_id"], ())),
                    summary=row["summary"],
                    intent=row["intent"],
                    observed=row["observed"],
                    epistemic_status=row["epistemic_status"],
                    node_revision=row["node_revision"],
                )
                for row in node_rows
            ),
            edges=tuple(
                ContextEdge(
                    edge_id=row["edge_id"],
                    edge_type=row["edge_type"],
                    source_node_id=row["source_node_id"],
                    target_node_id=row["target_node_id"],
                    epistemic_status=row["epistemic_status"],
                )
                for row in edge_rows
            ),
            flows=tuple(
                ContextFlow(
                    behavior_id=row["behavior_id"],
                    materialization_status=row["materialization_status"],
                    steps=tuple(steps_by_behavior.get(row["behavior_id"], ())),
                )
                for row in flow_rows
            ),
            mappings=tuple(
                ContextMapping(
                    mapping_id=row["mapping_id"],
                    subject_kind=row["subject_kind"],
                    subject_id=row["subject_id"],
                    entity_uid=row["entity_uid"],
                    role=row["role"],
                    resolution_status=row["resolution_status"],
                    evidence_note=row["evidence_note"],
                )
                for row in mapping_rows
            ),
            entities=tuple(
                ContextEntity(
                    entity_uid=row["entity_uid"],
                    last_known_address=row["last_known_address"],
                    kind=row["kind"],
                    relative_path=row["relative_path"],
                    signature=row["signature"],
                    fingerprint=row["fingerprint"],
                    resolution_status=row["resolution_status"],
                )
                for row in entity_rows
            ),
            evidence=tuple(
                ContextEvidence(
                    evidence_id=row["evidence_id"],
                    owner_kind=row["owner_kind"],
                    owner_id=row["owner_id"],
                    evidence_kind=row["evidence_kind"],
                    entity_uid=row["entity_uid"],
                    relative_path=row["relative_path"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    observation=row["observation"],
                )
                for row in evidence_rows
            ),
        )


def _validated_anchors(values: Sequence[str], label: str) -> tuple[str, ...]:
    materialized = tuple(dict.fromkeys(values))
    if any(not isinstance(value, str) or not value for value in materialized):
        raise ValueError(f"{label} list must contain non-empty strings")
    return materialized


def _bounded_int(value: int, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(f"Context {label} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"Context {label} must be between {minimum} and {maximum}")
    return value


def _is_corrupt_sqlite(error: sqlite3.DatabaseError) -> bool:
    """Whether SQLite identified the file itself as unreadable, not merely busy."""
    message = str(error).lower()
    return "file is not a database" in message or "database disk image is malformed" in message
