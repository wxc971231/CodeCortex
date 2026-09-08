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

from codecortex.integrations.codex.install import install_codex
from tests.e2e.conftest import CodexHarness, auto_approve_codecortex_tools

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
    auto_approve_codecortex_tools(home)
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
    """Two-turn real flow: build and display a proposal, then approve it.

    The first turn must not apply anything: the initial instruction is not
    approval of a proposal that does not exist yet. The second turn carries
    the user's explicit approval of the exact current patch digest.
    """
    first = codex_m1a_harness.run(
        "exec",
        "--json",
        """$codecortex init. Follow the installed M1a Skill exactly: run fact sync,
request analysis scope, delegate the read-only codecortex-analyzer, then submit one
complete AnalysisReport through create_cognitive_proposal_from_analysis. Do not
invent source anchors. Stop after the proposal is created and you have displayed
its proposal_id and current patch_digest. Do NOT apply anything in this turn; the
user will approve the exact digest in a follow-up message.""",
    )
    assert first.returncode == 0, first.stderr
    pending_dir = (
        codex_m1a_harness.root / ".codecortex" / ".cache" / "pending_proposals"
    )
    pending = sorted(pending_dir.glob("prop_*.json"))
    assert len(pending) == 1, (
        f"expected exactly one pending proposal, found {len(pending)}; "
        f"child output tail: {first.stdout[-2000:]}"
    )

    # Bounded approval loop: each attempt approves the newest pending proposal.
    # If Core rejects the apply (e.g. ANALYSIS_REPORT_INVALID), the child must
    # discard the rejected candidate, run a fresh analyzer pass that fixes the
    # reported issues, create a replacement proposal, and stop; the harness
    # then approves the new digest on the next attempt. Approval always comes
    # from the harness (the user), never from the child.
    root = codex_m1a_harness.root
    manifest_path = root / ".codecortex" / "manifest.json"
    attempted: set[str] = set()
    applied = False
    last_stdout = ""
    for _attempt in range(3):
        fresh = []
        for candidate in sorted(
            pending_dir.glob("prop_*.json"), key=lambda item: item.stat().st_mtime
        ):
            record = json.loads(candidate.read_text(encoding="utf-8"))
            if record["proposal_id"] not in attempted:
                fresh.append(record)
        assert fresh, (
            "child stopped without creating a replacement proposal after a "
            f"rejected apply; child output tail: {last_stdout[-2000:]}"
        )
        current = fresh[-1]
        attempted.add(current["proposal_id"])
        turn = codex_m1a_harness.run(
            "exec",
            "--json",
            f"""The user now explicitly approves proposal {current["proposal_id"]} with
current patch_digest {current["patch_digest"]}. Call apply_cognitive_proposal
immediately with this exact proposal_id and patch_digest, and report the
result. If Core rejects the
apply (for example ANALYSIS_REPORT_INVALID), do not give up and do not repair
the report yourself: discard the rejected proposal, delegate a fresh
codecortex-analyzer pass that fixes every issue Core reported, submit the new
report through create_cognitive_proposal_from_analysis, display the new
proposal_id and patch_digest, and stop without applying it; the user will
approve the new digest in a follow-up message.""",
        )
        assert turn.returncode == 0, turn.stderr
        last_stdout = turn.stdout
        if json.loads(manifest_path.read_text(encoding="utf-8"))[
            "cognition_initialized"
        ] is True:
            applied = True
            break
    assert applied, (
        "proposal was not applied within 3 approval rounds; "
        f"child output tail: {last_stdout[-2000:]}"
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    graph = json.loads(
        (root / ".codecortex" / "graph.json").read_text(encoding="utf-8")
    )
    assert manifest["cognition_initialized"] is True
    assert manifest["graph_revision"] >= 1
    node_ids = {node["id"] for node in graph["nodes"]}
    kinds = {node["kind"] for node in graph["nodes"]}
    assert "responsibility" in kinds and "behavior" in kinds
    assert len(node_ids) >= 2

    entity_refs = json.loads(
        (root / ".codecortex" / "entity_refs.json").read_text(encoding="utf-8")
    )
    refs_by_uid = {entry["uid"]: entry for entry in entity_refs["entities"]}
    mappings = graph.get("implementation_mappings", [])
    assert mappings, (
        "no implementation mappings; "
        f"child output tail: {last_stdout[-2000:]}"
    )
    resolved = [
        refs_by_uid[mapping["entity_uid"]]
        for mapping in mappings
        if mapping.get("entity_uid") in refs_by_uid
    ]
    assert resolved, "no mapping resolves through formal entity refs"
    checked = 0
    for ref in resolved:
        anchor = root / ref["relative_path"]
        assert anchor.is_file() and anchor.suffix == ".py"
        qualname = str(ref["last_known_address"]).split(":", 1)[-1]
        if not qualname:
            continue  # module-level refs carry no symbol
        symbol = qualname.split(".")[-1]
        text = anchor.read_text(encoding="utf-8")
        assert f"def {symbol}" in text or f"class {symbol}" in text, (
            f"anchor {ref['relative_path']} does not define {symbol}"
        )
        checked += 1
    assert checked, "no symbol-level mapping anchor was verified"
