"""Exercise two-turn continuity without starting a paid Child or reading auth."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.run_codecortex_benchmark import (
    BenchmarkExecutionError,
    BenchmarkHarness,
    benchmark_run_digest,
)
from tests.e2e.test_codex_m1b import _config


def _output(answer: str, *, session: bool = True) -> str:
    records = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    if session:
        records.insert(0, {"type": "thread.started", "thread_id": "thread-real"})
    return "\n".join(json.dumps(record) for record in records)


def test_followup_resumes_both_isolated_arms_and_scores_only_final_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    prepared = harness.prepare_case("same-topic-followup", repetition=1)
    calls: list[tuple[list[str], Path, Path]] = []

    def execute(command: list[str], repository: Path, home: Path) -> subprocess.CompletedProcess[str]:
        calls.append((command, repository, home))
        answer = "initial checkpoint persistence /private/path secret=hidden"
        if "resume" in command:
            answer = "Read the latest atomically written checkpoint. src/forge/checkpoint.py:15-17"
        return subprocess.CompletedProcess(command, 0, _output(answer), "")

    monkeypatch.setattr(harness, "_execute", execute)
    monkeypatch.setattr(harness, "_install_codecortex_test_home", lambda _home: None)
    ticks = iter([1.0, 1.1, 2.0, 2.2, 3.0, 3.1, 4.0, 4.2])
    monkeypatch.setattr("scripts.run_codecortex_benchmark.time.perf_counter", lambda: next(ticks))
    try:
        result = harness.run_case(prepared)
        assert len(calls) == 4
        for offset, arm in ((0, result.native), (2, result.codecortex)):
            first, second = calls[offset:offset + 2]
            assert first[0][:2] == ["codex", "exec"]
            assert "--ephemeral" not in first[0]
            assert "Follow-up" not in first[0][-1]
            assert "checkpoint persisted" in first[0][-1]
            assert second[0][:3] == ["codex", "exec", "resume"]
            assert second[0][-2] == "thread-real"
            assert f'sandbox_mode="{harness.config.sandbox}"' in second[0]
            assert first[0][first[0].index("--sandbox") + 1] == harness.config.sandbox
            assert "Follow-up:" in second[0][-1]
            assert first[1:] == second[1:]
            for command, _, _ in (first, second):
                assert ("--ignore-user-config" in command) == (offset == 0)
                assert command[command.index("--model") + 1] == harness.config.model
                assert f'model_reasoning_effort="{harness.config.reasoning_effort}"' in command
            assert arm.trace.trace.input_tokens == 20
            assert arm.trace.trace.output_tokens == 10
            assert arm.trace.trace.latency_ms == 300
            assert "initial" not in arm.answer
            assert arm.score.required_fact_recall == 1.0
            artifact = (harness.config.artifact_dir / arm.trace_path).read_text()
            assert "initial checkpoint persistence" in artifact
            assert "Read the latest" in artifact
            assert "/private/path" not in artifact and "hidden" not in artifact
        assert calls[0][1] != calls[2][1]
        assert calls[0][2] != calls[2][2]
        digest = benchmark_run_digest([result], 3, harness.config.artifact_dir)
        path = harness.config.artifact_dir / result.native.trace_path
        path.write_text(path.read_text().replace("initial checkpoint persistence", "altered initial discussion"))
        assert benchmark_run_digest([result], 3, harness.config.artifact_dir) != digest
    finally:
        harness.cleanup(prepared)


def test_empty_followup_cannot_borrow_first_answer_or_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    prepared = harness.prepare_case("same-topic-followup", repetition=1)

    def execute(command: list[str], _repository: Path, _home: Path) -> subprocess.CompletedProcess[str]:
        answer = "Read the latest atomically written checkpoint. src/forge/checkpoint.py:15-17"
        output = _output(answer) if "resume" not in command else _output("")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(harness, "_execute", execute)
    try:
        arm = harness._run_child(prepared, "native")
        assert arm.answer == ""
        assert arm.score.required_fact_recall == 0.0
        assert arm.trace.trace.source_references == ()
        assert not arm.score.passed
    finally:
        harness.cleanup(prepared)


@pytest.mark.parametrize("first_output", [
    _output("first", session=False) + '\n{"item":{"thread_id":"invented"}}',
    _output("first", session=False) + '\n{"type":"thread.started","thread_id":"   "}',
    _output(""),
    _output("first") + '\n{"type":"turn.failed","error":{"message":"failed"}}',
])
def test_followup_rejects_invalid_session_evidence_or_failed_discussion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_output: str,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    prepared = harness.prepare_case("same-topic-followup", repetition=1)
    calls: list[list[str]] = []

    def execute(command: list[str], _repository: Path, _home: Path) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, first_output, "")

    monkeypatch.setattr(harness, "_execute", execute)
    try:
        with pytest.raises(BenchmarkExecutionError, match="Follow-up first turn"):
            harness._run_child(prepared, "codecortex")
        assert len(calls) == 1
    finally:
        harness.cleanup(prepared)


@pytest.mark.parametrize("returncode,session", [(1, True), (0, False)])
def test_followup_first_turn_failure_never_invents_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, session: bool,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    prepared = harness.prepare_case("same-topic-followup", repetition=1)
    calls: list[list[str]] = []

    def execute(command: list[str], _repository: Path, _home: Path) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, _output("first", session=session), "")

    monkeypatch.setattr(harness, "_execute", execute)
    try:
        with pytest.raises(BenchmarkExecutionError, match="Follow-up first turn"):
            harness._run_child(prepared, "native")
        assert len(calls) == 1
        assert list(harness.config.artifact_dir.rglob("*.jsonl"))
    finally:
        harness.cleanup(prepared)
