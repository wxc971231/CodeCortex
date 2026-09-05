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


class ReadOnlyRepositoryLock:
    """Acquire shared locks without creating or modifying cache state.

    Main owns creation of the stable lock file. An Analyzer process may join
    that lock only when it already exists; otherwise it fails closed and asks
    Main to prepare the disposable cache coordinate.
    """

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root

    @contextmanager
    def acquire(
        self, mode: LockMode, timeout_seconds: float
    ) -> Generator[None]:
        if mode != "shared":
            raise CodeCortexError(
                ErrorCode.CACHE_REBUILD_REQUIRED,
                "Read-only Analyzer cannot acquire an exclusive repository lock",
                suggested_action="Ask Main CodeCortex to prepare the cache",
            )
        lock_path = self._repository_root / ".codecortex" / ".cache" / "repository.lock"
        try:
            descriptor = os.open(lock_path, os.O_RDONLY)
        except FileNotFoundError as error:
            raise CodeCortexError(
                ErrorCode.CACHE_REBUILD_REQUIRED,
                "Repository cache lock is missing",
                retryable=True,
                suggested_action="Ask Main CodeCortex to prepare the cache",
            ) from error

        try:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
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
