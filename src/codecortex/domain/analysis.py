"""Bounded Analyzer report model and its strict pure validator.

M1a design section 10: the Analyzer subagent returns one complete, compressed,
structured report.  "Complete" means the report holds every candidate the
Analyzer chose to submit this round, never that it covers the repository.

Decisions fixed here (each is fail-closed to protect the Main context):

- Every object uses an exact key set; unknown or missing keys reject the
  report instead of silently dropping Analyzer output.
- Candidates carry no ``approval`` or ``*_revision`` fields; those are
  stamped by the formal apply path, not by the Analyzer.
- Source inference can never claim ``established`` epistemic status inside a
  candidate report (design section 8.1).
- Evidence items are counted across the whole report (top-level plus items
  embedded in nodes, edges, flows, steps, and mappings) against one shared
  cap, and evidence IDs are unique across the whole report.
- Every free-text prose field (summary, intent, observed, observation,
  evidence_note, flow-step summary, and each uncertainties/unmapped_regions/
  diagnostics/coverage entry) is capped in Unicode code points.
- Freshness is compared right after the header parses: a stale report is
  rejected before the expensive candidate validation runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.graph import (
    _NODE_ID,
    _SLUG,
    Actor,
    Behavior,
    Capability,
    CognitiveEdge,
    EdgeType,
    EpistemicStatus,
    Evidence,
    FlowStep,
    ImplementationMapping,
    LogicalFlow,
    MappingRole,
    MappingSubjectKind,
    MaterializationStatus,
    NodeKind,
    ResolutionStatus,
    Responsibility,
    SemanticNode,
    _valid_relative_path,
    _valid_ulid_id,
)

_SOURCE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FLOW_STEP_ID = re.compile(
    r"behavior\.[a-z0-9]+(?:-[a-z0-9]+)*#step\.[a-z0-9]+(?:-[a-z0-9]+)*\Z"
)
_EVIDENCE_KINDS = frozenset({"code_entity", "repository_document"})

_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "base_graph_revision",
        "analyzed_source_digest",
        "analysis_scope",
        "coverage",
        "candidate_nodes",
        "candidate_edges",
        "candidate_flows",
        "candidate_mappings",
        "evidence",
        "uncertainties",
        "unmapped_regions",
        "diagnostics",
    }
)
_SCOPE_KEYS = frozenset({"mode", "files", "modules"})
_COVERAGE_KEYS = frozenset({"analyzed_partitions", "unexamined_partitions"})
_NODE_KEYS = frozenset(
    {
        "id",
        "kind",
        "title",
        "aliases",
        "summary",
        "epistemic_status",
        "intent",
        "observed",
        "evidence",
        "created_by",
        "last_modified_by",
    }
)
_NODE_REQUIRED = frozenset({"id", "kind", "title"})
_EDGE_KEYS = frozenset(
    {"id", "type", "source_id", "target_id", "epistemic_status", "evidence"}
)
_EDGE_REQUIRED = frozenset({"id", "type", "source_id", "target_id"})
_FLOW_KEYS = frozenset({"behavior_id", "materialization_status", "steps", "evidence"})
_FLOW_REQUIRED = frozenset({"behavior_id", "materialization_status"})
_STEP_KEYS = frozenset(
    {"id", "order", "title", "summary", "uses_capabilities", "evidence"}
)
_STEP_REQUIRED = frozenset({"id", "order", "title"})
_MAPPING_KEYS = frozenset(
    {
        "id",
        "subject_kind",
        "subject_id",
        "entity_uid",
        "role",
        "resolution_status",
        "evidence_note",
        "evidence",
    }
)
_MAPPING_REQUIRED = frozenset(
    {"id", "subject_kind", "subject_id", "entity_uid", "role", "resolution_status"}
)
_EVIDENCE_KEYS = frozenset(
    {"id", "kind", "entity_uid", "relative_path", "start_line", "end_line", "observation"}
)
_EVIDENCE_REQUIRED = frozenset({"id", "kind"})


@dataclass(frozen=True, slots=True)
class AnalysisLimits:
    """The default safety caps protecting the Main Codex context."""

    serialized_bytes: int = 512 * 1024
    nodes: int = 300
    edges: int = 1000
    mappings: int = 2000
    evidence: int = 2000
    description_codepoints: int = 240


DEFAULT_ANALYSIS_LIMITS = AnalysisLimits()


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    """The analyzed repository scope summary."""

    mode: str
    files: int
    modules: int


@dataclass(frozen=True, slots=True)
class AnalysisCoverage:
    """Which recommended partitions were analyzed and which were not."""

    analyzed_partitions: tuple[str, ...]
    unexamined_partitions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """One validated, bounded Analyzer report ready for proposal creation."""

    schema_version: int
    base_graph_revision: int
    analyzed_source_digest: str
    analysis_scope: AnalysisScope
    coverage: AnalysisCoverage
    candidate_nodes: tuple[SemanticNode, ...]
    candidate_edges: tuple[CognitiveEdge, ...]
    candidate_flows: tuple[LogicalFlow, ...]
    candidate_mappings: tuple[ImplementationMapping, ...]
    evidence: tuple[Evidence, ...]
    uncertainties: tuple[str, ...]
    unmapped_regions: tuple[str, ...]
    diagnostics: tuple[str, ...]


class _ReportState:
    """Mutable cross-collection tallies shared by the per-item parsers."""

    def __init__(self) -> None:
        self.evidence_count = 0
        self.evidence_ids: set[str] = set()


def validate_analysis_report(
    payload: bytes,
    expected_graph_revision: int,
    expected_source_digest: str,
    *,
    limits: AnalysisLimits = DEFAULT_ANALYSIS_LIMITS,
) -> AnalysisReport:
    """Validate one serialized Analyzer report against caps and freshness.

    Raises ``ANALYSIS_REPORT_INVALID`` for any structural or cap violation and
    ``PROPOSAL_STALE`` when the report's base graph revision or analyzed
    source digest no longer matches the values Main re-read before consuming
    the report.
    """
    if (
        type(expected_graph_revision) is not int
        or isinstance(expected_graph_revision, bool)
        or expected_graph_revision < 0
    ):
        raise ValueError("Expected graph revision must be a non-negative integer")
    if not isinstance(expected_source_digest, str) or not expected_source_digest:
        raise ValueError("Expected source digest must be a non-empty string")

    if len(payload) > limits.serialized_bytes:
        raise _invalid(
            "Serialized analysis report exceeds the byte cap",
            limit=limits.serialized_bytes,
            actual=len(payload),
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid("Analysis report is not valid UTF-8") from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise _invalid("Analysis report is not valid JSON") from error
    if not isinstance(data, dict):
        raise _invalid("Analysis report must be a JSON object")
    if set(data) != _REPORT_KEYS:
        raise _invalid(
            "Analysis report top-level fields do not match the schema",
            actual=sorted(str(key) for key in data),
        )

    base_graph_revision = _header_revision(data["base_graph_revision"])
    analyzed_source_digest = _header_digest(data["analyzed_source_digest"])
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise _invalid("Analysis report requires schema_version 1")
    if base_graph_revision != expected_graph_revision:
        raise _stale(
            "Graph revision changed during analysis",
            expected=expected_graph_revision,
            actual=base_graph_revision,
        )
    if analyzed_source_digest != expected_source_digest:
        raise _stale(
            "Source changed during analysis",
            expected=expected_source_digest,
            actual=analyzed_source_digest,
        )

    scope = _parse_scope(data["analysis_scope"])
    coverage = _parse_coverage(data["coverage"], limits)
    state = _ReportState()
    nodes = _parse_nodes(data["candidate_nodes"], limits, state)
    edges = _parse_edges(data["candidate_edges"], limits, state)
    flows = _parse_flows(data["candidate_flows"], limits, state)
    mappings = _parse_mappings(data["candidate_mappings"], limits, state)
    evidence = _parse_evidence_list(data["evidence"], "evidence", limits, state)
    return AnalysisReport(
        schema_version=1,
        base_graph_revision=base_graph_revision,
        analyzed_source_digest=analyzed_source_digest,
        analysis_scope=scope,
        coverage=coverage,
        candidate_nodes=nodes,
        candidate_edges=edges,
        candidate_flows=flows,
        candidate_mappings=mappings,
        evidence=evidence,
        uncertainties=_parse_text_list(
            data["uncertainties"], "uncertainties", limits
        ),
        unmapped_regions=_parse_text_list(
            data["unmapped_regions"], "unmapped_regions", limits
        ),
        diagnostics=_parse_text_list(data["diagnostics"], "diagnostics", limits),
    )


def _header_revision(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise _invalid("base_graph_revision must be a non-negative integer")
    return value


def _header_digest(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_DIGEST.fullmatch(value) is None:
        raise _invalid("analyzed_source_digest must be a sha256 hex digest")
    return value


def _parse_scope(raw: object) -> AnalysisScope:
    location = "analysis_scope"
    mapping = _as_mapping(raw, location, _SCOPE_KEYS, _SCOPE_KEYS)
    if mapping["mode"] != "repository":
        raise _invalid(
            "analysis_scope.mode is not supported in M1a", actual=mapping["mode"]
        )
    files = mapping["files"]
    modules = mapping["modules"]
    for name, value in (("files", files), ("modules", modules)):
        if type(value) is not int or isinstance(value, bool) or value < 0:
            raise _invalid(f"analysis_scope.{name} must be a non-negative integer")
    return AnalysisScope(mode="repository", files=files, modules=modules)


def _parse_coverage(raw: object, limits: AnalysisLimits) -> AnalysisCoverage:
    location = "coverage"
    mapping = _as_mapping(raw, location, _COVERAGE_KEYS, _COVERAGE_KEYS)
    return AnalysisCoverage(
        analyzed_partitions=_parse_text_list(
            mapping["analyzed_partitions"],
            f"{location}.analyzed_partitions",
            limits,
        ),
        unexamined_partitions=_parse_text_list(
            mapping["unexamined_partitions"],
            f"{location}.unexamined_partitions",
            limits,
        ),
    )


def _parse_nodes(
    raw: object, limits: AnalysisLimits, state: _ReportState
) -> tuple[SemanticNode, ...]:
    items = _as_list(raw, "candidate_nodes")
    if len(items) > limits.nodes:
        raise _invalid(
            "Analysis report exceeds the node cap",
            limit=limits.nodes,
            actual=len(items),
        )
    seen: set[str] = set()
    nodes: list[SemanticNode] = []
    for index, item in enumerate(items):
        location = f"candidate_nodes[{index}]"
        mapping = _as_mapping(item, location, _NODE_KEYS, _NODE_REQUIRED)
        kind = _enum(mapping["kind"], NodeKind, f"{location}.kind")
        node_id = mapping["id"]
        if not isinstance(node_id, str) or not node_id.startswith(f"{kind.value}."):
            raise _invalid("Node ID must match its semantic kind", field=f"{location}.id")
        if _NODE_ID.fullmatch(node_id) is None:
            raise _invalid("Node ID is malformed", field=f"{location}.id")
        if node_id in seen:
            raise _invalid("Duplicate candidate node ID", field=f"{location}.id")
        seen.add(node_id)
        title = mapping["title"]
        if not isinstance(title, str) or not title.strip():
            raise _invalid("Node title must be non-empty text", field=f"{location}.title")
        epistemic = _epistemic(
            mapping.get("epistemic_status", EpistemicStatus.INFERRED),
            f"{location}.epistemic_status",
        )
        aliases = mapping.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise _invalid("Node aliases must be non-empty text", field=f"{location}.aliases")
        evidence = _parse_evidence_list(
            mapping.get("evidence", []), f"{location}.evidence", limits, state
        )
        node_type = {
            NodeKind.RESPONSIBILITY: Responsibility,
            NodeKind.BEHAVIOR: Behavior,
            NodeKind.CAPABILITY: Capability,
        }[kind]
        nodes.append(
            node_type(
                id=node_id,
                title=title,
                aliases=tuple(aliases),
                summary=_prose(mapping.get("summary"), f"{location}.summary", limits),
                epistemic_status=epistemic,
                intent=_prose(mapping.get("intent"), f"{location}.intent", limits),
                observed=_prose(mapping.get("observed"), f"{location}.observed", limits),
                evidence=evidence,
                created_by=_actor(mapping.get("created_by"), f"{location}.created_by"),
                last_modified_by=_actor(
                    mapping.get("last_modified_by"), f"{location}.last_modified_by"
                ),
            )
        )
    return tuple(nodes)


def _parse_edges(
    raw: object, limits: AnalysisLimits, state: _ReportState
) -> tuple[CognitiveEdge, ...]:
    items = _as_list(raw, "candidate_edges")
    if len(items) > limits.edges:
        raise _invalid(
            "Analysis report exceeds the edge cap",
            limit=limits.edges,
            actual=len(items),
        )
    seen: set[str] = set()
    edges: list[CognitiveEdge] = []
    for index, item in enumerate(items):
        location = f"candidate_edges[{index}]"
        mapping = _as_mapping(item, location, _EDGE_KEYS, _EDGE_REQUIRED)
        edge_id = mapping["id"]
        if not _valid_ulid_id(edge_id, "edge"):
            raise _invalid("Edge ID must use edge_ ULID namespace", field=f"{location}.id")
        if edge_id in seen:
            raise _invalid("Duplicate candidate edge ID", field=f"{location}.id")
        seen.add(edge_id)
        edge_type = _enum(mapping["type"], EdgeType, f"{location}.type")
        for endpoint in ("source_id", "target_id"):
            value = mapping[endpoint]
            if not isinstance(value, str) or _NODE_ID.fullmatch(value) is None:
                raise _invalid(
                    "Edge endpoint must be a semantic node ID",
                    field=f"{location}.{endpoint}",
                )
        edges.append(
            CognitiveEdge(
                id=edge_id,
                type=edge_type,
                source_id=mapping["source_id"],
                target_id=mapping["target_id"],
                epistemic_status=_epistemic(
                    mapping.get("epistemic_status", EpistemicStatus.INFERRED),
                    f"{location}.epistemic_status",
                ),
                evidence=_parse_evidence_list(
                    mapping.get("evidence", []), f"{location}.evidence", limits, state
                ),
            )
        )
    return tuple(edges)


def _parse_flows(
    raw: object, limits: AnalysisLimits, state: _ReportState
) -> tuple[LogicalFlow, ...]:
    items = _as_list(raw, "candidate_flows")
    owners: set[str] = set()
    flows: list[LogicalFlow] = []
    for index, item in enumerate(items):
        location = f"candidate_flows[{index}]"
        mapping = _as_mapping(item, location, _FLOW_KEYS, _FLOW_REQUIRED)
        behavior_id = mapping["behavior_id"]
        if not isinstance(behavior_id, str) or not behavior_id.startswith("behavior."):
            raise _invalid(
                "Flow owner must be a behavior ID", field=f"{location}.behavior_id"
            )
        if _NODE_ID.fullmatch(behavior_id) is None:
            raise _invalid("Flow owner ID is malformed", field=f"{location}.behavior_id")
        if behavior_id in owners:
            raise _invalid(
                "Duplicate logical flow for one behavior", field=f"{location}.behavior_id"
            )
        owners.add(behavior_id)
        status = _enum(
            mapping["materialization_status"],
            MaterializationStatus,
            f"{location}.materialization_status",
        )
        steps = _parse_steps(
            mapping.get("steps", []), behavior_id, location, limits, state
        )
        if status is MaterializationStatus.MATERIALIZED and not steps:
            raise _invalid(
                "Materialized flow requires at least one step", field=f"{location}.steps"
            )
        if status is not MaterializationStatus.MATERIALIZED and steps:
            raise _invalid(
                "Unmaterialized or not-applicable flow cannot contain steps",
                field=f"{location}.steps",
            )
        flows.append(
            LogicalFlow(
                behavior_id=behavior_id,
                materialization_status=status,
                flow_revision=1,
                steps=steps,
                evidence=_parse_evidence_list(
                    mapping.get("evidence", []), f"{location}.evidence", limits, state
                ),
            )
        )
    return tuple(flows)


def _parse_steps(
    raw: object,
    behavior_id: str,
    flow_location: str,
    limits: AnalysisLimits,
    state: _ReportState,
) -> tuple[FlowStep, ...]:
    items = _as_list(raw, f"{flow_location}.steps")
    steps: list[FlowStep] = []
    orders: list[int] = []
    for index, item in enumerate(items):
        location = f"{flow_location}.steps[{index}]"
        mapping = _as_mapping(item, location, _STEP_KEYS, _STEP_REQUIRED)
        step_id = mapping["id"]
        expected_prefix = f"{behavior_id}#step."
        if not isinstance(step_id, str) or not step_id.startswith(expected_prefix):
            raise _invalid("Flow step ID must extend its behavior ID", field=f"{location}.id")
        if _SLUG.fullmatch(step_id.removeprefix(expected_prefix)) is None:
            raise _invalid("Flow step ID suffix is malformed", field=f"{location}.id")
        order = mapping["order"]
        if type(order) is not int or isinstance(order, bool) or order < 1:
            raise _invalid("Flow step order must be a positive integer", field=f"{location}.order")
        orders.append(order)
        title = mapping["title"]
        if not isinstance(title, str) or not title.strip():
            raise _invalid("Flow step title must be non-empty text", field=f"{location}.title")
        capabilities = mapping.get("uses_capabilities", [])
        if not isinstance(capabilities, list):
            raise _invalid(
                "Flow step capabilities must be a list",
                field=f"{location}.uses_capabilities",
            )
        for capability_index, capability_id in enumerate(capabilities):
            if not isinstance(capability_id, str) or not capability_id.startswith(
                "capability."
            ) or _NODE_ID.fullmatch(capability_id) is None:
                raise _invalid(
                    "Flow step capability must be a capability node ID",
                    field=f"{location}.uses_capabilities[{capability_index}]",
                )
        steps.append(
            FlowStep(
                id=step_id,
                order=order,
                title=title,
                summary=_prose(mapping.get("summary"), f"{location}.summary", limits),
                uses_capabilities=tuple(capabilities),
                evidence=_parse_evidence_list(
                    mapping.get("evidence", []), f"{location}.evidence", limits, state
                ),
            )
        )
    if orders and sorted(orders) != list(range(1, len(orders) + 1)):
        raise _invalid(
            "Flow step order must start at one and be contiguous",
            field=f"{flow_location}.steps",
        )
    return tuple(steps)


def _parse_mappings(
    raw: object, limits: AnalysisLimits, state: _ReportState
) -> tuple[ImplementationMapping, ...]:
    items = _as_list(raw, "candidate_mappings")
    if len(items) > limits.mappings:
        raise _invalid(
            "Analysis report exceeds the mapping cap",
            limit=limits.mappings,
            actual=len(items),
        )
    seen: set[str] = set()
    mappings: list[ImplementationMapping] = []
    for index, item in enumerate(items):
        location = f"candidate_mappings[{index}]"
        mapping = _as_mapping(item, location, _MAPPING_KEYS, _MAPPING_REQUIRED)
        mapping_id = mapping["id"]
        if not _valid_ulid_id(mapping_id, "map"):
            raise _invalid(
                "Mapping ID must use map_ ULID namespace", field=f"{location}.id"
            )
        if mapping_id in seen:
            raise _invalid("Duplicate candidate mapping ID", field=f"{location}.id")
        seen.add(mapping_id)
        subject_kind = _enum(
            mapping["subject_kind"], MappingSubjectKind, f"{location}.subject_kind"
        )
        subject_id = mapping["subject_id"]
        if not isinstance(subject_id, str):
            raise _invalid("Mapping subject must be text", field=f"{location}.subject_id")
        if subject_kind is MappingSubjectKind.NODE:
            if _NODE_ID.fullmatch(subject_id) is None:
                raise _invalid(
                    "Mapping node subject must be a semantic node ID",
                    field=f"{location}.subject_id",
                )
        elif _FLOW_STEP_ID.fullmatch(subject_id) is None:
            raise _invalid(
                "Mapping flow-step subject must be a flow step ID",
                field=f"{location}.subject_id",
            )
        entity_uid = mapping["entity_uid"]
        if not _valid_ulid_id(entity_uid, "ent"):
            raise _invalid(
                "Mapping entity UID must use ent_ ULID namespace",
                field=f"{location}.entity_uid",
            )
        mappings.append(
            ImplementationMapping(
                id=mapping_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                entity_uid=entity_uid,
                role=_enum(mapping["role"], MappingRole, f"{location}.role"),
                resolution_status=_enum(
                    mapping["resolution_status"],
                    ResolutionStatus,
                    f"{location}.resolution_status",
                ),
                evidence_note=_prose(
                    mapping.get("evidence_note"), f"{location}.evidence_note", limits
                ),
                evidence=_parse_evidence_list(
                    mapping.get("evidence", []), f"{location}.evidence", limits, state
                ),
            )
        )
    return tuple(mappings)


def _parse_evidence_list(
    raw: object, location: str, limits: AnalysisLimits, state: _ReportState
) -> tuple[Evidence, ...]:
    items = _as_list(raw, location)
    parsed: list[Evidence] = []
    for index, item in enumerate(items):
        item_location = f"{location}[{index}]"
        mapping = _as_mapping(item, item_location, _EVIDENCE_KEYS, _EVIDENCE_REQUIRED)
        state.evidence_count += 1
        if state.evidence_count > limits.evidence:
            raise _invalid(
                "Analysis report exceeds the evidence cap",
                limit=limits.evidence,
                actual=state.evidence_count,
            )
        evidence_id = mapping["id"]
        if not _valid_ulid_id(evidence_id, "evid"):
            raise _invalid(
                "Evidence ID must use evid_ ULID namespace", field=f"{item_location}.id"
            )
        if evidence_id in state.evidence_ids:
            raise _invalid("Duplicate evidence ID", field=f"{item_location}.id")
        state.evidence_ids.add(evidence_id)
        kind = mapping["kind"]
        if kind not in _EVIDENCE_KINDS:
            raise _invalid("Evidence kind is invalid", field=f"{item_location}.kind")
        entity_uid = mapping.get("entity_uid")
        if kind == "code_entity" and not _valid_ulid_id(entity_uid, "ent"):
            raise _invalid(
                "Code-entity evidence requires an ent_ UID",
                field=f"{item_location}.entity_uid",
            )
        relative_path = mapping.get("relative_path")
        if not isinstance(relative_path, str) or not _valid_relative_path(relative_path):
            raise _invalid(
                "Evidence path must be repository-relative POSIX",
                field=f"{item_location}.relative_path",
            )
        start_line = mapping.get("start_line")
        end_line = mapping.get("end_line")
        if (start_line is None) != (end_line is None):
            raise _invalid(
                "Source evidence requires both start and end line",
                field=item_location,
            )
        if start_line is not None and (
            type(start_line) is not int
            or isinstance(start_line, bool)
            or type(end_line) is not int
            or isinstance(end_line, bool)
            or start_line < 1
            or end_line < start_line
        ):
            raise _invalid("Evidence line range is invalid", field=item_location)
        parsed.append(
            Evidence(
                id=evidence_id,
                kind=kind,
                entity_uid=entity_uid,
                relative_path=relative_path,
                start_line=start_line,
                end_line=end_line,
                observation=_prose(
                    mapping.get("observation"), f"{item_location}.observation", limits
                ),
            )
        )
    return tuple(parsed)


def _parse_text_list(
    raw: object, location: str, limits: AnalysisLimits
) -> tuple[str, ...]:
    items = _as_list(raw, location)
    parsed: list[str] = []
    for index, item in enumerate(items):
        item_location = f"{location}[{index}]"
        if not isinstance(item, str) or not item.strip():
            raise _invalid("Entries must be non-empty text", field=item_location)
        if len(item) > limits.description_codepoints:
            raise _invalid(
                "Entry exceeds the code-point cap",
                field=item_location,
                limit=limits.description_codepoints,
                actual=len(item),
            )
        parsed.append(item)
    return tuple(parsed)


def _prose(value: object, location: str, limits: AnalysisLimits) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("Prose fields must be text", field=location)
    if len(value) > limits.description_codepoints:
        raise _invalid(
            "Prose field exceeds the code-point cap",
            field=location,
            limit=limits.description_codepoints,
            actual=len(value),
        )
    return value


def _epistemic(value: object, location: str) -> EpistemicStatus:
    status = _enum(value, EpistemicStatus, location)
    if status is EpistemicStatus.ESTABLISHED:
        raise _invalid(
            "Source analysis cannot claim established status", field=location
        )
    return status


def _actor(value: object, location: str) -> Actor:
    if value is None:
        return Actor.ANALYZER
    return _enum(value, Actor, location)


def _enum[T: StrEnum](value: object, enum_type: type[T], location: str) -> T:
    try:
        return enum_type(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise _invalid(
            f"Value is not a valid {enum_type.__name__}", field=location, actual=value
        ) from error


def _as_mapping(
    raw: object, location: str, allowed: frozenset[str], required: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _invalid("Expected an object", field=location)
    keys = set(raw)
    if not keys <= allowed or not required <= keys:
        raise _invalid(
            "Object fields do not match the schema",
            field=location,
            actual=sorted(str(key) for key in keys),
        )
    return raw


def _as_list(raw: object, location: str) -> list[Any]:
    if not isinstance(raw, list):
        raise _invalid("Expected a list", field=location)
    return raw


def _invalid(message: str, **details: object) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.ANALYSIS_REPORT_INVALID,
        message,
        details={key: value for key, value in details.items()},
        suggested_action="Ask the Analyzer to regenerate a bounded report",
    )


def _stale(message: str, **details: object) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PROPOSAL_STALE,
        message,
        details={key: value for key, value in details.items()},
        suggested_action="Re-run the Analyzer against the current source snapshot",
    )
