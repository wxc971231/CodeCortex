"""Application-layer contracts for infrastructure adapters."""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, Protocol

from codecortex.domain.cognition import FormalState
from codecortex.domain.proposals import Proposal

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


class FormalStorePort(Protocol):
    """Load and initialize complete formal snapshots."""

    def initialize(self, state: FormalState) -> FormalState:
        """Create initial formal state or load an existing valid state."""

    def load(self) -> FormalState:
        """Load one complete validated formal-state snapshot."""

    def formal_file_presence(self) -> dict[str, bool]:
        """Return required formal-file existence by repository-relative path."""


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
