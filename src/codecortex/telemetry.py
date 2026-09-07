"""Disposable, allowlisted Core spans; independent of Python logging records.

No arguments, general result serialization, exception messages or source text
enter this module's output. A broken sink must never break a Core operation.
"""

# These boundaries intentionally suppress telemetry failures without logging
# exception text or recursively invoking the failed sink.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import inspect
import json
import math
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, cast

from codecortex.domain.errors import CodeCortexError

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
_COUNTS = frozenset(
    {
        "graph_revision",
        "index_generation",
        "parsed_files",
        "added_files",
        "changed_files",
        "deleted_files",
        "retry_count",
        "file_count",
        "entity_count",
        "node_count",
        "evidence_count",
        "cache_warning_count",
        "diagnostic_count",
        "truncation_count",
        "exit_code",
    }
)
_FLAGS = frozenset({"rebuilt", "truncated", "retryable"})
_ENUMS = {
    "scope_confidence": {"complete", "partial", "unknown"},
    "route": {
        "graph_current",
        "graph_unaffected",
        "source_first",
        "native_fallback",
        "offer_materialization",
    },
    "mode": {"auto", "full", "shared", "exclusive"},
}
_DIGESTS = {
    "repository_source_digest",
    "current_source_digest",
    "baseline_source_digest",
}
_current: ContextVar[tuple[str, str] | None] = ContextVar(
    "codecortex_span", default=None
)
_profile: ContextVar[str | None] = ContextVar("codecortex_profile", default=None)
_mutex = threading.RLock()


