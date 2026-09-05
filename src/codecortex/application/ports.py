"""Application-layer contracts for infrastructure adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from codecortex.domain.cognition import CognitiveGraph, FormalState
from codecortex.domain.proposals import Proposal

if TYPE_CHECKING:
    from codecortex.application.fact_sync import FactSyncResult

LockMode = Literal["shared", "exclusive"]


class RepositoryLockPort(Protocol):
    """Coordinates concurrent access to one repository."""

    def acquire(
        self, mode: LockMode, timeout_seconds: float
    ) -> AbstractContextManager[None]:
        """Hold *mode* until the context exits or the timeout is reached."""


class RepositoryContextPort(Protocol):
    """Expose the stable root needed by repository-scoped application results."""

    root: Path


class FactSyncPort(Protocol):
    """The deterministic Main-process operation that refreshes fact cache state."""

    def sync(self, mode: Literal["auto", "full"] = "auto") -> FactSyncResult:
        """Return the committed cache-generation result for one source snapshot."""

    def probe_source_digest(self) -> str:
        """Hash the live managed source set without mutating cache state."""


ViewRendererPort = Callable[[CognitiveGraph], Mapping[str, bytes]]
"""Render the deterministic view set for one committed graph revision."""


@dataclass(frozen=True)
class RecoveryResult:
    """The outcome of resolving interrupted formal transactions at startup."""

    visible_revision: int
    is_internally_consistent: bool
    restored_transactions: tuple[str, ...] = ()
    completed_transactions: tuple[str, ...] = ()


class FormalStorePort(Protocol):
    """Load, initialize, and atomically commit complete formal snapshots."""

    def initialize(self, state: FormalState) -> FormalState:
        """Create initial formal state or load an existing valid state."""

    def load(self) -> FormalState:
        """Load one complete validated formal-state snapshot."""

    def read_history_event(self, event_id: str) -> dict[str, object]:
        """Read one immutable event after validating the formal state."""

    def formal_file_presence(self) -> dict[str, bool]:
        """Return required formal-file existence by repository-relative path."""

    def verify_legacy_views(self, expected_views: Mapping[str, bytes]) -> None:
        """Reject hand-modified M0 views before their first M1a migration."""

    def commit(
        self,
        state: FormalState,
        event: Mapping[str, object],
        views: Mapping[str, bytes],
    ) -> None:
        """Commit one validated formal revision as a journaled transaction."""

    def commit_baseline_advance(
        self, state: FormalState, event: Mapping[str, object]
    ) -> None:
        """Commit a formal source-baseline advance without a graph revision change."""

    def recover(self) -> RecoveryResult:
        """Resolve interrupted transactions to a complete old or new revision."""


class PendingProposalStorePort(Protocol):
    """Persist the one current snapshot for each machine-local pending proposal."""

    def create(self, proposal: Proposal) -> None:
        """Persist a new proposal without replacing an existing identity."""

    def load(self, proposal_id: str) -> Proposal:
        """Load and validate the current pending proposal state."""

    def replace(self, proposal: Proposal) -> None:
        """Atomically replace an existing proposal's current state."""

    def delete(self, proposal_id: str) -> None:
        """Remove a consumed pending proposal if present."""
