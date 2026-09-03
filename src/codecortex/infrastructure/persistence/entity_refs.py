"""Recompute formal ``entity_refs.json`` content from graph references.

Design section 9: the formal entity-reference set holds exactly the UIDs the
formal graph actually references, recomputed from all mappings and embedded
evidence at every apply; unreferenced entities are dropped.  References that
no longer resolve against the current facts keep their last-known record from
the previous formal state (marked ``missing``) so cache rebuilds can still
recover the formal UID; a reference unknown to both fails closed.
"""

from collections.abc import Mapping

from codecortex.domain.cognition import SCHEMA_VERSION, CognitiveGraph, EntityRefs
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.persistence.facts_db import FactsDatabase


def referenced_entity_uids(graph: CognitiveGraph) -> frozenset[str]:
    """Collect every entity UID the formal graph sections reference."""
    uids: set[str] = set()
    for section in (
        graph.nodes,
        graph.semantic_edges,
        graph.logical_flows,
        graph.implementation_mappings,
    ):
        _collect_uids(section, uids)
    return frozenset(uids)


def recompute_entity_refs(
    graph: CognitiveGraph,
    graph_revision: int,
    *,
    facts: FactsDatabase,
    previous: EntityRefs,
) -> EntityRefs:
    """Rebuild the referenced-entity set for one applied graph revision.

    Records are refreshed from the current fact cache whenever the entity
    still exists, so fingerprint and address are re-verified against the exact
    source snapshot the apply re-validated.  UIDs that disappeared from the
    facts carry their previous last-known record forward as ``missing``; UIDs
    unknown to both are rejected before anything is written.
    """
    if type(graph_revision) is not int or graph_revision < 1:
        raise ValueError("Graph revision must be a positive integer")
    previous_by_uid = {
        str(entry.get("uid")): entry for entry in previous.entities
    }
    records: list[dict[str, object]] = []
    unknown: list[str] = []
    for uid in sorted(referenced_entity_uids(graph)):
        entity = facts.entity_by_uid(uid)
        if entity is not None:
            records.append(
                {
                    "uid": entity.uid,
                    "last_known_address": entity.address,
                    "kind": entity.kind,
                    "relative_path": entity.relative_path,
                    "signature": entity.signature,
                    "fingerprint": entity.fingerprint,
                    "resolution_status": entity.resolution_status,
                }
            )
            continue
        carried = previous_by_uid.get(uid)
        if carried is None:
            unknown.append(uid)
            continue
        records.append({**dict(carried), "resolution_status": "missing"})
    if unknown:
        raise CodeCortexError(
            ErrorCode.ANALYSIS_REPORT_INVALID,
            "Graph references entities unknown to the current facts and the "
            "previous formal entity references",
            details={"entity_uids": unknown},
            suggested_action="Re-run the analysis against the current facts",
        )
    return EntityRefs(SCHEMA_VERSION, graph_revision, tuple(records))


def _collect_uids(value: object, uids: set[str]) -> None:
    if isinstance(value, Mapping):
        uid = value.get("entity_uid")
        if isinstance(uid, str):
            uids.add(uid)
        for item in value.values():
            _collect_uids(item, uids)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_uids(item, uids)
