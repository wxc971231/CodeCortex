"""Deterministic discovery of the versioned managed Python source set."""

import fnmatch
import os
import subprocess
from pathlib import PurePosixPath

from codecortex.domain.facts import (
    SourceConfig,
    SourceDiagnostic,
    SourceDiscoveryResult,
    SourceFileInput,
)
from codecortex.infrastructure.repository import Repository

_ALWAYS_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".codecortex",
        ".venv",
        "venv",
        "env",
        "build",
        "dist",
        "site-packages",
        "__pycache__",
    }
)


class SourceDiscoveryError(ValueError):
    """A discovered candidate cannot safely become a managed source file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def discover_python_sources(
    repository: Repository, config: SourceConfig
) -> tuple[SourceFileInput, ...]:
    """Return sorted, safe Python files tracked or visible to Git.

    This compatibility convenience API deliberately omits non-fatal diagnostics.
    Fact Sync callers should use :func:`discover_python_source_set` instead.
    """
    return discover_python_source_set(repository, config).sources


def discover_python_source_set(
    repository: Repository, config: SourceConfig
) -> SourceDiscoveryResult:
    """Discover the managed-source set and isolate bad individual candidates.

    The Git invocation is intentionally argument-only (never a shell string),
    includes untracked files, and handles ignore behavior from the versioned
    source configuration. Paths are sorted by UTF-8 bytes, not locale.
    """
    command = ["git", "ls-files", "-z", "--cached", "--others"]
    if config.respect_gitignore:
        command.append("--exclude-standard")
    command.extend(("--", "*.py"))
    completed = subprocess.run(
        command,
        cwd=repository.root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SourceDiscoveryError(
            "SOURCE_DISCOVERY_FAILED", f"Git source discovery failed: {detail}"
        )

    paths = _git_paths(completed.stdout)
    candidates = {
        relative
        for relative in paths
        if _is_managed(relative, config)
    }
    sources: list[SourceFileInput] = []
    diagnostics: list[SourceDiagnostic] = []
    for relative in sorted(candidates, key=lambda value: value.encode("utf-8")):
        try:
            sources.append(_source_input(repository, relative))
        except SourceDiscoveryError as error:
            diagnostics.append(
                SourceDiagnostic(
                    relative_path=relative,
                    code=error.code,
                    message=error.message,
                )
            )
    return SourceDiscoveryResult(
        sources=tuple(sources), diagnostics=tuple(diagnostics)
    )


def _git_paths(payload: bytes) -> tuple[str, ...]:
    paths: list[str] = []
    for item in payload.split(b"\0"):
        if not item:
            continue
        try:
            relative = item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceDiscoveryError(
                "SOURCE_PATH_ENCODING_INVALID", "Git returned a non-UTF-8 source path"
            ) from error
        path = PurePosixPath(relative)
        if (
            not relative
            or "\\" in relative
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != relative
        ):
            raise SourceDiscoveryError(
                "SOURCE_PATH_UNSAFE", f"Git returned an unsafe source path: {relative}"
            )
        paths.append(relative)
    return tuple(paths)


def _is_managed(relative: str, config: SourceConfig) -> bool:
    parts = PurePosixPath(relative).parts
    if any(part in _ALWAYS_EXCLUDED_PARTS for part in parts):
        return False
    return any(_matches(relative, pattern) for pattern in config.include) and not any(
        _matches(relative, pattern) for pattern in config.exclude
    )


def _matches(relative: str, pattern: str) -> bool:
    """Match a POSIX glob while making leading `**/` include root files too."""
    path = PurePosixPath(relative)
    if path.match(pattern) or fnmatch.fnmatchcase(relative, pattern):
        return True
    if pattern.startswith("**/"):
        return _matches(relative, pattern.removeprefix("**/"))
    if pattern.endswith("/**"):
        prefix = pattern.removesuffix("/**")
        return relative.startswith(f"{prefix}/")
    return False


def _source_input(repository: Repository, relative: str) -> SourceFileInput:
    candidate = repository.root / PurePosixPath(relative)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SourceDiscoveryError(
            "SOURCE_PATH_UNREADABLE", f"Cannot resolve source path: {relative}"
        ) from error
    try:
        inside = os.path.commonpath((str(repository.root), str(resolved))) == str(
            repository.root
        )
    except ValueError:
        inside = False
    if not inside:
        raise SourceDiscoveryError(
            "SOURCE_SYMLINK_OUTSIDE_REPOSITORY",
            f"Source symlink points outside repository: {relative}",
        )
    if not resolved.is_file():
        raise SourceDiscoveryError(
            "SOURCE_NOT_REGULAR_FILE",
            f"Managed source is not a regular file: {relative}",
        )
    return SourceFileInput(relative, candidate)
