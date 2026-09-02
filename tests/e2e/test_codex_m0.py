"""Real child-Codex checks, disabled unless explicitly opted in."""

import pytest


@pytest.mark.codex_e2e
def test_native_codex_survives_broken_codecortex(codex_harness: object) -> None:
    result = codex_harness.run("exec", "--ephemeral", "--json", "Read README.md and return its first heading.")  # type: ignore[union-attr]
    assert result.returncode == 0, result.stderr
    assert "M0 Fixture" in result.stdout


@pytest.mark.codex_e2e
def test_unapproved_proposal_does_not_apply(codex_harness: object) -> None:
    result = codex_harness.run("exec", "--json", "$codecortex initialize this repository, then stop without applying a Proposal.")  # type: ignore[union-attr]
    assert result.returncode == 0, result.stderr
    assert not (codex_harness.root / ".codecortex/history/events").exists()  # type: ignore[union-attr]
