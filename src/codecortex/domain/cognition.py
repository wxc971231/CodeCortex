"""Pure domain models and invariants for CodeCortex formal cognition state."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

type JsonObject = dict[str, object]

SCHEMA_VERSION = 1
DIGEST_PROFILE_VERSION = 1
MANAGED_SOURCE_SET_VERSION = 1
MINIMUM_CORE_VERSION = "0.1.0"

_DIGEST_PREFIX = "sha256:"
_NODE_PREFIX_BY_KIND = {
    "responsibility": "responsibility.",
    "behavior": "behavior.",
    "capability": "capability.",
}
_ACTOR_VALUES = {"analyzer", "main_codex", "user"}
_EPISTEMIC_VALUES = {"established", "inferred", "uncertain"}


@dataclass(frozen=True)
class Manifest:
    """Commit marker and compatibility metadata for one formal graph revision."""

    schema_version: int
    graph_revision: int
    cognition_initialized: bool
    cognition_baseline: str | None
    minimum_core_version: str = MINIMUM_CORE_VERSION
    digest_profile_version: int = DIGEST_PROFILE_VERSION
    managed_source_set_version: int = MANAGED_SOURCE_SET_VERSION


@dataclass(frozen=True)
class CognitiveGraph:
    """The canonical semantic graph, independent of its storage representation."""

    schema_version: int
    graph_revision: int
    nodes: tuple[JsonObject, ...]
    semantic_edges: tuple[JsonObject, ...]
    logical_flows: tuple[JsonObject, ...]
    implementation_mappings: tuple[JsonObject, ...]

    @classmethod
    def empty(cls) -> CognitiveGraph:
        return cls(SCHEMA_VERSION, 0, (), (), (), ())


@dataclass(frozen=True)
class EntityRefs:
    """Stable code-entity identities referenced by the formal graph."""

    schema_version: int
    graph_revision: int
    entities: tuple[JsonObject, ...]

    @classmethod
    def empty(cls) -> EntityRefs:
        return cls(SCHEMA_VERSION, 0, ())


@dataclass(frozen=True)
class SourceBaseline:
    """Accepted managed-source digests corresponding to cognition baseline."""

    schema_version: int
    digest_profile_version: int
    managed_source_set_version: int
    repository_source_digest: str | None
    files: tuple[JsonObject, ...]

    @classmethod
    def empty(cls) -> SourceBaseline:
        return cls(
            SCHEMA_VERSION,
            DIGEST_PROFILE_VERSION,
            MANAGED_SOURCE_SET_VERSION,
            None,
            (),
        )


@dataclass(frozen=True)
class ViewManifest:
    """Git-portable byte digests for Core-managed rendered views."""

    schema_version: int
    graph_revision: int
    files: tuple[JsonObject, ...]


@dataclass(frozen=True)
class HistoryEventRef:
    """The identity and type needed to validate formal provenance references."""

    event_id: str
    event_type: str


@dataclass(frozen=True)
class FormalState:
    """One complete in-memory formal-state snapshot."""

    manifest: Manifest
    graph: CognitiveGraph
    entity_refs: EntityRefs
    source_baseline: SourceBaseline
    history_events: tuple[HistoryEventRef, ...] = ()
    view_manifest: ViewManifest | None = None

    @classmethod
    def empty(
        cls, *, manifest: Manifest, source_baseline: SourceBaseline
    ) -> FormalState:
        return cls(
            manifest=manifest,
            graph=CognitiveGraph.empty(),
            entity_refs=EntityRefs.empty(),
            source_baseline=source_baseline,
        )


class ValidationIssueCode(StrEnum):
    """Stable machine-readable formal-state invariant failures."""

    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    INVALID_REVISION = "INVALID_REVISION"
    GRAPH_REVISION_MISMATCH = "GRAPH_REVISION_MISMATCH"
    UNINITIALIZED_BASELINE_NOT_EMPTY = "UNINITIALIZED_BASELINE_NOT_EMPTY"
    INITIALIZED_BASELINE_EMPTY = "INITIALIZED_BASELINE_EMPTY"
    BASELINE_DIGEST_MISMATCH = "BASELINE_DIGEST_MISMATCH"
    INVALID_DIGEST = "INVALID_DIGEST"
    INVALID_RELATIVE_PATH = "INVALID_RELATIVE_PATH"
    INVALID_ID_NAMESPACE = "INVALID_ID_NAMESPACE"
    DUPLICATE_ID = "DUPLICATE_ID"
    INVALID_NODE = "INVALID_NODE"
    INVALID_NODE_REVISION = "INVALID_NODE_REVISION"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    DANGLING_APPROVAL_EVENT = "DANGLING_APPROVAL_EVENT"
    DANGLING_REFERENCE = "DANGLING_REFERENCE"
    INVALID_RELATION = "INVALID_RELATION"
    BEHAVIOR_PARENT_COUNT = "BEHAVIOR_PARENT_COUNT"
    CONTAINS_CYCLE = "CONTAINS_CYCLE"
    INVALID_FLOW_STATUS = "INVALID_FLOW_STATUS"
    UNMATERIALIZED_FLOW_HAS_STEPS = "UNMATERIALIZED_FLOW_HAS_STEPS"
    NOT_APPLICABLE_FLOW_HAS_STEPS = "NOT_APPLICABLE_FLOW_HAS_STEPS"
    MATERIALIZED_FLOW_EMPTY = "MATERIALIZED_FLOW_EMPTY"
    FLOW_ORDER_NOT_CONTIGUOUS = "FLOW_ORDER_NOT_CONTIGUOUS"
    INVALID_MAPPING_ROLE = "INVALID_MAPPING_ROLE"
    INVALID_MAPPING_RESOLUTION_STATUS = "INVALID_MAPPING_RESOLUTION_STATUS"
    RELATION_EVIDENCE_REQUIRED = "RELATION_EVIDENCE_REQUIRED"


@dataclass(frozen=True)
class ValidationIssue:
    code: ValidationIssueCode
    location: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...]


def validate_formal_state(state: FormalState) -> ValidationResult:
    """Validate one complete snapshot without accessing filesystem or adapters."""
    issues: list[ValidationIssue] = []
    _validate_versions(state, issues)
    _validate_revisions(state, issues)
    _validate_baseline(state, issues)
    applied_event_ids = _validate_history(state.history_events, issues)
    entity_ids = _validate_entity_refs(state.entity_refs, issues)
    _validate_graph(
        state.graph,
        applied_event_ids,
        entity_ids,
        issues,
        strict_m1a=state.manifest.cognition_initialized,
    )
    return ValidationResult(valid=not issues, issues=tuple(issues))


def _issue(
    issues: list[ValidationIssue],
    code: ValidationIssueCode,
    location: str,
    message: str,
) -> None:
    issues.append(ValidationIssue(code, location, message))


def _validate_versions(state: FormalState, issues: list[ValidationIssue]) -> None:
    for location, version in (
        ("manifest.schema_version", state.manifest.schema_version),
        ("graph.schema_version", state.graph.schema_version),
        ("entity_refs.schema_version", state.entity_refs.schema_version),
        ("source_baseline.schema_version", state.source_baseline.schema_version),
    ):
        if version != SCHEMA_VERSION:
            _issue(
                issues,
                ValidationIssueCode.UNSUPPORTED_SCHEMA,
                location,
                f"schema version {version} is unsupported",
            )
    if state.manifest.digest_profile_version != DIGEST_PROFILE_VERSION:
        _issue(
            issues,
            ValidationIssueCode.UNSUPPORTED_SCHEMA,
            "manifest.digest_profile_version",
            "digest profile version is unsupported",
        )
    if (
        state.source_baseline.digest_profile_version
        != state.manifest.digest_profile_version
    ):
        _issue(
            issues,
            ValidationIssueCode.UNSUPPORTED_SCHEMA,
            "source_baseline.digest_profile_version",
            "digest profile version differs from manifest",
        )
    if state.manifest.managed_source_set_version != MANAGED_SOURCE_SET_VERSION:
        _issue(
            issues,
            ValidationIssueCode.UNSUPPORTED_SCHEMA,
            "manifest.managed_source_set_version",
            "managed source set version is unsupported",
        )
    if (
        state.source_baseline.managed_source_set_version
        != state.manifest.managed_source_set_version
    ):
        _issue(
            issues,
            ValidationIssueCode.UNSUPPORTED_SCHEMA,
            "source_baseline.managed_source_set_version",
            "managed source set version differs from manifest",
        )
    if state.manifest.minimum_core_version != MINIMUM_CORE_VERSION:
        _issue(
            issues,
            ValidationIssueCode.UNSUPPORTED_SCHEMA,
            "manifest.minimum_core_version",
            "minimum Core version is unsupported",
        )


def _validate_revisions(state: FormalState, issues: list[ValidationIssue]) -> None:
    revisions = (
        state.manifest.graph_revision,
        state.graph.graph_revision,
        state.entity_refs.graph_revision,
    )
    if any(type(revision) is not int or revision < 0 for revision in revisions):
        _issue(
            issues,
            ValidationIssueCode.INVALID_REVISION,
            "graph_revision",
            "graph revisions must be non-negative integers",
        )
    if len(set(revisions)) != 1:
        _issue(
            issues,
            ValidationIssueCode.GRAPH_REVISION_MISMATCH,
            "graph_revision",
            "manifest, graph, and entity refs revisions differ",
        )
    if state.graph.graph_revision == 0 and any(
        (
            state.graph.nodes,
            state.graph.semantic_edges,
            state.graph.logical_flows,
            state.graph.implementation_mappings,
            state.entity_refs.entities,
            state.history_events,
        )
    ):
        _issue(
            issues,
            ValidationIssueCode.INVALID_REVISION,
            "graph_revision",
            "revision zero must be an empty formal graph",
        )


def _validate_baseline(state: FormalState, issues: list[ValidationIssue]) -> None:
    manifest_digest = state.manifest.cognition_baseline
    baseline_digest = state.source_baseline.repository_source_digest
    files = state.source_baseline.files
    if not state.manifest.cognition_initialized:
        if manifest_digest is not None or baseline_digest is not None or files:
            _issue(
                issues,
                ValidationIssueCode.UNINITIALIZED_BASELINE_NOT_EMPTY,
                "source_baseline",
                "uninitialized cognition requires null digests and no baseline files",
            )
    elif manifest_digest is None or baseline_digest is None or not files:
        _issue(
            issues,
            ValidationIssueCode.INITIALIZED_BASELINE_EMPTY,
            "source_baseline",
            "initialized cognition requires non-empty baseline state",
        )
    if manifest_digest != baseline_digest:
        _issue(
            issues,
            ValidationIssueCode.BASELINE_DIGEST_MISMATCH,
            "source_baseline.repository_source_digest",
            "source baseline digest differs from manifest cognition baseline",
        )
    for location, value in (
        ("manifest.cognition_baseline", manifest_digest),
        ("source_baseline.repository_source_digest", baseline_digest),
    ):
        if value is not None and not _is_digest(value):
            _issue(
                issues,
                ValidationIssueCode.INVALID_DIGEST,
                location,
                "digest must use sha256 followed by 64 lowercase hexadecimal digits",
            )
    paths: list[str] = []
    for index, file_record in enumerate(files):
        location = f"source_baseline.files[{index}]"
        path = file_record.get("relative_path")
        digest = file_record.get("content_digest")
        if not isinstance(path, str) or not _is_relative_path(path):
            _issue(
                issues,
                ValidationIssueCode.INVALID_RELATIVE_PATH,
                f"{location}.relative_path",
                "source paths must be normalized repository-relative POSIX paths",
            )
        else:
            paths.append(path)
        if not isinstance(digest, str) or not _is_digest(digest):
            _issue(
                issues,
                ValidationIssueCode.INVALID_DIGEST,
                f"{location}.content_digest",
                "content digest is invalid",
            )
    if paths != sorted(set(paths)):
        _issue(
            issues,
            ValidationIssueCode.INVALID_RELATIVE_PATH,
            "source_baseline.files",
            "source baseline paths must be unique and sorted",
        )


def _validate_history(
    events: tuple[HistoryEventRef, ...], issues: list[ValidationIssue]
) -> set[str]:
    applied_event_ids: set[str] = set()
    all_ids: set[str] = set()
    for index, event in enumerate(events):
        location = f"history_events[{index}].event_id"
        if not _is_ulid_id(event.event_id, "evt"):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                location,
                "history event ID must use the evt_ namespace",
            )
        if event.event_id in all_ids:
            _issue(
                issues,
                ValidationIssueCode.DUPLICATE_ID,
                location,
                "history event IDs must be unique",
            )
        all_ids.add(event.event_id)
        if event.event_type == "cognitive_proposal_applied":
            applied_event_ids.add(event.event_id)
    return applied_event_ids


def _validate_graph(
    graph: CognitiveGraph,
    applied_event_ids: set[str],
    entity_ids: set[str],
    issues: list[ValidationIssue],
    *,
    strict_m1a: bool,
) -> None:
    identifiers: set[str] = set()
    evidence_ids: set[str] = set()
    node_ids: set[str] = set()
    node_kinds: dict[str, str] = {}
    for index, node in enumerate(graph.nodes):
        location = f"graph.nodes[{index}]"
        identifier = node.get("id")
        kind = node.get("kind")
        if (
            not isinstance(identifier, str)
            or not isinstance(kind, str)
            or kind not in _NODE_PREFIX_BY_KIND
            or not _is_semantic_id(identifier, _NODE_PREFIX_BY_KIND[kind])
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.id",
                "node ID namespace must match its kind",
            )
        else:
            node_ids.add(identifier)
            node_kinds[identifier] = kind
        _validate_unique(identifier, identifiers, location, issues)
        node_revision = node.get("node_revision")
        if (
            type(node_revision) is not int
            or node_revision < 1
            or node_revision > graph.graph_revision
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_NODE_REVISION,
                f"{location}.node_revision",
                "node revision must be between one and graph revision",
            )
        _validate_optional_node_fields(node, location, issues)
        _validate_provenance(node, location, applied_event_ids, issues)
        _validate_evidence(node, location, evidence_ids, issues)
        _validate_repository_relative_fields(node, location, issues)

    flow_step_ids: set[str] = set()
    for index, flow in enumerate(graph.logical_flows):
        location = f"graph.logical_flows[{index}]"
        behavior_id = flow.get("behavior_id")
        if not isinstance(behavior_id, str) or not _is_semantic_id(
            behavior_id, "behavior."
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.behavior_id",
                "logical flow owner must use the behavior namespace",
            )
        elif behavior_id not in node_ids:
            _issue(
                issues,
                ValidationIssueCode.DANGLING_REFERENCE,
                f"{location}.behavior_id",
                "logical flow owner does not resolve to a graph node",
            )
        elif node_kinds.get(behavior_id) != "behavior":
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.behavior_id",
                "logical flow owner must resolve to a behavior node",
            )
        _validate_record_revision(
            flow, "flow_revision", location, graph.graph_revision, issues
        )
        _validate_provenance(flow, location, applied_event_ids, issues)
        _validate_evidence(flow, location, evidence_ids, issues)
        _validate_repository_relative_fields(flow, location, issues)
        steps = flow.get("steps", [])
        if not isinstance(steps, list):
            _issue(
                issues,
                ValidationIssueCode.INVALID_NODE,
                f"{location}.steps",
                "logical flow steps must be a list",
            )
            continue
        for step_index, step in enumerate(steps):
            step_location = f"{location}.steps[{step_index}]"
            step_id = step.get("id") if isinstance(step, dict) else None
            if (
                not isinstance(step_id, str)
                or not isinstance(behavior_id, str)
                or not step_id.startswith(f"{behavior_id}#step.")
                or not _valid_slug(step_id.partition("#step.")[2])
            ):
                _issue(
                    issues,
                    ValidationIssueCode.INVALID_ID_NAMESPACE,
                    f"{step_location}.id",
                    "flow step ID must belong to its behavior",
                )
            elif step_id in flow_step_ids:
                _issue(
                    issues,
                    ValidationIssueCode.DUPLICATE_ID,
                    f"{step_location}.id",
                    "flow step IDs must be unique",
                )
            else:
                flow_step_ids.add(step_id)
            if isinstance(step, dict):
                _validate_provenance(step, step_location, applied_event_ids, issues)
                _validate_evidence(step, step_location, evidence_ids, issues)
                _validate_capability_references(
                    step,
                    step_location,
                    node_ids,
                    node_kinds,
                    issues,
                )

    for index, edge in enumerate(graph.semantic_edges):
        location = f"graph.semantic_edges[{index}]"
        identifier = edge.get("id")
        if not isinstance(identifier, str) or not _is_ulid_id(identifier, "edge"):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.id",
                "ID must use the edge_ namespace",
            )
        _validate_unique(identifier, identifiers, location, issues)
        _validate_record_revision(
            edge, "edge_revision", location, graph.graph_revision, issues
        )
        _validate_provenance(edge, location, applied_event_ids, issues)
        _validate_evidence(edge, location, evidence_ids, issues)
        _validate_repository_relative_fields(edge, location, issues)
        _validate_edge_references(edge, location, node_ids, node_kinds, issues)

    for index, mapping in enumerate(graph.implementation_mappings):
        location = f"graph.implementation_mappings[{index}]"
        identifier = mapping.get("id")
        if not isinstance(identifier, str) or not _is_ulid_id(identifier, "map"):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.id",
                "ID must use the map_ namespace",
            )
        _validate_unique(identifier, identifiers, location, issues)
        _validate_record_revision(
            mapping, "mapping_revision", location, graph.graph_revision, issues
        )
        _validate_provenance(mapping, location, applied_event_ids, issues)
        _validate_evidence(mapping, location, evidence_ids, issues)
        _validate_repository_relative_fields(mapping, location, issues)
        _validate_mapping_references(
            mapping,
            location,
            node_ids,
            flow_step_ids,
            entity_ids,
            issues,
        )
    if strict_m1a:
        _validate_m1a_graph_invariants(graph, node_ids, node_kinds, issues)


def _validate_m1a_graph_invariants(
    graph: CognitiveGraph,
    node_ids: set[str],
    node_kinds: dict[str, str],
    issues: list[ValidationIssue],
) -> None:
    """Apply M1a-only semantics without breaking legacy M0 technical state.

    M0 deliberately permits small user-created graph fragments before a source
    cognition baseline exists.  Once cognition is initialized, the persisted
    JSON graph must also honour the typed M1a model in ``domain.graph``.
    """
    parents: dict[str, int] = {identifier: 0 for identifier in node_ids}
    contains: dict[str, list[str]] = {}
    for index, edge in enumerate(graph.semantic_edges):
        location = f"graph.semantic_edges[{index}]"
        relation = edge.get("type")
        source = edge.get("source_id")
        target = edge.get("target_id")
        if (
            relation in {"uses", "depends_on"}
            and edge.get("epistemic_status")
            in {
                "inferred",
                "uncertain",
            }
            and not edge.get("evidence")
        ):
            _issue(
                issues,
                ValidationIssueCode.RELATION_EVIDENCE_REQUIRED,
                f"{location}.evidence",
                "inferred or uncertain semantic edges require evidence",
            )
        if (
            relation == "contains"
            and isinstance(source, str)
            and isinstance(target, str)
        ):
            contains.setdefault(source, []).append(target)
            if node_kinds.get(target) == "behavior":
                parents[target] = parents.get(target, 0) + 1
    for identifier, kind in node_kinds.items():
        if kind == "behavior" and parents.get(identifier, 0) != 1:
            _issue(
                issues,
                ValidationIssueCode.BEHAVIOR_PARENT_COUNT,
                identifier,
                "each behavior requires exactly one responsibility parent",
            )
    if _has_cycle(contains):
        _issue(
            issues,
            ValidationIssueCode.CONTAINS_CYCLE,
            "graph.semantic_edges",
            "contains hierarchy must be acyclic",
        )

    for index, flow in enumerate(graph.logical_flows):
        location = f"graph.logical_flows[{index}]"
        status = flow.get("materialization_status")
        steps = flow.get("steps")
        if status not in {"unmaterialized", "materialized", "not_applicable"}:
            _issue(
                issues,
                ValidationIssueCode.INVALID_FLOW_STATUS,
                f"{location}.materialization_status",
                "logical flow materialization status is invalid",
            )
            continue
        if not isinstance(steps, list):
            continue
        if status == "unmaterialized" and steps:
            _issue(
                issues,
                ValidationIssueCode.UNMATERIALIZED_FLOW_HAS_STEPS,
                f"{location}.steps",
                "unmaterialized flow cannot contain steps",
            )
        elif status == "not_applicable" and steps:
            _issue(
                issues,
                ValidationIssueCode.NOT_APPLICABLE_FLOW_HAS_STEPS,
                f"{location}.steps",
                "not-applicable flow cannot contain steps",
            )
        elif status == "materialized" and not steps:
            _issue(
                issues,
                ValidationIssueCode.MATERIALIZED_FLOW_EMPTY,
                f"{location}.steps",
                "materialized flow requires steps",
            )
        orders = [step.get("order") for step in steps if isinstance(step, dict)]
        if len(orders) == len(steps) and orders != list(range(1, len(steps) + 1)):
            _issue(
                issues,
                ValidationIssueCode.FLOW_ORDER_NOT_CONTIGUOUS,
                f"{location}.steps",
                "flow step orders must start at one and be contiguous",
            )

    for index, mapping in enumerate(graph.implementation_mappings):
        location = f"graph.implementation_mappings[{index}]"
        if mapping.get("role") not in {"primary", "supporting"}:
            _issue(
                issues,
                ValidationIssueCode.INVALID_MAPPING_ROLE,
                f"{location}.role",
                "mapping role must be primary or supporting",
            )
        if mapping.get("resolution_status") not in {
            "resolved",
            "missing",
            "ambiguous",
        }:
            _issue(
                issues,
                ValidationIssueCode.INVALID_MAPPING_RESOLUTION_STATUS,
                f"{location}.resolution_status",
                "mapping resolution status is invalid",
            )


def _has_cycle(adjacency: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> bool:
        if identifier in visiting:
            return True
        if identifier in visited:
            return False
        visiting.add(identifier)
        if any(visit(target) for target in adjacency.get(identifier, ())):
            return True
        visiting.remove(identifier)
        visited.add(identifier)
        return False

    return any(visit(identifier) for identifier in adjacency)


def _validate_edge_references(
    edge: JsonObject,
    location: str,
    node_ids: set[str],
    node_kinds: dict[str, str],
    issues: list[ValidationIssue],
) -> None:
    relation = edge.get("type")
    allowed_kinds = {
        "contains": ("responsibility", "behavior"),
        "uses": ("behavior", "capability"),
        "depends_on": ("capability", "capability"),
    }
    expected_kinds = allowed_kinds.get(relation) if isinstance(relation, str) else None
    if expected_kinds is None:
        _issue(
            issues,
            ValidationIssueCode.INVALID_RELATION,
            f"{location}.type",
            "semantic edge type is unsupported",
        )
    resolved_kinds: list[str | None] = []
    for field in ("source_id", "target_id"):
        identifier = edge.get(field)
        if not isinstance(identifier, str) or not any(
            _is_semantic_id(identifier, prefix)
            for prefix in _NODE_PREFIX_BY_KIND.values()
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.{field}",
                "semantic edge endpoints must use a node namespace",
            )
            resolved_kinds.append(None)
        elif identifier not in node_ids:
            _issue(
                issues,
                ValidationIssueCode.DANGLING_REFERENCE,
                f"{location}.{field}",
                "semantic edge endpoint does not resolve to a graph node",
            )
            resolved_kinds.append(None)
        else:
            resolved_kinds.append(node_kinds[identifier])
    if (
        expected_kinds is not None
        and tuple(resolved_kinds) != expected_kinds
        and all(kind is not None for kind in resolved_kinds)
    ):
        _issue(
            issues,
            ValidationIssueCode.INVALID_RELATION,
            location,
            "semantic edge endpoint kinds are not allowed for this relation",
        )


def _validate_mapping_references(
    mapping: JsonObject,
    location: str,
    node_ids: set[str],
    flow_step_ids: set[str],
    entity_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    subject_kind = mapping.get("subject_kind")
    subject_id = mapping.get("subject_id")
    if subject_kind == "node":
        valid_namespace = isinstance(subject_id, str) and any(
            _is_semantic_id(subject_id, prefix)
            for prefix in _NODE_PREFIX_BY_KIND.values()
        )
        subject_ids = node_ids
    elif subject_kind == "flow_step":
        valid_namespace = isinstance(subject_id, str) and _is_flow_step_id(subject_id)
        subject_ids = flow_step_ids
    else:
        valid_namespace = False
        subject_ids = set()
        _issue(
            issues,
            ValidationIssueCode.INVALID_RELATION,
            f"{location}.subject_kind",
            "mapping subject kind must be node or flow_step",
        )
    if not valid_namespace:
        _issue(
            issues,
            ValidationIssueCode.INVALID_ID_NAMESPACE,
            f"{location}.subject_id",
            "mapping subject ID does not match its subject kind",
        )
    elif subject_id not in subject_ids:
        _issue(
            issues,
            ValidationIssueCode.DANGLING_REFERENCE,
            f"{location}.subject_id",
            "mapping subject does not resolve to a graph subject",
        )

    entity_uid = mapping.get("entity_uid")
    if not isinstance(entity_uid, str) or not _is_ulid_id(entity_uid, "ent"):
        _issue(
            issues,
            ValidationIssueCode.INVALID_ID_NAMESPACE,
            f"{location}.entity_uid",
            "mapping entity UID must use the ent_ namespace",
        )
    elif entity_uid not in entity_ids:
        _issue(
            issues,
            ValidationIssueCode.DANGLING_REFERENCE,
            f"{location}.entity_uid",
            "mapping entity UID does not resolve to entity refs",
        )


def _validate_capability_references(
    step: JsonObject,
    location: str,
    node_ids: set[str],
    node_kinds: dict[str, str],
    issues: list[ValidationIssue],
) -> None:
    capabilities = step.get("uses_capabilities", [])
    if not isinstance(capabilities, list):
        _issue(
            issues,
            ValidationIssueCode.INVALID_NODE,
            f"{location}.uses_capabilities",
            "flow step capability references must be a list",
        )
        return
    for index, capability_id in enumerate(capabilities):
        reference_location = f"{location}.uses_capabilities[{index}]"
        if not isinstance(capability_id, str) or not _is_semantic_id(
            capability_id, "capability."
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                reference_location,
                "flow step capability references must use the capability namespace",
            )
        elif capability_id not in node_ids:
            _issue(
                issues,
                ValidationIssueCode.DANGLING_REFERENCE,
                reference_location,
                "flow step capability does not resolve to a graph node",
            )
        elif node_kinds.get(capability_id) != "capability":
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                reference_location,
                "flow step reference does not resolve to a capability node",
            )


def _validate_evidence(
    owner: JsonObject,
    location: str,
    evidence_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    evidence = owner.get("evidence", [])
    if not isinstance(evidence, list):
        _issue(
            issues,
            ValidationIssueCode.INVALID_NODE,
            f"{location}.evidence",
            "evidence must be a list",
        )
        return
    for index, item in enumerate(evidence):
        evidence_location = f"{location}.evidence[{index}]"
        if not isinstance(item, dict):
            _issue(
                issues,
                ValidationIssueCode.INVALID_NODE,
                evidence_location,
                "evidence records must be objects",
            )
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not _is_ulid_id(evidence_id, "evid"):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{evidence_location}.id",
                "evidence ID must use the evid_ namespace",
            )
        elif evidence_id in evidence_ids:
            _issue(
                issues,
                ValidationIssueCode.DUPLICATE_ID,
                f"{evidence_location}.id",
                "evidence IDs must be unique",
            )
        else:
            evidence_ids.add(evidence_id)


def _validate_record_revision(
    record: JsonObject,
    revision_key: str,
    location: str,
    graph_revision: int,
    issues: list[ValidationIssue],
) -> None:
    revision = record.get(revision_key)
    if type(revision) is not int or revision < 1 or revision > graph_revision:
        _issue(
            issues,
            ValidationIssueCode.INVALID_REVISION,
            f"{location}.{revision_key}",
            "record revision must be between one and graph revision",
        )


def _validate_optional_node_fields(
    node: JsonObject, location: str, issues: list[ValidationIssue]
) -> None:
    epistemic = node.get("epistemic_status")
    if epistemic is not None and (
        not isinstance(epistemic, str) or epistemic not in _EPISTEMIC_VALUES
    ):
        _issue(
            issues,
            ValidationIssueCode.INVALID_NODE,
            f"{location}.epistemic_status",
            "epistemic status is invalid",
        )
    for key in ("created_by", "last_modified_by"):
        actor = node.get(key)
        if actor is not None and (
            not isinstance(actor, str) or actor not in _ACTOR_VALUES
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_NODE,
                f"{location}.{key}",
                "node actor is invalid",
            )


def _validate_repository_relative_fields(
    value: object, location: str, issues: list[ValidationIssue]
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if key == "relative_path" and (
                not isinstance(child, str) or not _is_relative_path(child)
            ):
                _issue(
                    issues,
                    ValidationIssueCode.INVALID_RELATIVE_PATH,
                    child_location,
                    "persisted paths must be normalized repository-relative POSIX paths",
                )
            else:
                _validate_repository_relative_fields(child, child_location, issues)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_repository_relative_fields(child, f"{location}[{index}]", issues)


def _validate_entity_refs(refs: EntityRefs, issues: list[ValidationIssue]) -> set[str]:
    identifiers: set[str] = set()
    ordered_ids: list[str] = []
    for index, entity in enumerate(refs.entities):
        location = f"entity_refs.entities[{index}]"
        identifier = entity.get("uid")
        if not isinstance(identifier, str) or not _is_ulid_id(identifier, "ent"):
            _issue(
                issues,
                ValidationIssueCode.INVALID_ID_NAMESPACE,
                f"{location}.uid",
                "entity reference UID must use the ent_ namespace",
            )
        else:
            ordered_ids.append(identifier)
        _validate_unique(identifier, identifiers, location, issues)
        relative_path = entity.get("relative_path")
        if relative_path is not None and (
            not isinstance(relative_path, str) or not _is_relative_path(relative_path)
        ):
            _issue(
                issues,
                ValidationIssueCode.INVALID_RELATIVE_PATH,
                f"{location}.relative_path",
                "entity paths must be normalized repository-relative POSIX paths",
            )
    if ordered_ids != sorted(ordered_ids):
        _issue(
            issues,
            ValidationIssueCode.INVALID_NODE,
            "entity_refs.entities",
            "entity references must be sorted by stable UID",
        )
    return identifiers


def _validate_unique(
    identifier: object,
    identifiers: set[str],
    location: str,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(identifier, str):
        return
    if identifier in identifiers:
        _issue(
            issues,
            ValidationIssueCode.DUPLICATE_ID,
            f"{location}.id",
            "formal IDs must be unique within their collection",
        )
    identifiers.add(identifier)


def _validate_provenance(
    record: JsonObject,
    location: str,
    applied_event_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    approval = record.get("approval")
    event_id = approval.get("approval_event_id") if isinstance(approval, dict) else None
    if not isinstance(event_id, str):
        _issue(
            issues,
            ValidationIssueCode.MISSING_PROVENANCE,
            f"{location}.approval.approval_event_id",
            "formal graph records require approval-event provenance",
        )
        return
    if not _is_ulid_id(event_id, "evt"):
        _issue(
            issues,
            ValidationIssueCode.INVALID_ID_NAMESPACE,
            f"{location}.approval.approval_event_id",
            "approval event must use the evt_ namespace",
        )
        return
    if event_id not in applied_event_ids:
        _issue(
            issues,
            ValidationIssueCode.DANGLING_APPROVAL_EVENT,
            f"{location}.approval.approval_event_id",
            "approval event does not resolve to a cognitive proposal applied event",
        )


def _is_digest(value: str) -> bool:
    payload = value.removeprefix(_DIGEST_PREFIX)
    return (
        value.startswith(_DIGEST_PREFIX)
        and len(payload) == 64
        and all(character in "0123456789abcdef" for character in payload)
    )


def _is_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and value != "."
    )


def _is_ulid_id(value: str, prefix: str) -> bool:
    namespace = f"{prefix}_"
    payload = value.removeprefix(namespace)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return (
        value.startswith(namespace)
        and len(payload) == 26
        and all(character in alphabet for character in payload)
        and payload[0] in "01234567"
    )


def _is_semantic_id(value: str, prefix: str) -> bool:
    return value.startswith(prefix) and _valid_slug(value.removeprefix(prefix))


def _is_flow_step_id(value: str) -> bool:
    behavior_id, separator, slug = value.partition("#step.")
    return (
        separator == "#step."
        and _is_semantic_id(behavior_id, "behavior.")
        and _valid_slug(slug)
    )


def _valid_slug(value: str) -> bool:
    return bool(value) and all(
        character.islower() or character.isdigit() or character == "-"
        for character in value
    )
