"""Deterministic safety gates for the opt-in M1b Child Codex benchmark."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.run_codecortex_benchmark import (
    BenchmarkConfig,
    BenchmarkHarness,
    run_benchmark,
)
from tests.e2e.trace import sanitize_trace

CORPUS = Path(__file__).parents[1] / "benchmark" / "questions.yaml"
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "m1b_repo"


def _config(tmp_path: Path, *, execute: bool = False) -> BenchmarkConfig:
    return BenchmarkConfig(
        corpus=CORPUS,
        fixture_root=FIXTURE_ROOT,
        artifact_dir=tmp_path / "artifacts",
        repetitions=3,
        execute=execute,
    )


def test_native_and_codecortex_runs_use_distinct_clean_copies(tmp_path: Path) -> None:
    harness = BenchmarkHarness(_config(tmp_path))

    run = harness.prepare_case("graph-outside-cli-coding", repetition=1)

    assert run.native_repo != run.codecortex_repo
    assert run.native_repo.is_dir() and run.codecortex_repo.is_dir()
    assert run.native_commit == run.codecortex_commit
    assert run.native_codex_home != run.codecortex_codex_home
    assert not (run.native_repo / ".codex").exists()
    assert not (run.codecortex_repo / ".codex").exists()
    assert not (run.native_codex_home / ".codex").exists()
    assert not (run.codecortex_codex_home / ".codex").exists()


def test_trace_redacts_machine_paths_and_tokens() -> None:
    raw = (
        """{"OPENAI_API_KEY":"sk-secret-token","path":"/home/alice/project/app.py","authorization":"Bearer secret-value","nested":{"cwd":"/tmp/codecortex-run","token":"abc"}}\n"""
        "OPENAI_API_KEY=plain-text-secret\n"
    )

    sanitized = sanitize_trace(raw)

    assert "/home/" not in sanitized
    assert "/tmp/" not in sanitized
    assert "OPENAI_API_KEY" not in sanitized
    assert "sk-secret-token" not in sanitized
    assert "secret-value" not in sanitized
    assert "plain-text-secret" not in sanitized
    assert "\"<redacted>\"" in sanitized


def test_default_benchmark_run_records_an_explicit_opt_in_skip(tmp_path: Path) -> None:
    report = run_benchmark(_config(tmp_path))

    assert report.status == "skipped"
    assert report.skip_reason is not None
    persisted = json.loads((tmp_path / "artifacts" / "benchmark_report.json").read_text())
    assert persisted["status"] == "skipped"
    assert persisted["results"] == []


def test_approval_mode_uses_a_persistent_first_turn_and_resume(tmp_path: Path) -> None:
    harness = BenchmarkHarness(_config(tmp_path, execute=True))
    prepared = harness.prepare_case("unmaterialized-expand", repetition=1)

    first = harness.approval_first_command(prepared.case)
    resumed = harness.approval_resume_command("thread-123", "approve exact digest")

    assert "--ephemeral" not in first
    assert first[:3] == ["codex", "exec", "--json"]
    assert resumed[:4] == ["codex", "exec", "resume", "--json"]
    assert "thread-123" in resumed
