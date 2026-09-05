"""POSIX advisory locking for repository-wide coordination."""

import fcntl
import os
import stat
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
        try:
            descriptor = _open_read_only_lock(self._repository_root)
        except OSError as error:
            raise _read_only_lock_error(
                "Repository cache lock is missing or unsafe"
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
                except OSError as error:
                    raise _read_only_lock_error(
                        "Repository cache lock cannot be acquired safely"
                    ) from error
            yield
        finally:
            cleanup_error: OSError | None = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as error:
                cleanup_error = error
            finally:
                try:
                    os.close(descriptor)
                except OSError as error:
                    cleanup_error = cleanup_error or error
            if cleanup_error is not None:
                raise _read_only_lock_error(
                    "Repository cache lock could not be released safely"
                ) from cleanup_error


def _open_read_only_lock(repository_root: Path) -> int:
    """Open the fixed lock with no symlink traversal or special-file access."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(repository_root, directory_flags)
        descriptors.append(current)
        for component in (".codecortex", ".cache"):
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
        lock_descriptor = os.open(
            "repository.lock",
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=current,
        )
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            os.close(lock_descriptor)
            raise OSError("Repository cache lock is not a regular file")
        return lock_descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_only_lock_error(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.CACHE_REBUILD_REQUIRED,
        message,
        retryable=True,
        suggested_action="Ask Main CodeCortex to prepare the cache",
    )
