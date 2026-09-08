"""Unit coverage for the user-level Codex resource installer."""

from pathlib import Path

import pytest
import tomlkit

from codecortex.integrations.codex import install as installer
from codecortex.integrations.codex.install import install_codex


@pytest.fixture
def codecortex_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / "codecortex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return executable


def test_second_install_has_no_diff_and_preserves_unknown_config(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    """Managed MCP fields merge without disturbing unrelated TOML or comments."""
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "# personal setting\n[features]\ncustom = true\n", encoding="utf-8"
    )

    first = install_codex(
        tmp_path, codecortex_executable, dry_run=False, force=False
    )
    first_bytes = config.read_bytes()
    second = install_codex(
        tmp_path, codecortex_executable, dry_run=False, force=False
    )

    parsed = tomlkit.parse(config.read_text(encoding="utf-8"))
    assert first.changed is True
    assert second.changed is False
    assert config.read_bytes() == first_bytes
    assert parsed["features"]["custom"] is True
    assert parsed["mcp_servers"]["codecortex"]["command"] == str(
        codecortex_executable.resolve()
    )
    assert parsed["mcp_servers"]["codecortex"]["args"] == [
        "mcp",
        "--profile",
        "main",
    ]
    assert (
        parsed["mcp_servers"]["codecortex"]["tools"][
            "apply_cognitive_proposal"
        ]["approval_mode"]
        == "prompt"
    )


def test_dry_run_reports_changes_without_creating_user_files(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    result = install_codex(tmp_path, codecortex_executable, dry_run=True, force=False)

    assert result.changed is True
    assert result.dry_run is True
    assert result.changed_paths == (
        ".agents/skills/codecortex/SKILL.md",
        ".codex/agents/codecortex-analyzer.toml",
        ".codex/config.toml",
    )
    assert not (tmp_path / ".agents").exists()
    assert not (tmp_path / ".codex").exists()

def test_rejects_symlinked_managed_target(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("do not overwrite\n", encoding="utf-8")
    target = tmp_path / ".agents" / "skills" / "codecortex" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        install_codex(tmp_path, codecortex_executable, dry_run=False, force=False)

    assert outside.read_text(encoding="utf-8") == "do not overwrite\n"


def test_rejects_symlinked_config_before_reading_its_target(
    tmp_path: Path, codecortex_executable: Path
) -> None:
    outside = tmp_path / "outside.toml"
    outside.write_text("this is not valid TOML = [\n", encoding="utf-8")
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.symlink_to(outside)

    with pytest.raises(ValueError, match="symlinked managed target"):
        install_codex(tmp_path, codecortex_executable, dry_run=False, force=False)

    assert outside.read_text(encoding="utf-8") == "this is not valid TOML = [\n"


def test_validation_failure_restores_every_changed_target(
    tmp_path: Path, codecortex_executable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-write validation failure leaves the previous files byte-identical."""
    config = tmp_path / ".codex" / "config.toml"
    skill = tmp_path / ".agents" / "skills" / "codecortex" / "SKILL.md"
    agent = tmp_path / ".codex" / "agents" / "codecortex-analyzer.toml"
    for path, content in (
        (config, b"[features]\ncustom = true\n"),
        (skill, b"old skill\n"),
        (agent, b"old agent\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = {path: path.read_bytes() for path in (config, skill, agent)}

    def fail_validation(*args: object, **kwargs: object) -> None:
        raise ValueError("forced validation failure")

    monkeypatch.setattr(installer, "_validate_outputs", fail_validation)
    with pytest.raises(ValueError, match="forced validation failure"):
        install_codex(tmp_path, codecortex_executable, dry_run=False, force=True)

    assert {path: path.read_bytes() for path in before} == before
