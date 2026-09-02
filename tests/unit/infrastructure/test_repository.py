import tempfile
from pathlib import Path

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.repository import Repository, find_repository


def test_finds_nearest_git_root_and_normalizes_paths(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)

    repo = find_repository(nested)

    assert repo.root == root.resolve()
    assert repo.to_relative(root / "src" / "pkg") == "src/pkg"


def test_file_start_uses_file_parent(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    source = root / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n")

    assert find_repository(source).root == root.resolve()


def test_finds_nearest_nested_git_root(tmp_path):
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "vendor" / "inner"
    (inner / ".git").mkdir(parents=True)
    nested = inner / "src"
    nested.mkdir()

    assert find_repository(nested).root == inner.resolve()


def test_rejects_parent_escape(repository):
    with pytest.raises(CodeCortexError) as exc:
        repository.resolve_relative("../outside.py")

    assert exc.value.code is ErrorCode.PATH_OUTSIDE_REPOSITORY


def test_rejects_absolute_input(repository):
    with pytest.raises(CodeCortexError) as exc:
        repository.resolve_relative(str(repository.root / "inside.py"))

    assert exc.value.code is ErrorCode.PATH_OUTSIDE_REPOSITORY


def test_rejects_symlink_escape(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository.root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CodeCortexError) as exc:
        repository.resolve_relative("link/secret.py")

    assert exc.value.code is ErrorCode.PATH_OUTSIDE_REPOSITORY


def test_rejects_path_to_external_file(repository, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n")

    with pytest.raises(CodeCortexError) as exc:
        repository.to_relative(outside)

    assert exc.value.code is ErrorCode.PATH_OUTSIDE_REPOSITORY


def test_missing_git_root_is_not_initialized():
    with tempfile.TemporaryDirectory(dir="/dev/shm") as temporary:
        start = Path(temporary) / "not-a-repo" / "src"
        start.mkdir(parents=True)

        with pytest.raises(CodeCortexError) as exc:
            find_repository(start)

    assert exc.value.code is ErrorCode.NOT_INITIALIZED


@pytest.fixture
def repository(tmp_path) -> Repository:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return Repository(root)
