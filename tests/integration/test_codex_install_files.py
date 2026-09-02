"""Installed file and package-resource integration coverage."""

import importlib.resources
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


def test_packaged_resources_are_available_to_installed_code() -> None:
    resources = importlib.resources.files("codecortex.integrations.codex.resources")
    assert resources.joinpath("SKILL.md").is_file()
    assert resources.joinpath("codecortex-analyzer.toml").is_file()


def test_built_wheel_contains_both_codex_resources() -> None:
    wheel = next(Path("dist").glob("codecortex-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert any(name.endswith("resources/SKILL.md") for name in names)
    assert any(name.endswith("resources/codecortex-analyzer.toml") for name in names)
