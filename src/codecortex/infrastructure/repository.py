"""Repository discovery and safe repository-relative path handling."""

import os
from dataclasses import dataclass
from pathlib import Path

from codecortex.domain.errors import CodeCortexError, ErrorCode

_VALID_GIT_MARKERS: tuple[str, ...] = ("HEAD", "objects", "refs")


def _outside_repository(root: Path, path: Path) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PATH_OUTSIDE_REPOSITORY,
        f"Path is outside repository: {path}",
        details={"repository_root": root.as_posix()},
    )


def _is_valid_git_dir(path: Path) -> bool:
    if not path.is_dir() or not (path / "HEAD").is_file():
        return False
    if all((path / marker).exists() for marker in _VALID_GIT_MARKERS[1:]):
        return True
    common_dir_file = path / "commondir"
    if not common_dir_file.is_file():
        return False
    common_dir = (path / common_dir_file.read_text(encoding="utf-8").strip()).resolve()
    return all((common_dir / marker).exists() for marker in _VALID_GIT_MARKERS[1:])


@dataclass(frozen=True)
class Repository:
    """A repository identified by its resolved Git root."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def _resolve_inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            inside = os.path.commonpath((str(self.root), str(resolved))) == str(self.root)
        except ValueError:
            inside = False
        if not inside:
            raise _outside_repository(self.root, resolved)
        return resolved

    def resolve_relative(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise _outside_repository(self.root, candidate)
        return self._resolve_inside(self.root / candidate)

    def to_relative(self, path: Path) -> str:
        candidate = path if path.is_absolute() else self.root / path
        resolved = self._resolve_inside(candidate)
        return resolved.relative_to(self.root).as_posix()


def find_repository(start: Path) -> Repository:
    """Find the nearest Git root at or above *start*."""

    start = Path(start)
    cursor = (start if start.is_dir() else start.parent).resolve()
    for candidate in (cursor, *cursor.parents):
        git_dir_candidate = candidate / ".git"
        if not git_dir_candidate.exists():
            continue
        if git_dir_candidate.is_file():
            raw = git_dir_candidate.read_text(encoding="utf-8").strip()
            if not raw.startswith("gitdir:"):
                continue
            resolved_gitdir = raw.removeprefix("gitdir:").strip()
            git_dir = (candidate / resolved_gitdir).resolve()
            if _is_valid_git_dir(git_dir):
                return Repository(candidate)
            continue
        if _is_valid_git_dir(git_dir_candidate):
            return Repository(candidate)
    raise CodeCortexError(ErrorCode.NOT_INITIALIZED, "No Git repository found")
