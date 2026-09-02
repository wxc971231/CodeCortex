"""Application-layer contracts for infrastructure adapters."""

from contextlib import AbstractContextManager
from typing import Literal, Protocol

LockMode = Literal["shared", "exclusive"]


class RepositoryLockPort(Protocol):
    """Coordinates concurrent access to one repository."""

    def acquire(
        self, mode: LockMode, timeout_seconds: float
    ) -> AbstractContextManager[None]:
        """Hold *mode* until the context exits or the timeout is reached."""
