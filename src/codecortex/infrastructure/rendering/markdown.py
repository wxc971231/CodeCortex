"""Deterministic Markdown projections of the canonical cognitive graph.

Views deliberately use only formal graph data.  They never consult the local
SQLite fact cache, so the bytes committed during an approved graph apply are
portable and reproducible on another machine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from codecortex.domain.cognition import CognitiveGraph, JsonObject
from codecortex.infrastructure.formal import EMPTY_TREE_VIEW

_KIND_DIRECTORIES = {
    "responsibility": "responsibilities",
    "behavior": "behaviors",
    "capability": "capabilities",
}
_KIND_ORDER = ("responsibility", "behavior", "capability")


def render_views(graph: CognitiveGraph) -> Mapping[str, bytes]:
    """Render the complete, ordered set of Core-managed Markdown views."""
    if not graph.nodes:
        return {"views/TREE.md": EMPTY_TREE_VIEW}
    views: dict[str, bytes] = {"views/TREE.md": render_tree(graph)}
    for kind in _KIND_ORDER:
        for node in _nodes_of_kind(graph.nodes, kind):
            node_id = _text(node, "id")
            slug = node_id.removeprefix(f"{kind}.")
            views[f"views/{_KIND_DIRECTORIES[kind]}/{slug}.md"] = render_node_page(
                graph, node
            )
    return views


def render_tree(graph: CognitiveGraph) -> bytes:
    """Render the Responsibility → Behavior hierarchy and shared capabilities."""
    if not graph.nodes:
        return EMPTY_TREE_VIEW
    nodes = {_text(node, "id"): node for node in graph.nodes}
    contains = _edges_by_source(graph.semantic_edges, "contains")
    uses = _edges_by_source(graph.semantic_edges, "uses")
    lines = ["# CodeCortex Cognitive Tree", "", "## Responsibilities", ""]
    responsibilities = _nodes_of_kind(graph.nodes, "responsibility")
    for responsibility in responsibilities:
        responsibility_id = _text(responsibility, "id")
        lines.append(_tree_line("", responsibility))
        for behavior_id in contains.get(responsibility_id, ()):
            behavior = nodes.get(behavior_id)
            if behavior is None or behavior.get("kind") != "behavior":
                continue
            lines.append(_tree_line("  ", behavior))
            for capability_id in uses.get(behavior_id, ()):
                capability = nodes.get(capability_id)
                if capability is not None and capability.get("kind") == "capability":
                    lines.append(
                        f"    - uses `{capability_id}` — {_title(capability)}"
                    )
    if not responsibilities:
        lines.append("- None")
    contained_behaviors = {
        behavior_id
        for responsibility in responsibilities
        for behavior_id in contains.get(_text(responsibility, "id"), ())
    }
    unassigned = [
        behavior
        for behavior in _nodes_of_kind(graph.nodes, "behavior")
        if _text(behavior, "id") not in contained_behaviors
    ]
    if unassigned:
        lines.extend(["", "## Unassigned Behaviors", ""])
        lines.extend(_tree_line("", behavior) for behavior in unassigned)
    lines.extend(["", "## Shared Capabilities", ""])
    capabilities = _nodes_of_kind(graph.nodes, "capability")
    if capabilities:
        lines.extend(_tree_line("", capability) for capability in capabilities)
    else:
        lines.append("- None")
    return _bytes(lines)


def render_node_page(graph: CognitiveGraph, node: JsonObject) -> bytes:
    """Render one full node page from sorted formal graph collections."""
    node_id = _text(node, "id")
    lines = [f"# {_title(node)}", "", f"- ID: `{node_id}`", f"- Kind: {_text(node, 'kind')}"]
    for label, field in (
        ("Epistemic status", "epistemic_status"),
        ("Node revision", "node_revision"),
    ):
        value = node.get(field)
        if value is not None:
            lines.append(f"- {label}: {value}")
    _append_approval(lines, node)
    _append_prose(lines, "Summary", node.get("summary"))
    _append_prose(lines, "Intent", node.get("intent"))
    _append_prose(lines, "Observed", node.get("observed"))
    _append_relationships(lines, graph, node_id)
    _append_flow(lines, graph, node_id)
    _append_mappings(lines, graph, node_id)
    _append_evidence(lines, graph, node_id)
    return _bytes(lines)


def _append_prose(lines: list[str], heading: str, value: object) -> None:
    if isinstance(value, str) and value.strip():
        lines.extend(["", f"## {heading}", "", value])


def _append_relationships(lines: list[str], graph: CognitiveGraph, node_id: str) -> None:
    edges = sorted(
        (
            edge
            for edge in graph.semantic_edges
            if edge.get("source_id") == node_id or edge.get("target_id") == node_id
        ),
        key=lambda edge: _text(edge, "id"),
    )
    if not edges:
        return
    lines.extend(["", "## Relationships", ""])
    for edge in edges:
        lines.append(
            f"- `{_text(edge, 'type')}`: `{_text(edge, 'source_id')}` → "
            f"`{_text(edge, 'target_id')}`"
        )


def _append_flow(lines: list[str], graph: CognitiveGraph, node_id: str) -> None:
    flow = next(
        (item for item in graph.logical_flows if item.get("behavior_id") == node_id),
        None,
    )
    if flow is None:
        return
    lines.extend(
        [
            "",
            "## Flow",
            "",
            f"- Materialization: {_text(flow, 'materialization_status')}",
            f"- Flow revision: {flow.get('flow_revision')}",
        ]
    )
    for step in sorted(_objects(flow.get("steps")), key=_step_order):
        capabilities = _strings(step.get("uses_capabilities"))
        suffix = "" if not capabilities else f" (uses {', '.join(f'`{item}`' for item in capabilities)})"
        lines.append(f"- {step.get('order')}. {_title(step)}{suffix}")
        summary = step.get("summary")
        if isinstance(summary, str) and summary.strip():
            lines.append(f"  {summary}")


def _append_mappings(lines: list[str], graph: CognitiveGraph, node_id: str) -> None:
    flow_step_ids = {
        _text(step, "id")
        for flow in graph.logical_flows
        if flow.get("behavior_id") == node_id
        for step in _objects(flow.get("steps"))
    }
    mappings = sorted(
        (
            mapping
            for mapping in graph.implementation_mappings
            if mapping.get("subject_id") == node_id
            or mapping.get("subject_id") in flow_step_ids
        ),
        key=lambda mapping: _text(mapping, "id"),
    )
    if not mappings:
        return
    lines.extend(["", "## Implementation mappings", ""])
    for mapping in mappings:
        lines.append(
            f"- `{_text(mapping, 'id')}`: `{_text(mapping, 'entity_uid')}` "
            f"({mapping.get('role')}; {mapping.get('resolution_status')})"
        )
        note = mapping.get("evidence_note")
        if isinstance(note, str) and note.strip():
            lines.append(f"  {note}")


def _append_evidence(lines: list[str], graph: CognitiveGraph, node_id: str) -> None:
    node = _node_by_id(graph, node_id)
    items: list[JsonObject] = list(_objects(node.get("evidence")))
    for edge in graph.semantic_edges:
        if edge.get("source_id") == node_id or edge.get("target_id") == node_id:
            items.extend(_objects(edge.get("evidence")))
    for flow in graph.logical_flows:
        if flow.get("behavior_id") != node_id:
            continue
        items.extend(_objects(flow.get("evidence")))
        for step in _objects(flow.get("steps")):
            items.extend(_objects(step.get("evidence")))
    for mapping in graph.implementation_mappings:
        if mapping.get("subject_id") == node_id:
            items.extend(_objects(mapping.get("evidence")))
    if not items:
        return
    lines.extend(["", "## Evidence", ""])
    for evidence in sorted(items, key=lambda item: _text(item, "id")):
        path = evidence.get("relative_path")
        start = evidence.get("start_line")
        end = evidence.get("end_line")
        location = ""
        if isinstance(path, str) and path:
            location = f" — `{path}`"
            if type(start) is int:
                location += f":{start}" if start == end else f":{start}-{end}"
        observation = evidence.get("observation")
        suffix = f" — {observation}" if isinstance(observation, str) and observation else ""
        lines.append(f"- `{_text(evidence, 'id')}`{location}{suffix}")


def _node_by_id(graph: CognitiveGraph, node_id: str) -> JsonObject:
    return next(node for node in graph.nodes if node.get("id") == node_id)


def _append_approval(lines: list[str], value: Mapping[str, object]) -> None:
    approval = value.get("approval")
    if isinstance(approval, Mapping):
        event_id = approval.get("approval_event_id")
        if isinstance(event_id, str) and event_id:
            lines.append(f"- Approval event: `{event_id}`")


def _edges_by_source(
    edges: Iterable[JsonObject], edge_type: str
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("type") == edge_type:
            grouped.setdefault(_text(edge, "source_id"), []).append(_text(edge, "target_id"))
    return {source: tuple(sorted(targets)) for source, targets in grouped.items()}


def _nodes_of_kind(nodes: Iterable[JsonObject], kind: str) -> list[JsonObject]:
    return sorted(
        (node for node in nodes if node.get("kind") == kind),
        key=lambda node: _text(node, "id"),
    )


def _sorted_nodes(nodes: Iterable[JsonObject]) -> list[JsonObject]:
    return sorted(nodes, key=lambda node: _text(node, "id"))


def _tree_line(indent: str, node: Mapping[str, object]) -> str:
    return f"{indent}- `{_text(node, 'id')}` — {_title(node)}"


def _title(node: Mapping[str, object]) -> str:
    value = node.get("title")
    return value if isinstance(value, str) and value.strip() else _text(node, "id")


def _text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    return item if isinstance(item, str) else ""


def _objects(value: object) -> tuple[JsonObject, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _step_order(step: Mapping[str, object]) -> int:
    value = step.get("order")
    return value if type(value) is int else 0


def _bytes(lines: list[str]) -> bytes:
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")
