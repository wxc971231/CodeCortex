"""POSIX advisory locking for repository-wide coordination."""

import fcntl
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from codecortex.application.ports import LockMode
from codecortex.domain.errors import CodeCortexError, ErrorCode


class RepositoryLock:
    """Acquire shared or exclusive advisory locks for one repository."""

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root

    @contextmanager
    def acquire(
        self, mode: LockMode, timeout_seconds: float
    ) -> Generator[None]:
        """Hold a repository lock, releasing its descriptor on every exit path."""
        lock_path = self._repository_root / ".codecortex" / ".cache" / "repository.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)

        try:
            os.fchmod(descriptor, 0o600)
            flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(descriptor, flag | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CodeCortexError(
                            ErrorCode.LOCK_TIMEOUT,
                            "Repository lock timed out",
                        )
                    time.sleep(0.01)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
