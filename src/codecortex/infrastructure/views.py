"""Deterministic Markdown projections of the canonical cognitive graph."""

from collections.abc import Mapping

from codecortex.domain.cognition import CognitiveGraph, JsonObject
from codecortex.infrastructure.formal import EMPTY_TREE_VIEW

_KIND_DIRECTORIES = {
    "responsibility": "responsibilities",
    "behavior": "behaviors",
    "capability": "capabilities",
}
_KIND_ORDER = ("responsibility", "behavior", "capability")


def render_views(graph: CognitiveGraph) -> Mapping[str, bytes]:
    """Render the complete human-readable view set for one graph revision.

    The returned mapping is the authoritative desired content of ``views/``:
    commit replaces these files and removes any managed view file not listed.
    The same graph always renders the same bytes on any machine.
    """
    if not graph.nodes:
        return {"views/TREE.md": EMPTY_TREE_VIEW}
    views: dict[str, bytes] = {"views/TREE.md": _render_tree(graph)}
    for node in _sorted_nodes(graph.nodes):
        node_id = str(node.get("id"))
        kind = str(node.get("kind"))
        slug = node_id.removeprefix(f"{kind}.")
        views[f"views/{_KIND_DIRECTORIES[kind]}/{slug}.md"] = _render_node(node)
    return views


def _sorted_nodes(nodes: tuple[JsonObject, ...]) -> list[JsonObject]:
    return sorted(nodes, key=lambda node: str(node.get("id")))


def _node_title(node: JsonObject) -> str:
    title = node.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return str(node.get("id"))


def _render_tree(graph: CognitiveGraph) -> bytes:
    lines = ["# CodeCortex Cognitive Tree", ""]
    for kind in _KIND_ORDER:
        nodes = [
            node for node in _sorted_nodes(graph.nodes) if node.get("kind") == kind
        ]
        if not nodes:
            continue
        lines.append(f"## {_KIND_DIRECTORIES[kind].capitalize()}")
        lines.append("")
        for node in nodes:
            lines.append(f"- `{node.get('id')}` — {_node_title(node)}")
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _render_node(node: JsonObject) -> bytes:
    lines = [
        f"# {_node_title(node)}",
        "",
        f"- ID: `{node.get('id')}`",
        f"- Kind: {node.get('kind')}",
    ]
    node_revision = node.get("node_revision")
    if type(node_revision) is int:
        lines.append(f"- Node revision: {node_revision}")
    approval = node.get("approval")
    if isinstance(approval, dict):
        event_id = approval.get("approval_event_id")
        if isinstance(event_id, str):
            lines.append(f"- Approval event: `{event_id}`")
    summary = node.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.extend(["", summary])
    return ("\n".join(lines) + "\n").encode("utf-8")
