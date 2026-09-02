"""Process-level behavior tests for the POSIX repository lock."""

import multiprocessing
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure import locking
from codecortex.infrastructure.locking import RepositoryLock


def hold_lock(repo_path: str, mode: str, ready: Event, release: Event) -> None:
    """Acquire a real lock in a child process until its parent releases it."""
    with RepositoryLock(Path(repo_path)).acquire(mode, timeout_seconds=1):
        ready.set()
        release.wait(5)


def stop_holder(holder: multiprocessing.Process, release: Event) -> None:
    """Release a test holder, force-stopping it only if its cleanup failed."""
    release.set()
    holder.join(2)
    if holder.is_alive():
        holder.terminate()
        holder.join(2)
    assert holder.exitcode == 0


@pytest.fixture
def repo_path(tmp_path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    return repository


def test_shared_reader_allows_another_shared_reader(repo_path: Path) -> None:
    """A mistaken exclusive shared-lock flag would prevent this second reader."""
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    reader = multiprocessing.Process(
        target=hold_lock,
        args=(str(repo_path), "shared", ready, release),
    )
    reader.start()
    try:
        assert ready.wait(2)
        with RepositoryLock(repo_path).acquire("shared", timeout_seconds=0.05):
            assert reader.is_alive()
    finally:
        stop_holder(reader, release)


def test_exclusive_writer_times_out_behind_shared_reader(repo_path: Path) -> None:
    """Removing writer exclusion would let a writer enter beside a reader."""
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    reader = multiprocessing.Process(
        target=hold_lock,
        args=(str(repo_path), "shared", ready, release),
    )
    reader.start()
    try:
        assert ready.wait(2)
        with (
            pytest.raises(CodeCortexError) as exc,
            RepositoryLock(repo_path).acquire("exclusive", timeout_seconds=0.05),
        ):
            pass
        assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    finally:
        stop_holder(reader, release)


def test_exclusive_writer_times_out_behind_exclusive_writer(repo_path: Path) -> None:
    """Dropping exclusive exclusion would allow two concurrent writers."""
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    writer = multiprocessing.Process(
        target=hold_lock,
        args=(str(repo_path), "exclusive", ready, release),
    )
    writer.start()
    try:
        assert ready.wait(2)
        with (
            pytest.raises(CodeCortexError) as exc,
            RepositoryLock(repo_path).acquire("exclusive", timeout_seconds=0.05),
        ):
            pass
        assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    finally:
        stop_holder(writer, release)


def test_killed_lock_holder_releases_exclusive_lock(repo_path: Path) -> None:
    """Keeping a stale lock after a process crash would block the next writer."""
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    writer = multiprocessing.Process(
        target=hold_lock,
        args=(str(repo_path), "exclusive", ready, release),
    )
    writer.start()
    try:
        assert ready.wait(2)
        writer.kill()
        writer.join(2)
        assert writer.exitcode is not None
        with RepositoryLock(repo_path).acquire("exclusive", timeout_seconds=0.5):
            pass
    finally:
        if writer.is_alive():
            writer.kill()
            writer.join(2)


def test_lock_file_is_private_and_persists_after_release(repo_path: Path) -> None:
    """Deleting or exposing the lock file would break safe later coordination."""
    lock_path = repo_path / ".codecortex" / ".cache" / "repository.lock"

    with RepositoryLock(repo_path).acquire("exclusive", timeout_seconds=0.05):
        assert lock_path.stat().st_mode & 0o777 == 0o600

    assert lock_path.is_file()


def test_permission_setting_failure_closes_lock_descriptor(
    repo_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission-setting failure must not leak the newly opened descriptor."""
    closed_descriptors: list[int] = []
    close_descriptor = locking.os.close

    def fail_permission_setting(*_args: object) -> None:
        raise OSError("permission setting failed")

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        close_descriptor(descriptor)

    monkeypatch.setattr(locking.os, "chmod", fail_permission_setting)
    monkeypatch.setattr(locking.os, "fchmod", fail_permission_setting)
    monkeypatch.setattr(locking.os, "close", record_close)

    with (
        pytest.raises(OSError, match="permission setting failed"),
        RepositoryLock(repo_path).acquire("exclusive", timeout_seconds=0.05),
    ):
        pass

    assert len(closed_descriptors) == 1


def test_unlock_failure_still_closes_lock_descriptor(
    repo_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlock error must not prevent the descriptor from being closed."""
    closed_descriptors: list[int] = []
    close_descriptor = locking.os.close
    flock = locking.fcntl.flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == locking.fcntl.LOCK_UN:
            raise OSError("unlock failed")
        flock(descriptor, operation)

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        close_descriptor(descriptor)

    monkeypatch.setattr(locking.fcntl, "flock", fail_unlock)
    monkeypatch.setattr(locking.os, "close", record_close)

    with (
        pytest.raises(OSError, match="unlock failed"),
        RepositoryLock(repo_path).acquire("exclusive", timeout_seconds=0.05),
    ):
        pass

    assert len(closed_descriptors) == 1
