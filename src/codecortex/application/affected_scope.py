"""Conservative, bounded propagation from changed code facts to cognition."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from codecortex.domain.cognition import FormalState
from codecortex.domain.freshness import (
    AffectedScope,
    ChangeSet,
    ScopeConfidence,
    UnmappedChange,
)
from codecortex.infrastructure.persistence.facts_db import (
    FactEntitySnapshot,
    FactsDatabase,
)


class AffectedScopeCalculator:
    """Calculate scope from formal evidence and current facts without any Agent call."""

    def __init__(self, formal: FormalState, facts: FactsDatabase) -> None:
        self._formal = formal
        self._facts = facts

    def calculate(self, change_set: ChangeSet) -> AffectedScope:
        """Execute the fixed eight-stage algorithm with a one-hop dependency bound."""
        graph = self._formal.graph
        current = {item.uid: item for item in self._facts.current_entity_snapshots()}
        baseline = {item.uid: item for item in self._facts.baseline_entity_snapshots()}
        statuses = {item.relative_path: item for item in self._facts.source_file_statuses()}
        changed_paths = _changed_paths(change_set)
        changed_uids = _changed_entity_uids(change_set)
        diagnostic_codes = self._facts.diagnostic_codes_for_paths(
            tuple(sorted(path for path in changed_paths if path in statuses))
        ) if changed_paths & set(statuses) else {}
        entity_paths: dict[str, str] = {}
        for uid in changed_uids:
            entity = _entity_for_uid(uid, current, baseline)
            if entity is not None:
                entity_paths[uid] = entity.relative_path

        nodes: set[str] = set()
        flows: set[str] = set()
        entities: set[str] = set(changed_uids)
        unmapped: set[UnmappedChange] = set()
        diagnostics: set[str] = set()
        directly_owned: set[str] = set()

        # Stages 1-3: current/missing entities, direct mappings, and evidence.
        unowned_entities: list[str] = []
        for uid in sorted(changed_uids):
            targets = _formal_targets(graph, uid, None, nodes, flows)
            if targets:
                directly_owned.add(uid)
            else:
                unowned_entities.append(uid)
        owned_paths = {
            entity.relative_path
            for owner in directly_owned
            if (entity := _entity_for_uid(owner, current, baseline)) is not None
        }
        for uid in unowned_entities:
            entity = current.get(uid) or baseline.get(uid)
            if (
                entity is not None
                and entity.kind == "module"
                and entity.relative_path in owned_paths
            ):
                continue
            unmapped.add(
                UnmappedChange(
                    entity_paths.get(uid, "<missing-baseline-entity>"),
                    "no_formal_mapping_or_evidence",
                    uid,
                )
            )

        # A source file can be semantically relevant through path-only evidence.
        path_owned = _path_evidence_targets(graph, changed_paths, nodes, flows)
        for path in sorted(changed_paths):
            status = statuses.get(path)
            if status is None:
                if path in change_set.changed_files.deleted:
                    unmapped.add(UnmappedChange(path, "deleted_source_not_currently_parseable"))
                else:
                    unmapped.add(UnmappedChange(path, "source_file_missing_from_facts"))
                continue
            if status.parse_status != "parsed" or status.diagnostic_count:
                codes = diagnostic_codes.get(path, ())
                diagnostics.add(
                    f"{path}: parse status={status.parse_status}, diagnostics="
                    f"{','.join(codes) if codes else status.diagnostic_count}"
                )
                unmapped.add(UnmappedChange(path, "parse_diagnostic"))
                continue
            if path in change_set.changed_files.added and not _path_has_entity(path, current):
                unmapped.add(UnmappedChange(path, "new_file_has_no_formal_mapping"))
            elif not _path_has_entity(path, current) and path not in path_owned:
                # A parsed changed file with no changed entity/evidence is a
                # rule-proven implementation-irrelevant text-only difference.
                continue

        # Stages 4-6: flow-step → behavior, behavior → responsibility, and
        # capability → direct user behavior.  These are bounded graph edges.
        _propagate_semantic_graph(graph, nodes, flows)

        # Stage 7: exactly one resolved local dependency hop from directly
        # changed entities.  Never inspect relations for newly reached peers.
        if changed_uids:
            try:
                relations = self._facts.relations_touching_entity_uids(
                    tuple(sorted(changed_uids))
                )
            except ValueError:
                diagnostics.add("changed entity set exceeds one-hop relation query bound")
                unmapped.add(UnmappedChange("<repository>", "relation_query_bound"))
                relations = ()
            for relation in relations:
                if relation.resolution_status != "resolved":
                    diagnostics.add(
                        f"{relation.relation_key}: unresolved {relation.relation_type} relation"
                    )
                    continue
                neighbor = (
                    relation.target_uid
                    if relation.source_uid in changed_uids
                    else relation.source_uid
                )
                if neighbor is None or neighbor in changed_uids:
                    continue
                entities.add(neighbor)
                neighbor_path = (
                    current.get(neighbor) or baseline.get(neighbor)
                )
                targets = _formal_targets(
                    graph,
                    neighbor,
                    None,
                    nodes,
                    flows,
                )
                if not targets:
                    unmapped.add(
                        UnmappedChange(
                            "<unknown>" if neighbor_path is None else neighbor_path.relative_path,
                            "one_hop_dependency_has_no_formal_owner",
                            neighbor,
                        )
                    )
            _propagate_semantic_graph(graph, nodes, flows)

        # Stage 8: a strict confidence decision; partial cache history alone
        # does not lower confidence unless an actual proof obligation failed.
        confidence: ScopeConfidence
        if any(item.reason == "parse_diagnostic" for item in unmapped):
            confidence = "unknown"
        elif unmapped or diagnostics:
            confidence = "partial"
        else:
            confidence = "complete"
        return AffectedScope(
            affected_nodes=tuple(sorted(nodes)),
            affected_flows=tuple(sorted(flows)),
            affected_entities=tuple(sorted(entities)),
            unmapped_changes=tuple(
                sorted(
                    unmapped,
                    key=lambda item: (item.relative_path, item.entity_uid or "", item.reason),
                )
            ),
            diagnostics=tuple(sorted(diagnostics)),
            scope_confidence=confidence,
        )


def _changed_entity_uids(change_set: ChangeSet) -> set[str]:
    return {
        *change_set.changed_entities.added,
        *change_set.changed_entities.modified,
        *change_set.changed_entities.missing,
        *(item[0] for item in change_set.changed_entities.moved),
    }


def _entity_for_uid(
    uid: str,
    current: Mapping[str, FactEntitySnapshot],
    baseline: Mapping[str, FactEntitySnapshot],
) -> FactEntitySnapshot | None:
    return current.get(uid) or baseline.get(uid)


def _changed_paths(change_set: ChangeSet) -> set[str]:
    return {
        *change_set.changed_files.added,
        *change_set.changed_files.modified,
        *change_set.changed_files.deleted,
        *(path for pair in change_set.changed_files.renamed for path in pair),
    }


def _path_has_entity(path: str, entities: Mapping[str, object]) -> bool:
    return any(getattr(entity, "relative_path", None) == path for entity in entities.values())


def _formal_targets(
    graph: object,
    entity_uid: str,
    relative_path: str | None,
    nodes: set[str],
    flows: set[str],
) -> bool:
    owned = False
    for mapping in getattr(graph, "implementation_mappings", ()):
        if mapping.get("entity_uid") != entity_uid:
            continue
        subject = mapping.get("subject_id")
        if not isinstance(subject, str) or mapping.get("resolution_status") != "resolved":
            continue
        owned = True
        if mapping.get("subject_kind") == "flow_step":
            behavior = _behavior_for_step(graph, subject)
            if behavior is not None:
                flows.add(behavior)
                nodes.add(behavior)
            else:
                owned = False
        else:
            nodes.add(subject)
    for owner, owner_id, evidence in _evidence_owners(graph):
        if _evidence_matches(evidence, entity_uid, relative_path):
            owned = True
            _add_evidence_owner(graph, owner, owner_id, nodes, flows)
    return owned


def _path_evidence_targets(
    graph: object, paths: set[str], nodes: set[str], flows: set[str]
) -> set[str]:
    owned: set[str] = set()
    for owner, owner_id, evidence in _evidence_owners(graph):
        matched = {
            path
            for path in paths
            if _evidence_matches(evidence, None, path)
        }
        if matched:
            owned.update(matched)
            _add_evidence_owner(graph, owner, owner_id, nodes, flows)
    return owned


def _evidence_owners(graph: object) -> Iterable[tuple[str, str, object]]:
    for node in getattr(graph, "nodes", ()):
        identifier = node.get("id")
        if isinstance(identifier, str):
            yield "node", identifier, node.get("evidence")
    for edge in getattr(graph, "semantic_edges", ()):
        identifier = edge.get("id")
        if isinstance(identifier, str):
            yield "edge", identifier, edge.get("evidence")
    for flow in getattr(graph, "logical_flows", ()):
        behavior = flow.get("behavior_id")
        if isinstance(behavior, str):
            yield "flow", behavior, flow.get("evidence")
            for step in flow.get("steps", ()):
                identifier = step.get("id") if isinstance(step, Mapping) else None
                if isinstance(identifier, str):
                    yield "flow_step", identifier, step.get("evidence")
    for mapping in getattr(graph, "implementation_mappings", ()):
        identifier = mapping.get("id")
        if isinstance(identifier, str):
            yield "mapping", identifier, mapping.get("evidence")


def _evidence_matches(
    evidence: object,
    entity_uid: str | None,
    relative_path: str | None,
    paths: set[str] | None = None,
) -> bool:
    if not isinstance(evidence, (list, tuple)):
        return False
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        if entity_uid is not None and item.get("entity_uid") == entity_uid:
            return True
        path = item.get("relative_path")
        if relative_path is not None and path == relative_path:
            return True
        if paths is not None and path in paths:
            return True
    return False


def _add_evidence_owner(
    graph: object, owner: str, owner_id: str, nodes: set[str], flows: set[str]
) -> None:
    if owner == "node":
        nodes.add(owner_id)
    elif owner == "flow":
        flows.add(owner_id)
        nodes.add(owner_id)
    elif owner == "flow_step":
        behavior = _behavior_for_step(graph, owner_id)
        if behavior is not None:
            flows.add(behavior)
            nodes.add(behavior)
    elif owner == "edge":
        for edge in getattr(graph, "semantic_edges", ()):
            if edge.get("id") == owner_id:
                nodes.update(
                    value
                    for value in (edge.get("source_id"), edge.get("target_id"))
                    if isinstance(value, str)
                )
    elif owner == "mapping":
        for mapping in getattr(graph, "implementation_mappings", ()):
            if mapping.get("id") == owner_id:
                subject = mapping.get("subject_id")
                if isinstance(subject, str):
                    if mapping.get("subject_kind") == "flow_step":
                        behavior = _behavior_for_step(graph, subject)
                        if behavior is not None:
                            flows.add(behavior)
                            nodes.add(behavior)
                    else:
                        nodes.add(subject)


def _behavior_for_step(graph: object, step_id: str) -> str | None:
    for flow in getattr(graph, "logical_flows", ()):
        behavior = flow.get("behavior_id")
        if not isinstance(behavior, str):
            continue
        if any(
            isinstance(step, Mapping) and step.get("id") == step_id
            for step in flow.get("steps", ())
        ):
            return behavior
    return None


def _propagate_semantic_graph(graph: object, nodes: set[str], flows: set[str]) -> None:
    changed = True
    while changed:
        changed = False
        for edge in getattr(graph, "semantic_edges", ()):
            relation = edge.get("type")
            source = edge.get("source_id")
            target = edge.get("target_id")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            if relation == "contains" and target in nodes and source not in nodes:
                nodes.add(source)
                changed = True
            if relation == "uses" and target in nodes and source not in nodes:
                nodes.add(source)
                changed = True
        for flow in getattr(graph, "logical_flows", ()):
            behavior = flow.get("behavior_id")
            if not isinstance(behavior, str):
                continue
            capabilities = {
                capability
                for step in flow.get("steps", ())
                if isinstance(step, Mapping)
                for capability in step.get("uses_capabilities", ())
                if isinstance(capability, str)
            }
            if capabilities & nodes and behavior not in nodes:
                nodes.add(behavior)
                changed = True
