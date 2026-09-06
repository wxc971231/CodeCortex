"""POSIX advisory locking for repository-wide coordination."""

import errno
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
        try:
            descriptor = _open_or_create_lock(self._repository_root)
        except OSError as error:
            if error.errno in _UNSAFE_PATH_ERRNOS:
                raise _unsafe_main_lock_error() from error
            raise

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
        try:
            is_regular = stat.S_ISREG(os.fstat(lock_descriptor).st_mode)
        except OSError:
            os.close(lock_descriptor)
            raise
        if not is_regular:
            os.close(lock_descriptor)
            raise OSError("Repository cache lock is not a regular file")
        return lock_descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_LOCK_FLAGS = (
    os.O_CREAT
    | os.O_RDWR
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_UNSAFE_PATH_ERRNOS = {
    errno.EINVAL,
    errno.EISDIR,
    errno.ELOOP,
    errno.ENOTDIR,
}


def _open_or_create_lock(repository_root: Path) -> int:
    """Open Main's lock through anchored no-follow directory descriptors."""
    descriptors: list[int] = []
    lock_descriptor: int | None = None
    try:
        descriptors.append(os.open(repository_root, _DIRECTORY_FLAGS))
        for component in (".codecortex", ".cache"):
            parent = descriptors[-1]
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            descriptors.append(child)
        lock_descriptor = os.open(
            "repository.lock", _LOCK_FLAGS, 0o600, dir_fd=descriptors[-1]
        )
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            raise OSError(errno.EINVAL, "Repository lock is not a regular file")
        return lock_descriptor
    except BaseException:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unsafe_main_lock_error() -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PATH_OUTSIDE_REPOSITORY,
        "Repository cache lock path is unsafe",
        suggested_action="Replace repository cache symlinks with local directories",
    )


def _read_only_lock_error(message: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.CACHE_REBUILD_REQUIRED,
        message,
        retryable=True,
        suggested_action="Ask Main CodeCortex to prepare the cache",
    )
