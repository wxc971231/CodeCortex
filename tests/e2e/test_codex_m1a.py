"""Opt-in real-Codex M1a repository-understanding acceptance.

This is intentionally not simulated. A missing CLI/model/browser-auth setup
is a pytest skip, while an opted-in run must produce real formal state.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import tomlkit

from codecortex.integrations.codex.install import CONFIG_RELATIVE, install_codex
from tests.e2e.conftest import CodexHarness

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "m1a_repo"


@pytest.fixture
def codex_m1a_harness(tmp_path: Path) -> CodexHarness:
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
    shutil.copytree(FIXTURE_ROOT, root)
    home.mkdir()
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


def test_m1a_fixture_oracle_is_human_authored_and_grounded() -> None:
    """Deterministic gate: the oracle must cover and resolve against the fixture.

    This runs without any model access. It pins the human-authored oracle to
    the real fixture sources so a drifting fixture or an ungrounded oracle
    fails before any Codex run is attempted.
    """
    oracle = json.loads((FIXTURE_ROOT / "oracle.json").read_text(encoding="utf-8"))
    files = sorted((FIXTURE_ROOT / "src").rglob("*.py"))
    assert 20 <= len(files) <= 50
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert oracle["responsibilities"] == [
        "responsibility.ingestion",
        "responsibility.reporting",
    ]
    assert oracle["behaviors"] == [
        "behavior.import-records",
        "behavior.render-report",
    ]
    assert oracle["shared_capabilities"] == ["capability.record-validation"]
    assert set(oracle["flows"]) <= set(oracle["behaviors"])

    anchored_ids = set(oracle["anchors"])
    expected_ids = (
        set(oracle["responsibilities"])
        | set(oracle["behaviors"])
        | set(oracle["shared_capabilities"])
    )
    assert expected_ids <= anchored_ids
    for subject, anchor in sorted(oracle["anchors"].items()):
        relative, separator, symbol = anchor.partition(":")
        assert separator, f"{subject} anchor must be '<path>:<symbol>'"
        assert relative.startswith("src/") and relative.endswith(".py")
        assert symbol.isidentifier()
        text = (FIXTURE_ROOT / relative).read_text(encoding="utf-8")
        assert f"def {symbol}" in text, f"{subject} anchor does not resolve"


@pytest.mark.codex_e2e
def test_real_init_creates_grounded_graph(codex_m1a_harness: CodexHarness) -> None:
    result = codex_m1a_harness.run(
        "exec",
        "--json",
        """$codecortex init. Follow the installed M1a Skill exactly: run fact sync,
request analysis scope, delegate the read-only codecortex-analyzer, submit one
complete AnalysisReport through create_cognitive_proposal_from_analysis, show
the current patch digest, then apply that exact proposal. Do not invent source
anchors. Complete the full workflow in this test repository.""",
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(
        (codex_m1a_harness.root / ".codecortex" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    graph = json.loads(
        (codex_m1a_harness.root / ".codecortex" / "graph.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["cognition_initialized"] is True
    assert manifest["graph_revision"] >= 1
    node_ids = {node["id"] for node in graph["nodes"]}
    assert {"responsibility.ingestion", "responsibility.reporting"} <= node_ids
