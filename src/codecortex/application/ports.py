"""Application-layer contracts for infrastructure adapters."""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, Protocol

from codecortex.domain.cognition import FormalState

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
