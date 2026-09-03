"""Pure value objects for managed Python source facts and portable digests."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def _normalized_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and value != "."
    )


def _valid_pattern(value: str) -> bool:
    return _normalized_relative_path(value) and not value.endswith("/")


@dataclass(frozen=True)
class SourceConfig:
    """Versioned managed-source selection rules from `.codecortex/config.toml`."""

    include: tuple[str, ...] = ("**/*.py",)
    exclude: tuple[str, ...] = ()
    respect_gitignore: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.include, tuple) or not self.include:
            raise TypeError("Managed-source include patterns must be a non-empty tuple")
        if not isinstance(self.exclude, tuple):
            raise TypeError("Managed-source exclude patterns must be a tuple")
        if not all(
            isinstance(pattern, str) and _valid_pattern(pattern)
            for pattern in (*self.include, *self.exclude)
        ):
            raise ValueError("Managed-source patterns must be normalized relative paths")
        if type(self.respect_gitignore) is not bool:
            raise ValueError("respect_gitignore must be boolean")


@dataclass(frozen=True)
class DigestProfile:
    """The versioned source-text and repository-digest algorithm selection."""

    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Digest profile version must be a positive integer")


@dataclass(frozen=True)
class SourceFileInput:
    """One discovered repository-relative source file, never executable code."""

    relative_path: str
    absolute_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.relative_path, str) or not _normalized_relative_path(
            self.relative_path
        ):
            raise ValueError("Source path must be normalized repository-relative POSIX")
        if not isinstance(self.absolute_path, Path):
            raise TypeError("Source path must be a pathlib Path")


@dataclass(frozen=True)
class SourceDiagnostic:
    """One non-fatal discovery problem, retained for a later Fact Sync run."""

    relative_path: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.relative_path, str) or not _normalized_relative_path(
            self.relative_path
        ):
            raise ValueError("Diagnostic path must be normalized repository-relative POSIX")
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("Diagnostic code must be a non-empty string")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("Diagnostic message must be a non-empty string")


@dataclass(frozen=True)
class SourceDiscoveryResult:
    """The usable managed-source set plus non-fatal candidate diagnostics."""

    sources: tuple[SourceFileInput, ...]
    diagnostics: tuple[SourceDiagnostic, ...]


@dataclass(frozen=True)
class SourceFileDigest:
    """Decoded, newline-normalized source text and its portable content digest."""

    source: SourceFileInput
    normalized_text: str
    content_digest: str
    size_bytes: int
    encoding: str

    def __post_init__(self) -> None:
        if not self.content_digest.startswith("sha256:") or len(self.content_digest) != 71:
            raise ValueError("Source content digest must be SHA-256")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("Source size must be non-negative")