def _metrics(values: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values.items():
        if (
            key in _COUNTS
            and type(value) is int
            and value >= 0
            or key in _FLAGS
            and type(value) is bool
        ):
            result[key] = value
        elif type(value) is str:
            if (
                (key in _ENUMS and value in _ENUMS[key])
                or (key in _DIGESTS and re.fullmatch(r"sha256:[0-9a-f]{64}", value))
                or (
                    key in {"error_code", "error_class"}
                    and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", value)
                )
            ):
                result[key] = value
        elif (
            key == "wait_ms"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            result[key] = value
    return result


class _FileSink:
    """Private per-process files opened through an anchored no-follow directory."""

    def __init__(self, directory: str) -> None:
        self.directory_fd = -1
        self.file_fd = -1
        self.name = f"codecortex-{os.getpid()}-{uuid.uuid4().hex}.jsonl"
        path = Path(directory)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Log directory must be an absolute safe path")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open("/", flags)
        try:
            for component in path.parts[1:]:
                try:
                    child = os.open(component, flags, dir_fd=current)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=current)
                os.close(current)
                current = child
            info = os.fstat(current)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("Log directory must be private and owned by this user")
            self.directory_fd = current
            current = -1
            self._create()
        except Exception:
            self.close()
            raise
        finally:
            if current >= 0:
                os.close(current)

    def _create(self) -> None:
        self.file_fd = os.open(
            self.name,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        os.fchmod(self.file_fd, 0o600)

    def _validate(self, name: str) -> bool:
        try:
            info = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError("Unsafe log file")
        return True

    def write(self, payload: bytes) -> None:
        if len(payload) > MAX_BYTES:
            raise ValueError("Runtime event exceeds rotation bound")
        info = os.fstat(self.file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe log descriptor")
        if info.st_size + len(payload) > MAX_BYTES:
            names = [
                self.name,
                *(f"{self.name}.{index}" for index in range(1, BACKUP_COUNT + 1)),
            ]
            present = [self._validate(name) for name in names]
            os.close(self.file_fd)
            self.file_fd = -1
            for index in range(BACKUP_COUNT, 0, -1):
                if present[index - 1]:
                    os.replace(
                        names[index - 1],
                        names[index],
                        src_dir_fd=self.directory_fd,
                        dst_dir_fd=self.directory_fd,
                    )
            self._create()
        view = memoryview(payload)
        while view:
            written = os.write(self.file_fd, view)
            if written <= 0:
                raise OSError("Log write made no progress")
            view = view[written:]

    def close(self) -> None:
        for attribute in ("file_fd", "directory_fd"):
            descriptor = getattr(self, attribute)
            setattr(self, attribute, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class _Runtime:
    def __init__(self, profile: str = "main") -> None:
        self.profile = profile
        self.level = os.environ.get("CODECORTEX_LOG_LEVEL", "INFO").upper()
        if self.level not in {"INFO", "DEBUG", "OFF"}:
            self.level = "INFO"
        self.directory = os.environ.get("CODECORTEX_LOG_DIR")
        self.sink: _FileSink | None = None
        self.failed = False
        self.warned = False


_runtime = _Runtime()


def configure(profile: str = "main") -> None:
    """Read process configuration; never create a file until Main emits an event."""
    global _runtime
    with _mutex:
        configured = _Runtime(profile if profile in {"main", "analyzer"} else "main")
        if (_runtime.profile, _runtime.level, _runtime.directory) == (
            configured.profile,
            configured.level,
            configured.directory,
        ):
            return
        if _runtime.sink is not None:
            _runtime.sink.close()
        _runtime = configured


def _stderr(payload: str) -> None:
    try:
        sys.stderr.write(payload)
        sys.stderr.flush()
    except Exception:
        pass


def _emit(
    name: str,
    event: str,
    identity: tuple[str, str],
    parent: str | None,
    values: Mapping[str, object],
    duration: float | None = None,
    *,
    level: str = "INFO",
) -> None:
    try:
        with _mutex:
            runtime = _runtime
            if runtime.level == "OFF" or (
                level == "DEBUG" and runtime.level != "DEBUG"
            ):
                return
            profile = _profile.get() or runtime.profile
            record: dict[str, object] = {
                "schema_version": 1,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "pid": os.getpid(),
                "profile": profile,
                "operation_id": identity[0],
                "span_id": identity[1],
                "parent_id": parent,
                "name": name,
                "event": event,
                "level": level,
                "metrics": _metrics(values),
            }
            if duration is not None:
                record["duration_ms"] = round(duration, 3)
            payload = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
            if profile != "analyzer" and runtime.directory and not runtime.failed:
                try:
                    if runtime.sink is None:
                        runtime.sink = _FileSink(runtime.directory)
                    runtime.sink.write(payload.encode("utf-8"))
                    return
                except Exception:
                    runtime.failed = True
                    if runtime.sink is not None:
                        runtime.sink.close()
            if runtime.failed and not runtime.warned:
                runtime.warned = True
                warning = {
                    **record,
                    "name": "telemetry.sink",
                    "event": "warning",
                    "metrics": {"error_code": "LOG_SINK_UNAVAILABLE"},
                }
                _stderr(json.dumps(warning, separators=(",", ":")) + "\n")
            _stderr(payload)
    except Exception:
        # Includes serialization/stream failures. Never recurse into logging.
        pass


@contextmanager
def span(
    name: str, *, profile: str | None = None, level: str = "INFO", root: bool = False
) -> Generator[dict[str, object]]:
    """Measure one explicitly named operation; nested spans inherit correlation."""
    previous = None if root else _current.get()
    identity = (previous[0] if previous else uuid.uuid4().hex, uuid.uuid4().hex)
    parent = previous[1] if previous else None
    token = _current.set(identity)
    profile_token = _profile.set(profile) if profile is not None else None
    values: dict[str, object] = {}
    started = time.perf_counter()
    _emit(name, "start", identity, parent, values, level=level)
    try:
        yield values
    except BaseException as error:
        values["error_class"] = type(error).__name__
        cause: BaseException | None = error
        for _ in range(4):
            if isinstance(cause, CodeCortexError):
                values.update(error_code=cause.code.value, retryable=cause.retryable)
                break
            cause = cause.__cause__ if cause is not None else None
        _emit(
            name,
            "error",
            identity,
            parent,
            values,
            (time.perf_counter() - started) * 1000,
            level=level,
        )
        raise
    else:
        exit_code = values.get("exit_code", 0)
        outcome = "error" if type(exit_code) is int and exit_code != 0 else "success"
        _emit(
            name,
            outcome,
            identity,
            parent,
            values,
            (time.perf_counter() - started) * 1000,
            level=level,
        )
    finally:
        _current.reset(token)
        if profile_token is not None:
            _profile.reset(profile_token)


def traced[**P, R](
    name: str,
    *,
    profile: str | None = None,
    level: str = "INFO",
    root: bool = False,
    result: Callable[[Any], Mapping[str, object]] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Explicit decorator preserving signatures and coroutine nature for MCP.

    Call sites select result metadata; telemetry never enumerates result fields.
    A faulty optional selector is ignored just like a faulty output sink.
    """

    def select(value: Any, values: dict[str, object]) -> None:
        if result is not None:
            try:
                values.update(result(value))
            except Exception:
                pass

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args: P.args, **kwargs: P.kwargs) -> Any:
                with span(name, profile=profile, level=level, root=root) as values:
                    value = await function(*args, **kwargs)
                    select(value, values)
                    return value

            return cast(Callable[P, R], asynchronous)

        @wraps(function)
        def synchronous(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(name, profile=profile, level=level, root=root) as values:
                value = function(*args, **kwargs)
                select(value, values)
                return value

        return synchronous

    return decorate
