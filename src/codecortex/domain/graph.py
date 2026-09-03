"""Typed M1a cognitive-graph model and its pure invariant validator.

The formal JSON files intentionally remain adapter-owned dictionaries for M0
compatibility.  This module is the stricter, typed representation used by M1a
analysis, proposal construction, and graph-replica imports.  Keeping the
validator here pure makes the rules testable without a repository or SQLite.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

_ULID_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_NODE_ID = re.compile(
    r"(?:responsibility|behavior|capability)\.[a-z0-9]+(?:-[a-z0-9]+)*\Z"
)


class NodeKind(StrEnum):
    """The only semantic node kinds in the M1a graph."""

    RESPONSIBILITY = "responsibility"
    BEHAVIOR = "behavior"
    CAPABILITY = "capability"


class EpistemicStatus(StrEnum):
    """How certain the semantic conclusion is, independent of approval."""

    ESTABLISHED = "established"
    INFERRED = "inferred"
    UNCERTAIN = "uncertain"


class Actor(StrEnum):
    """The producer or last editor of a formal semantic conclusion."""

    ANALYZER = "analyzer"
    MAIN_CODEX = "main_codex"
    USER = "user"


class EdgeType(StrEnum):
    CONTAINS = "contains"
    USES = "uses"
    DEPENDS_ON = "depends_on"


class MaterializationStatus(StrEnum):
    UNMATERIALIZED = "unmaterialized"
    MATERIALIZED = "materialized"
    NOT_APPLICABLE = "not_applicable"


class MappingSubjectKind(StrEnum):
    NODE = "node"
    FLOW_STEP = "flow_step"


class MappingRole(StrEnum):
    PRIMARY = "primary"
    SUPPORTING = "supporting"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Approval:
    """The immutable applied-event reference that authorizes a formal object."""

    approval_event_id: str
    approved_by: Actor | str = Actor.USER
    approved_at: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """A source snapshot supporting a semantic conclusion."""

    id: str
    kind: str
    entity_uid: str | None = None
    relative_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    observation: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticNode:
    """Common fields shared by all three allowed semantic node kinds."""

    id: str
    title: str
    aliases: tuple[str, ...] = ()
    summary: str | None = None
    epistemic_status: EpistemicStatus | str = EpistemicStatus.INFERRED
    intent: str | None = None
    observed: str | None = None
    evidence: tuple[Evidence, ...] = ()
    created_by: Actor | str = Actor.ANALYZER
    last_modified_by: Actor | str = Actor.ANALYZER
    node_revision: int = 1
    approval: Approval | None = None

    @property
    def kind(self) -> NodeKind:
        raise NotImplementedError


@dataclass(frozen=True, slots=True, kw_only=True)
class Responsibility(SemanticNode):
    @property
    def kind(self) -> NodeKind:
        return NodeKind.RESPONSIBILITY


@dataclass(frozen=True, slots=True, kw_only=True)
class Behavior(SemanticNode):
    @property
    def kind(self) -> NodeKind:
        return NodeKind.BEHAVIOR


@dataclass(frozen=True, slots=True, kw_only=True)
class Capability(SemanticNode):
    @property
    def kind(self) -> NodeKind:
        return NodeKind.CAPABILITY


@dataclass(frozen=True, slots=True)
class CognitiveEdge:
    id: str
    type: EdgeType | str
    source_id: str
    target_id: str
    epistemic_status: EpistemicStatus | str = EpistemicStatus.INFERRED
    evidence: tuple[Evidence, ...] = ()
    edge_revision: int = 1
    approval: Approval | None = None


@dataclass(frozen=True, slots=True)
class FlowStep:
    id: str
    order: int
    title: str
    summary: str | None = None
    uses_capabilities: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    approval: Approval | None = None


@dataclass(frozen=True, slots=True)
class LogicalFlow:
    behavior_id: str
    materialization_status: MaterializationStatus | str
    flow_revision: int
    steps: tuple[FlowStep, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    approval: Approval | None = None


@dataclass(frozen=True, slots=True)
class ImplementationMapping:
    id: str
    subject_kind: MappingSubjectKind | str
    subject_id: str
    entity_uid: str
    role: MappingRole | str
    resolution_status: ResolutionStatus | str
    evidence_note: str | None = None
    evidence: tuple[Evidence, ...] = ()
    mapping_revision: int = 1
    approval: Approval | None = None


@dataclass(frozen=True, slots=True)
class CognitiveGraph:
    """A complete typed M1a graph snapshot, not a persistence adapter DTO."""

    graph_revision: int = 0
    nodes: tuple[SemanticNode, ...] = ()
    semantic_edges: tuple[CognitiveEdge, ...] = ()
    logical_flows: tuple[LogicalFlow, ...] = ()
    implementation_mappings: tuple[ImplementationMapping, ...] = ()
    entity_uids: frozenset[str] = field(default_factory=frozenset)
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class GraphViolation:
    """One deterministic graph-invariant failure suitable for UI or tests."""

    code: str
    location: str
    message: str


_ALLOWED_EDGES = {
    (NodeKind.RESPONSIBILITY, EdgeType.CONTAINS, NodeKind.BEHAVIOR),
    (NodeKind.BEHAVIOR, EdgeType.USES, NodeKind.CAPABILITY),
    (NodeKind.CAPABILITY, EdgeType.DEPENDS_ON, NodeKind.CAPABILITY),
}


def validate_cognitive_graph(graph: CognitiveGraph) -> tuple[GraphViolation, ...]:
    """Return all deterministic M1a graph violations in traversal order.

    This function deliberately does no source lookup.  ``entity_uids`` is the
    formal entity-ref set supplied by the caller; a mapping may point to an
    entity that is missing from the live facts cache as long as its UID remains
    in that formal set.
    """
    violations: list[GraphViolation] = []
    _validate_graph_header(graph, violations)
    evidence_ids: set[str] = set()
    node_by_id = _validate_nodes(graph, evidence_ids, violations)
    _validate_edges(graph, node_by_id, evidence_ids, violations)
    step_ids = _validate_flows(graph, node_by_id, evidence_ids, violations)
    _validate_mappings(graph, node_by_id, step_ids, evidence_ids, violations)
    return tuple(violations)


def _violation(
    violations: list[GraphViolation], code: str, location: str, message: str
) -> None:
    violations.append(GraphViolation(code, location, message))


def _validate_graph_header(
    graph: CognitiveGraph, violations: list[GraphViolation]
) -> None:
    if graph.schema_version != 1:
        _violation(
            violations,
            "UNSUPPORTED_SCHEMA",
            "schema_version",
            "M1a requires schema version 1",
        )
    if type(graph.graph_revision) is not int or graph.graph_revision < 0:
        _violation(
            violations,
            "INVALID_GRAPH_REVISION",
            "graph_revision",
            "graph revision must be a non-negative integer",
        )


def _validate_nodes(
    graph: CognitiveGraph,
    evidence_ids: set[str],
    violations: list[GraphViolation],
) -> dict[str, SemanticNode]:
    nodes: dict[str, SemanticNode] = {}
    alias_owner: dict[str, str] = {}
    for index, node in enumerate(graph.nodes):
        location = f"nodes[{index}]"
        if not isinstance(node, (Responsibility, Behavior, Capability)):
            _violation(
                violations,
                "INVALID_NODE_KIND",
                location,
                "M1a permits only responsibility, behavior, and capability nodes",
            )
            continue
        expected_prefix = f"{node.kind.value}."
        if not _valid_semantic_id(node.id, expected_prefix):
            _violation(
                violations,
                "NODE_ID_NAMESPACE",
                f"{location}.id",
                "node ID must match its semantic kind",
            )
        if node.id in nodes:
            _violation(
                violations,
                "DUPLICATE_NODE_ID",
                f"{location}.id",
                "node IDs must be unique",
            )
        else:
            nodes[node.id] = node
        if not isinstance(node.title, str) or not node.title.strip():
            _violation(
                violations,
                "INVALID_NODE_TITLE",
                f"{location}.title",
                "node title must be non-empty text",
            )
        _validate_revision(
            node.node_revision,
            graph.graph_revision,
            f"{location}.node_revision",
            violations,
        )
        _validate_epistemic(
            node.epistemic_status, f"{location}.epistemic_status", violations
        )
        _validate_actor(node.created_by, f"{location}.created_by", violations)
        _validate_actor(
            node.last_modified_by, f"{location}.last_modified_by", violations
        )
        if node.intent is not None and not isinstance(node.intent, str):
            _violation(
                violations,
                "INVALID_INTENT",
                f"{location}.intent",
                "intent must be text or null",
            )
        _validate_approval(node.approval, f"{location}.approval", violations)
        for alias_index, alias in enumerate(node.aliases):
            alias_location = f"{location}.aliases[{alias_index}]"
            if not isinstance(alias, str) or not alias.strip():
                _violation(
                    violations,
                    "INVALID_ALIAS",
                    alias_location,
                    "aliases must be non-empty text",
                )
                continue
            normalized = _normalized_alias(alias)
            owner = alias_owner.get(normalized)
            if owner is not None:
                _violation(
                    violations,
                    "DUPLICATE_ALIAS",
                    alias_location,
                    f"alias is already owned by {owner}",
                )
            else:
                alias_owner[normalized] = node.id
        _validate_evidence(
            node.evidence,
            f"{location}.evidence",
            evidence_ids,
            graph.entity_uids,
            violations,
        )
    return nodes


def _validate_edges(
    graph: CognitiveGraph,
    node_by_id: dict[str, SemanticNode],
    evidence_ids: set[str],
    violations: list[GraphViolation],
) -> None:
    edge_ids: set[str] = set()
    contains: dict[str, list[str]] = defaultdict(list)
    behavior_parent_count: dict[str, int] = defaultdict(int)
    for index, edge in enumerate(graph.semantic_edges):
        location = f"semantic_edges[{index}]"
        if not _valid_ulid_id(edge.id, "edge"):
            _violation(
                violations,
                "EDGE_ID_NAMESPACE",
                f"{location}.id",
                "edge ID must use edge_ ULID namespace",
            )
        if edge.id in edge_ids:
            _violation(
                violations,
                "DUPLICATE_EDGE_ID",
                f"{location}.id",
                "edge IDs must be unique",
            )
        edge_ids.add(edge.id)
        _validate_revision(
            edge.edge_revision,
            graph.graph_revision,
            f"{location}.edge_revision",
            violations,
        )
        _validate_approval(edge.approval, f"{location}.approval", violations)
        _validate_epistemic(
            edge.epistemic_status, f"{location}.epistemic_status", violations
        )
        _validate_evidence(
            edge.evidence,
            f"{location}.evidence",
            evidence_ids,
            graph.entity_uids,
            violations,
        )
        source = node_by_id.get(edge.source_id)
        target = node_by_id.get(edge.target_id)
        if source is None:
            _violation(
                violations,
                "DANGLING_EDGE_SOURCE",
                f"{location}.source_id",
                "edge source must resolve to a node",
            )
        if target is None:
            _violation(
                violations,
                "DANGLING_EDGE_TARGET",
                f"{location}.target_id",
                "edge target must resolve to a node",
            )
        edge_type = _enum_value(edge.type, EdgeType)
        if edge_type is None:
            _violation(
                violations,
                "EDGE_TYPE_NOT_ALLOWED",
                f"{location}.type",
                "edge type is not allowed in M1a",
            )
        elif (
            source is not None
            and target is not None
            and (source.kind, edge_type, target.kind) not in _ALLOWED_EDGES
        ):
            _violation(
                violations,
                "EDGE_KIND_NOT_ALLOWED",
                location,
                "edge endpoint kinds are not allowed for this relation",
            )
        if _requires_evidence(edge.epistemic_status) and not edge.evidence:
            _violation(
                violations,
                "RELATION_EVIDENCE_REQUIRED",
                f"{location}.evidence",
                "inferred or uncertain relations require evidence",
            )
        if edge_type is EdgeType.CONTAINS and source is not None and target is not None:
            contains[edge.source_id].append(edge.target_id)
            if target.kind is NodeKind.BEHAVIOR:
                behavior_parent_count[target.id] += 1

    for node in node_by_id.values():
        if node.kind is NodeKind.BEHAVIOR and behavior_parent_count[node.id] != 1:
            _violation(
                violations,
                "BEHAVIOR_PARENT_COUNT",
                node.id,
                "every behavior must have exactly one responsibility parent",
            )
    if _contains_cycle(contains):
        _violation(
            violations,
            "CONTAINS_CYCLE",
            "semantic_edges",
            "contains hierarchy must be acyclic",
        )


def _validate_flows(
    graph: CognitiveGraph,
    node_by_id: dict[str, SemanticNode],
    evidence_ids: set[str],
    violations: list[GraphViolation],
) -> set[str]:
    owners: set[str] = set()
    all_step_ids: set[str] = set()
    for index, flow in enumerate(graph.logical_flows):
        location = f"logical_flows[{index}]"
        behavior = node_by_id.get(flow.behavior_id)
        if behavior is None:
            _violation(
                violations,
                "DANGLING_FLOW_BEHAVIOR",
                f"{location}.behavior_id",
                "flow owner must resolve to a behavior",
            )
        elif behavior.kind is not NodeKind.BEHAVIOR:
            _violation(
                violations,
                "FLOW_OWNER_NOT_BEHAVIOR",
                f"{location}.behavior_id",
                "flow owner must be a behavior",
            )
        if flow.behavior_id in owners:
            _violation(
                violations,
                "DUPLICATE_LOGICAL_FLOW",
                f"{location}.behavior_id",
                "each behavior may own at most one logical flow",
            )
        owners.add(flow.behavior_id)
        _validate_revision(
            flow.flow_revision,
            graph.graph_revision,
            f"{location}.flow_revision",
            violations,
        )
        _validate_approval(flow.approval, f"{location}.approval", violations)
        _validate_evidence(
            flow.evidence,
            f"{location}.evidence",
            evidence_ids,
            graph.entity_uids,
            violations,
        )
        status = _enum_value(flow.materialization_status, MaterializationStatus)
        if status is None:
            _violation(
                violations,
                "INVALID_FLOW_STATUS",
                f"{location}.materialization_status",
                "flow materialization status is invalid",
            )
        elif status is MaterializationStatus.UNMATERIALIZED and flow.steps:
            _violation(
                violations,
                "UNMATERIALIZED_FLOW_HAS_STEPS",
                f"{location}.steps",
                "unmaterialized flow cannot contain steps",
            )
        elif status is MaterializationStatus.NOT_APPLICABLE and flow.steps:
            _violation(
                violations,
                "NOT_APPLICABLE_FLOW_HAS_STEPS",
                f"{location}.steps",
                "not-applicable flow cannot contain steps",
            )
        elif status is MaterializationStatus.MATERIALIZED and not flow.steps:
            _violation(
                violations,
                "MATERIALIZED_FLOW_EMPTY",
                f"{location}.steps",
                "materialized flow requires at least one step",
            )

        orders: list[int] = []
        for step_index, step in enumerate(flow.steps):
            step_location = f"{location}.steps[{step_index}]"
            expected_prefix = f"{flow.behavior_id}#step."
            suffix = step.id.removeprefix(expected_prefix)
            if not step.id.startswith(expected_prefix) or not _SLUG.fullmatch(suffix):
                _violation(
                    violations,
                    "FLOW_STEP_ID_NAMESPACE",
                    f"{step_location}.id",
                    "step ID must belong to the owning behavior",
                )
            if step.id in all_step_ids:
                _violation(
                    violations,
                    "DUPLICATE_FLOW_STEP_ID",
                    f"{step_location}.id",
                    "flow step IDs must be unique",
                )
            all_step_ids.add(step.id)
            if type(step.order) is not int:
                _violation(
                    violations,
                    "INVALID_FLOW_ORDER",
                    f"{step_location}.order",
                    "flow step order must be an integer",
                )
            else:
                orders.append(step.order)
            if not isinstance(step.title, str) or not step.title.strip():
                _violation(
                    violations,
                    "INVALID_FLOW_STEP_TITLE",
                    f"{step_location}.title",
                    "flow step title must be non-empty text",
                )
            _validate_approval(step.approval, f"{step_location}.approval", violations)
            _validate_evidence(
                step.evidence,
                f"{step_location}.evidence",
                evidence_ids,
                graph.entity_uids,
                violations,
            )
            for capability_index, capability_id in enumerate(step.uses_capabilities):
                capability = node_by_id.get(capability_id)
                if capability is None:
                    _violation(
                        violations,
                        "DANGLING_FLOW_CAPABILITY",
                        f"{step_location}.uses_capabilities[{capability_index}]",
                        "flow capability must resolve to a node",
                    )
                elif capability.kind is not NodeKind.CAPABILITY:
                    _violation(
                        violations,
                        "FLOW_REFERENCE_NOT_CAPABILITY",
                        f"{step_location}.uses_capabilities[{capability_index}]",
                        "flow references must target capabilities",
                    )
        if orders and sorted(orders) != list(range(1, len(orders) + 1)):
            _violation(
                violations,
                "FLOW_ORDER_NOT_CONTIGUOUS",
                f"{location}.steps",
                "flow step order must start at one and be contiguous",
            )
    return all_step_ids


def _validate_mappings(
    graph: CognitiveGraph,
    node_by_id: dict[str, SemanticNode],
    step_ids: set[str],
    evidence_ids: set[str],
    violations: list[GraphViolation],
) -> None:
    ids: set[str] = set()
    for index, mapping in enumerate(graph.implementation_mappings):
        location = f"implementation_mappings[{index}]"
        if not _valid_ulid_id(mapping.id, "map"):
            _violation(
                violations,
                "MAPPING_ID_NAMESPACE",
                f"{location}.id",
                "mapping ID must use map_ ULID namespace",
            )
        if mapping.id in ids:
            _violation(
                violations,
                "DUPLICATE_MAPPING_ID",
                f"{location}.id",
                "mapping IDs must be unique",
            )
        ids.add(mapping.id)
        _validate_revision(
            mapping.mapping_revision,
            graph.graph_revision,
            f"{location}.mapping_revision",
            violations,
        )
        _validate_approval(mapping.approval, f"{location}.approval", violations)
        _validate_evidence(
            mapping.evidence,
            f"{location}.evidence",
            evidence_ids,
            graph.entity_uids,
            violations,
        )
        subject_kind = _enum_value(mapping.subject_kind, MappingSubjectKind)
        if subject_kind is None:
            _violation(
                violations,
                "INVALID_MAPPING_SUBJECT_KIND",
                f"{location}.subject_kind",
                "mapping subject kind must be node or flow_step",
            )
        elif (
            subject_kind is MappingSubjectKind.NODE
            and mapping.subject_id not in node_by_id
        ):
            _violation(
                violations,
                "DANGLING_MAPPING_SUBJECT",
                f"{location}.subject_id",
                "mapping node subject must resolve",
            )
        elif (
            subject_kind is MappingSubjectKind.FLOW_STEP
            and mapping.subject_id not in step_ids
        ):
            _violation(
                violations,
                "DANGLING_MAPPING_SUBJECT",
                f"{location}.subject_id",
                "mapping flow-step subject must resolve",
            )
        if not _valid_ulid_id(mapping.entity_uid, "ent"):
            _violation(
                violations,
                "ENTITY_UID_NAMESPACE",
                f"{location}.entity_uid",
                "mapping entity UID must use ent_ ULID namespace",
            )
        elif mapping.entity_uid not in graph.entity_uids:
            _violation(
                violations,
                "DANGLING_ENTITY_REFERENCE",
                f"{location}.entity_uid",
                "mapping entity UID must exist in formal entity refs",
            )
        if _enum_value(mapping.role, MappingRole) is None:
            _violation(
                violations,
                "INVALID_MAPPING_ROLE",
                f"{location}.role",
                "mapping role must be primary or supporting",
            )
        if _enum_value(mapping.resolution_status, ResolutionStatus) is None:
            _violation(
                violations,
                "INVALID_MAPPING_RESOLUTION_STATUS",
                f"{location}.resolution_status",
                "mapping resolution status is invalid",
            )


def _validate_evidence(
    evidence: Iterable[Evidence],
    location: str,
    known_ids: set[str],
    entity_uids: frozenset[str],
    violations: list[GraphViolation],
) -> None:
    for index, item in enumerate(evidence):
        item_location = f"{location}[{index}]"
        if not _valid_ulid_id(item.id, "evid"):
            _violation(
                violations,
                "EVIDENCE_ID_NAMESPACE",
                f"{item_location}.id",
                "evidence ID must use evid_ ULID namespace",
            )
        if item.id in known_ids:
            _violation(
                violations,
                "DUPLICATE_EVIDENCE_ID",
                f"{item_location}.id",
                "evidence IDs must be unique per graph object class",
            )
        known_ids.add(item.id)
        if item.kind not in {"code_entity", "repository_document"}:
            _violation(
                violations,
                "INVALID_EVIDENCE_KIND",
                f"{item_location}.kind",
                "evidence kind is invalid",
            )
        if item.kind == "code_entity" and not _valid_ulid_id(item.entity_uid, "ent"):
            _violation(
                violations,
                "INVALID_EVIDENCE_ENTITY",
                f"{item_location}.entity_uid",
                "code-entity evidence requires an ent_ UID",
            )
        elif item.kind == "code_entity" and item.entity_uid not in entity_uids:
            _violation(
                violations,
                "DANGLING_EVIDENCE_ENTITY",
                f"{item_location}.entity_uid",
                "evidence entity UID must exist in formal entity refs",
            )
        if (
            item.kind in {"code_entity", "repository_document"}
            and item.relative_path is None
        ):
            _violation(
                violations,
                "MISSING_EVIDENCE_LOCATION",
                f"{item_location}.relative_path",
                "formal evidence requires a repository-relative source location",
            )
        elif item.relative_path is not None and not _valid_relative_path(
            item.relative_path
        ):
            _violation(
                violations,
                "INVALID_RELATIVE_PATH",
                f"{item_location}.relative_path",
                "evidence path must be repository-relative POSIX",
            )
        if (item.start_line is None) != (item.end_line is None):
            _violation(
                violations,
                "INVALID_EVIDENCE_LOCATION",
                item_location,
                "source evidence requires both start and end line",
            )
        elif item.start_line is not None and (
            type(item.start_line) is not int
            or type(item.end_line) is not int
            or item.start_line < 1
            or item.end_line < item.start_line
        ):
            _violation(
                violations,
                "INVALID_EVIDENCE_LOCATION",
                item_location,
                "evidence line range is invalid",
            )


def _validate_approval(
    approval: Approval | None, location: str, violations: list[GraphViolation]
) -> None:
    if approval is None:
        _violation(
            violations,
            "MISSING_APPROVAL_EVENT",
            f"{location}.approval_event_id",
            "formal semantic objects require an approval event",
        )
        return
    if not _valid_ulid_id(approval.approval_event_id, "evt"):
        _violation(
            violations,
            "APPROVAL_EVENT_NAMESPACE",
            f"{location}.approval_event_id",
            "approval event must use evt_ ULID namespace",
        )


def _validate_revision(
    value: object, graph_revision: int, location: str, violations: list[GraphViolation]
) -> None:
    if type(value) is not int or value < 1 or value > graph_revision:
        _violation(
            violations,
            "INVALID_OBJECT_REVISION",
            location,
            "object revision must be between one and graph revision",
        )


def _validate_epistemic(
    value: object, location: str, violations: list[GraphViolation]
) -> None:
    if _enum_value(value, EpistemicStatus) is None:
        _violation(
            violations,
            "INVALID_EPISTEMIC_STATUS",
            location,
            "epistemic status is invalid",
        )


def _validate_actor(
    value: object, location: str, violations: list[GraphViolation]
) -> None:
    if _enum_value(value, Actor) is None:
        _violation(violations, "INVALID_ACTOR", location, "actor is invalid")


def _contains_cycle(adjacency: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(target) for target in adjacency.get(node_id, ())):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in adjacency)


def _requires_evidence(value: object) -> bool:
    status = _enum_value(value, EpistemicStatus)
    return status in {EpistemicStatus.INFERRED, EpistemicStatus.UNCERTAIN}


def _enum_value[T: StrEnum](value: object, enum_type: type[T]) -> T | None:
    try:
        return enum_type(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _valid_ulid_id(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(f"{prefix}_"):
        return False
    payload = value.removeprefix(f"{prefix}_")
    return (
        len(payload) == 26
        and payload[0] in "01234567"
        and all(char in _ULID_ALPHABET for char in payload)
    )


def _valid_semantic_id(value: str, prefix: str) -> bool:
    return value.startswith(prefix) and _NODE_ID.fullmatch(value) is not None


def _valid_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and value != "."
    )


def _normalized_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()
