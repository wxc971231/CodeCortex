"""Providers that derive cognitive-replica inputs from live formal state.

The replica is rebuilt at initialization and after every applied proposal.
These callables are evaluated at rebuild time, so the replica always imports
the current formal entity references and history instead of a stale snapshot.
"""

from __future__ import annotations

from collections.abc import Callable

from codecortex.application.ports import FormalStorePort
from codecortex.domain.cognition import HistoryEventRef
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.persistence.graph_replica import (
    EntityRefRecord,
    HistoryEventRecord,
)


def formal_entity_ref_provider(
    formal_store: FormalStorePort,
) -> Callable[[], tuple[EntityRefRecord, ...]]:
    """Return a provider reading the current formal entity references."""

    def provide() -> tuple[EntityRefRecord, ...]:
        state = formal_store.load()
        return tuple(
            EntityRefRecord(
                uid=str(entry["uid"]),
                last_known_address=str(entry["last_known_address"]),
                kind=str(entry["kind"]),
                relative_path=str(entry["relative_path"]),
                signature=(
                    None
                    if entry.get("signature") is None
                    else str(entry["signature"])
                ),
                fingerprint=str(entry["fingerprint"]),
                resolution_status=str(entry["resolution_status"]),
            )
            for entry in state.entity_refs.entities
        )

    return provide


def formal_history_event_provider(
    formal_store: FormalStorePort,
) -> Callable[[], tuple[HistoryEventRecord, ...]]:
    """Return a provider reading the current formal history events."""

    def provide() -> tuple[HistoryEventRecord, ...]:
        state = formal_store.load()
        return tuple(
            _history_record(formal_store, ref) for ref in state.history_events
        )

    return provide


def _history_record(
    formal_store: FormalStorePort, ref: HistoryEventRef
) -> HistoryEventRecord:
    event = formal_store.read_history_event(ref.event_id)
    revision = event.get("graph_revision")
    applied_at = event.get("applied_at")
    if type(revision) is not int or not isinstance(applied_at, str):
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            "History event is missing its revision or timestamp",
            details={"event_id": ref.event_id},
        )
    return HistoryEventRecord(
        event_id=ref.event_id,
        event_type=ref.event_type,
        resulting_graph_revision=revision,
        created_at=applied_at,
        relative_path=f"history/events/{ref.event_id}.json",
    )
