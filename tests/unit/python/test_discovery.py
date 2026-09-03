"""Managed Python source discovery against real Git repositories."""

import subprocess
from pathlib import Path

import pytest

from codecortex.domain.facts import SourceConfig
from codecortex.infrastructure.python.discovery import (
    discover_python_source_set,
    discover_python_sources,
)
from codecortex.infrastructure.repository import Repository


@pytest.fixture
def git_repository(tmp_path: Path) -> Repository:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return Repository(tmp_path)


def write(path: Path, relative: str, content: str) -> None:
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_discovery_includes_untracked_and_excludes_ignored(
    git_repository: Repository,
) -> None:
    write(git_repository.root, ".gitignore", "ignored.py\n")
    write(git_repository.root, "tracked.py", "x = 1\n")
    write(git_repository.root, "untracked.py", "y = 2\n")
    write(git_repository.root, "ignored.py", "z = 3\n")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.py"],
        cwd=git_repository.root,
        check=True,
    )

    paths = [
        item.relative_path
        for item in discover_python_sources(git_repository, SourceConfig())
    ]

    assert paths == ["tracked.py", "untracked.py"]


def test_discovery_applies_versioned_include_exclude_and_posix_sorting(
    git_repository: Repository,
) -> None:
    write(git_repository.root, "root.py", "x = 1\n")
    write(git_repository.root, "src/a.py", "x = 1\n")
    write(git_repository.root, "src/generated/skip.py", "x = 1\n")
    write(git_repository.root, "tests/test_a.py", "x = 1\n")

    paths = [
        item.relative_path
        for item in discover_python_sources(
            git_repository,
            SourceConfig(include=("**/*.py",), exclude=("src/generated/**",)),
        )
    ]

    assert paths == ["root.py", "src/a.py", "tests/test_a.py"]


def test_discovery_can_include_gitignored_files_when_configured(
    git_repository: Repository,
) -> None:
    write(git_repository.root, ".gitignore", "ignored.py\n")
    write(git_repository.root, "ignored.py", "x = 1\n")

    paths = [
        item.relative_path
        for item in discover_python_sources(
            git_repository, SourceConfig(respect_gitignore=False)
        )
    ]

    assert paths == ["ignored.py"]


def test_discovery_always_excludes_codecortex_state(
    git_repository: Repository,
) -> None:
    write(git_repository.root, ".codecortex/generated.py", "x = 1\n")
    write(git_repository.root, "real.py", "x = 1\n")

    paths = [
        item.relative_path
        for item in discover_python_sources(git_repository, SourceConfig())
    ]

    assert paths == ["real.py"]


def test_discovery_skips_external_symlink_and_reports_diagnostic(
    git_repository: Repository, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside = True\n", encoding="utf-8")
    (git_repository.root / "external.py").symlink_to(outside)
    write(git_repository.root, "real.py", "inside = True\n")

    result = discover_python_source_set(git_repository, SourceConfig())

    assert [source.relative_path for source in result.sources] == ["real.py"]
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.relative_path == "external.py"
    assert diagnostic.code == "SOURCE_SYMLINK_OUTSIDE_REPOSITORY"
    assert diagnostic.message == "Source symlink points outside repository: external.py"
