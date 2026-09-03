"""Compatibility imports for deterministic cognitive Markdown views."""

from collections.abc import Mapping

from codecortex.domain.cognition import CognitiveGraph, JsonObject
from codecortex.infrastructure.formal import EMPTY_TREE_VIEW
from codecortex.infrastructure.rendering.markdown import render_views

__all__ = ["render_legacy_views", "render_views"]


def render_legacy_views(graph: CognitiveGraph) -> Mapping[str, bytes]:
    """Reproduce the M0 renderer solely for safe legacy-state admission.

    M1a's richer projection must never silently overwrite a hand-edited M0
    view.  This compact implementation remains isolated from the M1a renderer
    so the first migration compares against exactly the M0 byte shape.
    """
    if not graph.nodes:
        return {"views/TREE.md": EMPTY_TREE_VIEW}
    directories = {
        "responsibility": "responsibilities",
        "behavior": "behaviors",
        "capability": "capabilities",
    }
    nodes = sorted(graph.nodes, key=lambda node: str(node.get("id")))
    views: dict[str, bytes] = {"views/TREE.md": _legacy_tree(nodes, directories)}
    for node in nodes:
        kind = str(node.get("kind"))
        node_id = str(node.get("id"))
        slug = node_id.removeprefix(f"{kind}.")
        views[f"views/{directories[kind]}/{slug}.md"] = _legacy_node(node)
    return views


def _legacy_tree(nodes: list[JsonObject], directories: Mapping[str, str]) -> bytes:
    lines = ["# CodeCortex Cognitive Tree", ""]
    for kind in ("responsibility", "behavior", "capability"):
        selected = [node for node in nodes if node.get("kind") == kind]
        if not selected:
            continue
        lines.extend([f"## {directories[kind].capitalize()}", ""])
        for node in selected:
            lines.append(f"- `{node.get('id')}` — {_legacy_title(node)}")
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _legacy_node(node: JsonObject) -> bytes:
    lines = [
        f"# {_legacy_title(node)}",
        "",
        f"- ID: `{node.get('id')}`",
        f"- Kind: {node.get('kind')}",
    ]
    if type(node.get("node_revision")) is int:
        lines.append(f"- Node revision: {node['node_revision']}")
    approval = node.get("approval")
    if isinstance(approval, dict) and isinstance(
        approval.get("approval_event_id"), str
    ):
        lines.append(f"- Approval event: `{approval['approval_event_id']}`")
    summary = node.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.extend(["", summary])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _legacy_title(node: JsonObject) -> str:
    title = node.get("title")
    return title if isinstance(title, str) and title.strip() else str(node.get("id"))
