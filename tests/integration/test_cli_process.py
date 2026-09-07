"""Process-level tests for the CLI contract against real repositories."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import codecortex
from codecortex.application.services import ApplicationServices
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views

SOURCE_ROOT = Path(codecortex.__file__).resolve().parents[1]


def run_codecortex(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `python -m codecortex` in *cwd* with the package importable."""
    environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "codecortex", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def initialized_repo(repo_root: Path) -> Path:
    repository = Repository(repo_root)
    ApplicationServices(
        repository=repository,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(repo_root),
        view_renderer=render_views,
    ).initialize_repository()
    return repo_root


def test_version_prints_version(tmp_path: Path) -> None:
    result = run_codecortex("--version", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == codecortex.__version__
    assert result.stderr == ""


def test_json_mode_keeps_diagnostics_off_stdout(initialized_repo: Path) -> None:
    result = run_codecortex("validate", "--json", cwd=initialized_repo)
    assert result.returncode == 0
    assert json.loads(result.stdout)["valid"] is True
    assert "INFO" not in result.stdout
    assert result.stderr == ""


def test_validate_from_nested_directory_finds_repository(
    initialized_repo: Path,
) -> None:
    nested = initialized_repo / "pkg" / "module"
    nested.mkdir(parents=True)
    result = run_codecortex("validate", "--json", cwd=nested)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"valid": True, "issues": []}


def test_validate_human_output(initialized_repo: Path) -> None:
    result = run_codecortex("validate", cwd=initialized_repo)
    assert result.returncode == 0
    assert "valid" in result.stdout
    assert result.stderr == ""


def test_validate_uninitialized_repository_exits_3(repo_root: Path) -> None:
    """A Git repository without formal state maps NOT_INITIALIZED to exit 3."""
    result = run_codecortex("validate", "--json", cwd=repo_root)
    assert result.returncode == 3
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "NOT_INITIALIZED"


def test_validate_outside_git_repository_exits_3(tmp_path: Path) -> None:
    result = run_codecortex("validate", cwd=tmp_path)
    assert result.returncode == 3
    assert result.stdout == ""
    assert "NOT_INITIALIZED" in result.stderr


def test_unknown_command_exits_2(tmp_path: Path) -> None:
    result = run_codecortex("frobnicate", cwd=tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""


def test_mcp_rejects_unknown_profile(tmp_path: Path) -> None:
    result = run_codecortex("mcp", "--profile", "bogus", cwd=tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""


def test_mcp_without_repository_keeps_stdout_free_for_protocol_frames(tmp_path: Path) -> None:
    """The real Task 9 server rejects a non-repository before protocol startup."""
    result = run_codecortex("mcp", "--profile", "main", cwd=tmp_path)
    assert result.returncode == 3
    assert result.stdout == ""
    assert "NOT_INITIALIZED" in result.stderr
