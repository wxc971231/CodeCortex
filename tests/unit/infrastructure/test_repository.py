import subprocess
from pathlib import Path

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.repository import Repository, find_repository


def _make_git_root(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_finds_nearest_git_root_and_normalizes_paths(tmp_path):
    root = tmp_path / "repo"
    _make_git_root(root)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)

    repo = find_repository(nested)

    assert repo.root == root.resolve()
    assert repo.to_relative(root / "src" / "pkg") == "src/pkg"


def test_file_start_uses_file_parent(tmp_path):
    root = tmp_path / "repo"
    _make_git_root(root)
    source = root / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n")

    assert find_repository(source).root == root.resolve()


def test_finds_nearest_nested_git_root(tmp_path):
    outer = tmp_path / "outer"
    _make_git_root(outer)
    inner = outer / "vendor" / "inner"
    _make_git_root(inner)
    nested = inner / "src"
    nested.mkdir()

    assert find_repository(nested).root == inner.resolve()


def test_finds_linked_git_worktree_as_its_own_root(tmp_path: Path) -> None:
    main = tmp_path / "main"
    _make_git_root(main)
    (main / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=CodeCortex Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(linked)],
        check=True,
    )

    assert find_repository(linked / "README.md").root == linked.resolve()


def test_skips_non_utf8_git_indirection_metadata(tmp_path: Path) -> None:
    root = tmp_path / "invalid-gitdir"
    root.mkdir()
    (root / ".git").write_bytes(b"gitdir: \xff\n")

    with pytest.raises(CodeCortexError) as exc:
        find_repository(root)

    assert exc.value.code is ErrorCode.NOT_INITIALIZED


@pytest.mark.parametrize("contents", (b"\xff\n", b"\x00\n"))
def test_skips_invalid_linked_worktree_common_dir_metadata(
    tmp_path: Path, contents: bytes
) -> None:
    root = tmp_path / "invalid-commondir"
    root.mkdir()
    git_dir = tmp_path / "metadata"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "commondir").write_bytes(contents)
    (root / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    with pytest.raises(CodeCortexError) as exc:
        find_repository(root)

    assert exc.value.code is ErrorCode.NOT_INITIALIZED


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


def test_missing_git_root_is_not_initialized(tmp_path: Path):
    start = tmp_path / "not-a-repo" / "src"
    start.mkdir(parents=True)

    with pytest.raises(CodeCortexError) as exc:
        find_repository(start)

    assert exc.value.code is ErrorCode.NOT_INITIALIZED


@pytest.fixture
def repository(tmp_path) -> Repository:
    root = tmp_path / "repo"
    _make_git_root(root)
    return Repository(root)
