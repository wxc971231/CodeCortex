"""Installed file and package-resource integration coverage."""

import importlib.resources
import subprocess
import sys
import zipfile
from pathlib import Path

from codecortex.integrations.codex.install import install_codex


def test_install_writes_parseable_skill_agent_and_config(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codecortex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    result = install_codex(tmp_path, executable, dry_run=False, force=False)

    assert result.changed is True
    skill = tmp_path / ".agents" / "skills" / "codecortex" / "SKILL.md"
    agent = tmp_path / ".codex" / "agents" / "codecortex-analyzer.toml"
    assert "$codecortex" in skill.read_text(encoding="utf-8")
    assert 'sandbox_mode = "read-only"' in agent.read_text(encoding="utf-8")
    assert 'args = ["mcp", "--profile", "analyzer"]' in agent.read_text(
        encoding="utf-8"
    )


def test_installed_skill_requires_faithful_pending_change_reporting(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codecortex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    install_codex(tmp_path, executable, dry_run=False, force=False)

    skill = (
        tmp_path / ".agents" / "skills" / "codecortex" / "SKILL.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(skill.split())
    assert "`pending_changes(cursor=null, limit=50)`" in normalized
    assert "`changed_files` and `unmapped_changes` as separate lists" in normalized
    assert "unmapped dependency or entity is not a modified or refreshed source file" in normalized
    assert "`scope_confidence=partial` or `unknown`" in normalized


def test_packaged_resources_are_available_to_installed_code() -> None:
    resources = importlib.resources.files("codecortex.integrations.codex.resources")
    assert resources.joinpath("SKILL.md").is_file()
    assert resources.joinpath("codecortex-analyzer.toml").is_file()


def test_built_wheel_contains_both_codex_resources(tmp_path: Path) -> None:
    """Build in an isolated test directory; never depend on a prior build."""
    project_root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("codecortex-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert any(name.endswith("resources/SKILL.md") for name in names)
    assert any(name.endswith("resources/codecortex-analyzer.toml") for name in names)
