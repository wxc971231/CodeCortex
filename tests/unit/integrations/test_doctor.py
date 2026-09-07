"""Unit coverage for CodeCortex's read-only installation diagnostics."""

import subprocess
from pathlib import Path

import pytest
import tomlkit

from codecortex.application.services import ApplicationServices
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views
from codecortex.integrations.codex.doctor import DoctorStatus, run_doctor
from codecortex.integrations.codex.install import CONFIG_RELATIVE, install_codex


@pytest.fixture
def codecortex_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / "codecortex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return executable


def test_doctor_reports_clean_install_without_writing(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    install_codex(tmp_path, codecortex_executable, dry_run=False, force=False)
    before = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".codecortex-backup-" not in path.name
    }

    report = run_doctor(tmp_path, codecortex_executable, repository=None)

    assert report.has_errors is False
    assert report.by_code("CODEX_MCP_COMMAND").status is DoctorStatus.OK
    assert report.by_code("CODEX_SKILL_RESOURCE").status is DoctorStatus.OK
    assert report.by_code("CODEX_ANALYZER_RESOURCE").status is DoctorStatus.OK
    assert {
        path: path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".codecortex-backup-" not in path.name
    } == before


def test_doctor_reports_actionable_mcp_mismatch(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    install_codex(tmp_path, codecortex_executable, dry_run=False, force=False)
    config_path = tmp_path / CONFIG_RELATIVE
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    document["mcp_servers"]["codecortex"]["command"] = "/missing/codecortex"
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    report = run_doctor(tmp_path, codecortex_executable, repository=None)

    check = report.by_code("CODEX_MCP_COMMAND")
    assert check.status is DoctorStatus.ERROR
    assert check.action == "Run `codecortex install-codex` again."
    assert "/missing/codecortex" not in check.summary


def test_doctor_redacts_malformed_config_contents(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    config_path = tmp_path / CONFIG_RELATIVE
    config_path.parent.mkdir()
    secret = "super-secret-value"
    config_path.write_text(f"token = '{secret}'\ninvalid = [\n", encoding="utf-8")

    report = run_doctor(tmp_path, codecortex_executable, repository=None)

    check = report.by_code("CODEX_MCP_CONFIG")
    assert check.status is DoctorStatus.ERROR
    assert secret not in check.summary
    assert secret not in (check.action or "")


def test_doctor_marks_missing_installed_skill_as_error(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    report = run_doctor(tmp_path, codecortex_executable, repository=None)

    check = report.by_code("CODEX_SKILL_RESOURCE")
    assert check.status is DoctorStatus.ERROR
    assert check.action == "Run `codecortex install-codex` again."


def test_doctor_validates_repository_formal_state(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    repository = Repository(tmp_path)
    ApplicationServices(
        repository=repository,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(tmp_path),
        view_renderer=render_views,
    ).initialize_repository()

    report = run_doctor(tmp_path, codecortex_executable, repository)

    assert report.by_code("REPOSITORY_STATE").status is DoctorStatus.OK
