"""Opt-in real-Codex harness; it never uses the normal Codex home."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import tomlkit

from codecortex.integrations.codex.install import CONFIG_RELATIVE, install_codex


class CodexHarness:
    def __init__(self, root: Path, home: Path) -> None:
        self.root, self.home = root, home

    def run(
        self, *arguments: str, break_codecortex_mcp: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if break_codecortex_mcp:
            config = self.home / CONFIG_RELATIVE
            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            document["mcp_servers"]["codecortex"]["command"] = "/missing/codecortex"
            config.write_text(tomlkit.dumps(document), encoding="utf-8")
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
        }
        return subprocess.run(
            ["codex", *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )


@pytest.fixture
def codex_harness(tmp_path: Path) -> CodexHarness:
    if os.environ.get("CODECORTEX_RUN_CODEX_E2E") != "1" or shutil.which("codex") is None:
        pytest.skip("Codex CLI/model access not configured; set CODECORTEX_RUN_CODEX_E2E=1 to opt in")
    executable = shutil.which("codecortex")
    if executable is None:
        pytest.skip("CodeCortex CLI is not installed on PATH")
    if os.environ.get("CODECORTEX_E2E_COPY_AUTH") != "1":
        pytest.skip("Set CODECORTEX_E2E_COPY_AUTH=1 to copy browser auth into the temporary Codex home")
    auth_source = Path.home() / ".codex" / "auth.json"
    if not auth_source.is_file() or auth_source.is_symlink():
        pytest.skip("Browser authentication file is unavailable for isolated E2E")
    root, home = tmp_path / "fixture", tmp_path / "codex-home"
    root.mkdir(); home.mkdir()
    (root / "README.md").write_text("# M0 Fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    install_codex(home, Path(executable), dry_run=False, force=False)
    auth_target = home / ".codex" / "auth.json"
    shutil.copyfile(auth_source, auth_target)
    auth_target.chmod(0o600)
    config = home / CONFIG_RELATIVE
    document = tomlkit.parse(config.read_text(encoding="utf-8"))
    document["mcp_servers"]["codecortex"]["tools"]["apply_cognitive_proposal"]["approval_mode"] = "approve"
    config.write_text(tomlkit.dumps(document), encoding="utf-8")
    return CodexHarness(root, home)
