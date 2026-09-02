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

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["codex", *arguments], cwd=self.root, env={**os.environ, "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex")}, capture_output=True, text=True, check=False, timeout=180)


@pytest.fixture
def codex_harness(tmp_path: Path) -> CodexHarness:
    if os.environ.get("CODECORTEX_RUN_CODEX_E2E") != "1" or shutil.which("codex") is None:
        pytest.skip("Codex CLI/model access not configured; set CODECORTEX_RUN_CODEX_E2E=1 to opt in")
    executable = shutil.which("codecortex")
    if executable is None:
        pytest.skip("CodeCortex CLI is not installed on PATH")
    root, home = tmp_path / "fixture", tmp_path / "codex-home"
    root.mkdir(); home.mkdir()
    (root / "README.md").write_text("# M0 Fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    install_codex(home, Path(executable), dry_run=False, force=False)
    config = home / CONFIG_RELATIVE
    document = tomlkit.parse(config.read_text(encoding="utf-8"))
    document["mcp_servers"]["codecortex"]["tools"]["apply_cognitive_proposal"]["approval_mode"] = "approve"
    config.write_text(tomlkit.dumps(document), encoding="utf-8")
    return CodexHarness(root, home)
