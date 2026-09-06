"""Deterministic safety gates for the opt-in M1b Child Codex benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecortex.domain.cognition import validate_formal_state
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.repository import Repository
from scripts.run_codecortex_benchmark import (
    BenchmarkConfig,
    BenchmarkExecutionError,
    BenchmarkHarness,
    BenchmarkReport,
    attach_blind_review_to_report,
    benchmark_acceptance_status,
    benchmark_run_digest,
    corpus_file_digest,
    load_blind_review,
    run_benchmark,
    write_benchmark_report,
)
from scripts.run_codecortex_benchmark import main as benchmark_main
from tests.e2e.trace import parse_sanitized_trace, sanitize_trace

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


def _completed_run_payload(
    tmp_path: Path,
) -> tuple[Path, tuple[dict[str, object], ...]]:
    artifact_dir = tmp_path / "artifacts"
    native_path = artifact_dir / "traces/case-a/1/native.jsonl"
    codecortex_path = artifact_dir / "traces/case-a/1/codecortex.jsonl"
    native_path.parent.mkdir(parents=True)
    native_path.write_text(
        sanitize_trace('{"message":"native answer"}'), encoding="utf-8"
    )
    codecortex_path.write_text(
        sanitize_trace('{"message":"codecortex answer"}'), encoding="utf-8"
    )
    return artifact_dir, (
        {
            "case_id": "case-a",
            "repetition": 1,
            "commit": "0123456789abcdef",
            "native": {
                "side": "native",
                "returncode": 0,
                "trace_path": "traces/case-a/1/native.jsonl",
                "answer": "native answer",
            },
            "codecortex": {
                "side": "codecortex",
                "returncode": 0,
                "trace_path": "traces/case-a/1/codecortex.jsonl",
                "answer": "codecortex answer",
            },
        },
    )


def _completed_corpus_run_payload(
    tmp_path: Path,
) -> tuple[Path, tuple[dict[str, object], ...]]:
    artifact_dir = tmp_path / "artifacts"
    cases = BenchmarkHarness(_config(tmp_path)).cases
    results: list[dict[str, object]] = []
    for case in cases:
        for repetition in range(1, 4):
            arms: dict[str, dict[str, object]] = {}
            for side in ("native", "codecortex"):
                relative = f"traces/{case.id}/{repetition}/{side}.jsonl"
                path = artifact_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                answer = f"{side} answer for {case.id} repetition {repetition}"
                path.write_text(
                    sanitize_trace(json.dumps({"message": answer})),
                    encoding="utf-8",
                )
                arms[side] = {
                    "side": side,
                    "returncode": 0,
                    "trace_path": relative,
                    "answer": answer,
                }
            results.append(
                {
                    "case_id": case.id,
                    "repetition": repetition,
                    "commit": "0123456789abcdef",
                    "native": arms["native"],
                    "codecortex": arms["codecortex"],
                }
            )
    return artifact_dir, tuple(results)


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
    harness.cleanup(run)


def test_cleanup_removes_entire_workspace_outside_artifact_directory(
    tmp_path: Path,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    prepared = harness.prepare_case("graph-outside-cli-coding", repetition=1)

    assert prepared.workspace.is_dir()
    assert not prepared.workspace.is_relative_to(harness.config.artifact_dir)

    harness.cleanup(prepared)

    assert not prepared.workspace.exists()
    assert not (harness.config.artifact_dir / "workspaces").exists()


def test_cleanup_failure_is_an_explicit_benchmark_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    prepared = harness.prepare_case("graph-outside-cli-coding", repetition=1)
    monkeypatch.setattr(
        "scripts.run_codecortex_benchmark.shutil.rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("workspace cannot be removed")
        ),
    )

    with pytest.raises(BenchmarkExecutionError, match="cleanup failed"):
        harness.cleanup(prepared)


def test_prepare_failure_removes_partial_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    workspace_parent = tmp_path / "temporary-workspaces"
    workspace_parent.mkdir()
    monkeypatch.setattr(harness, "_workspace_parent", lambda: str(workspace_parent))

    def fail_seed(_root: Path) -> None:
        raise RuntimeError("fixture seed failed")

    monkeypatch.setattr(harness, "_seed_formal_baseline", fail_seed)

    with pytest.raises(RuntimeError, match="fixture seed failed"):
        harness.prepare_case("graph-outside-cli-coding", repetition=1)

    assert tuple(workspace_parent.iterdir()) == ()


def test_prepared_codecortex_copy_contains_pinned_validated_formal_graph(
    tmp_path: Path,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))

    run = harness.prepare_case("graph-covered-fresh-checkpoint", repetition=1)
    state = FormalStore(Repository(run.codecortex_repo)).load()

    assert validate_formal_state(state).valid is True
    assert state.graph.graph_revision == 1
    assert {node["id"] for node in state.graph.nodes} >= {
        "behavior.persist-checkpoint",
        "behavior.render-report",
        "behavior.evaluate-batch",
    }
    assert state.graph.logical_flows
    assert state.graph.implementation_mappings
    assert any(node.get("evidence") for node in state.graph.nodes)
    assert state.history_events
    harness.cleanup(run)


def test_trace_route_and_anchors_come_from_mcp_evidence_not_self_claim() -> None:
    self_claim = parse_sanitized_trace(
        json.dumps({"message": "CODECORTEX_BENCHMARK_ROUTE: graph_current"})
    )
    assert self_claim.trace.route is None
    assert self_claim.trace.cognitive_anchor_ids == ()

    traced = parse_sanitized_trace(
        "\n".join(
            (
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "search_cognitive_graph",
                        "output": {"hits": [{"node_id": "behavior.render-report"}]},
                    }
                ),
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "effective_query_freshness",
                        "output": {"status": "unaffected_current"},
                    }
                ),
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "get_discussion_context",
                        "output": {"nodes": [{"id": "behavior.render-report"}]},
                    }
                ),
            )
        )
    )
    assert traced.trace.route == "graph_unaffected"
    assert traced.trace.cognitive_anchor_ids == ("behavior.render-report",)


def test_trace_ignores_self_claims_outside_the_matching_mcp_records() -> None:
    traced = parse_sanitized_trace(
        "\n".join(
            (
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "search_cognitive_graph",
                        "output": {"hits": [{"node_id": "behavior.render-report"}]},
                    }
                ),
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "effective_query_freshness",
                        "output": {"status": "unaffected_current"},
                    }
                ),
                json.dumps(
                    {
                        "message": "I used behavior.fake-anchor",
                        "status": "affected_source_first",
                    }
                ),
            )
        )
    )

    assert traced.trace.route == "graph_unaffected"
    assert traced.trace.cognitive_anchor_ids == ("behavior.render-report",)


def test_trace_reads_json_encoded_mcp_results_as_evidence() -> None:
    traced = parse_sanitized_trace(
        "\n".join(
            (
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "search_cognitive_graph",
                        "result": json.dumps(
                            {"hits": [{"node_id": "behavior.persist-checkpoint"}]}
                        ),
                    }
                ),
                json.dumps(
                    {
                        "server": "codecortex",
                        "tool": "effective_query_freshness",
                        "result": json.dumps({"status": "current"}),
                    }
                ),
            )
        )
    )

    assert traced.trace.route == "graph_current"
    assert traced.trace.cognitive_anchor_ids == ("behavior.persist-checkpoint",)


def test_trace_preserves_ordered_duplicate_mcp_tool_events() -> None:
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex_analyzer",
                "tool": "search_cognitive_graph",
                "result": {"hits": [{"node_id": "behavior.render-report"}]},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex-analyzer",
                "tool": "search_cognitive_graph",
                "result": {"hits": [{"node_id": "behavior.render-report"}]},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "effective_query_freshness",
                "result": {"status": "current"},
            },
        },
    ]

    traced = parse_sanitized_trace("\n".join(json.dumps(item) for item in events))

    assert traced.trace.mcp_tool_names == (
        "codecortex_analyzer.search_cognitive_graph",
        "codecortex-analyzer.search_cognitive_graph",
        "codecortex.effective_query_freshness",
    )
    assert traced.trace.analyzer_tool_call_count == 2


def test_trace_counts_only_real_assistant_materialization_prompts() -> None:
    events = (
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "search_cognitive_graph",
                "result": {"hits": [{"node_id": "behavior.evaluate-batch"}]},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "get_discussion_context",
                "result": {
                    "materialization_status": "unmaterialized",
                    "message": "A. Expand it now\nB. Keep it transient",
                },
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "I honored option B and answered from src/forge/evaluation.py:6-10.",
            },
        },
    )

    traced = parse_sanitized_trace("\n".join(json.dumps(item) for item in events))

    assert traced.answer.startswith("I honored option B")
    assert traced.trace.materialization_prompt_count == 0
    assert traced.trace.route == "source_first"


def test_trace_detects_repeated_assistant_materialization_prompts() -> None:
    events = (
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "search_cognitive_graph",
                "result": {"hits": [{"node_id": "behavior.evaluate-batch"}]},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "get_discussion_context",
                "result": {"materialization_status": "unmaterialized"},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Choose option A to materialize or option B for a transient answer?",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Again: option A to materialize, or option B transient?",
            },
        },
    )

    traced = parse_sanitized_trace("\n".join(json.dumps(item) for item in events))

    assert traced.trace.materialization_prompt_count == 2
    assert traced.trace.route == "offer_materialization"


def test_trace_detects_skill_standard_lettered_materialization_prompt() -> None:
    events = (
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "search_cognitive_graph",
                "result": {"hits": [{"node_id": "behavior.evaluate-batch"}]},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "get_discussion_context",
                "result": {"materialization_status": "unmaterialized"},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "A. Expand it now\nB. Keep it transient",
            },
        },
    )

    traced = parse_sanitized_trace("\n".join(json.dumps(item) for item in events))

    assert traced.trace.materialization_prompt_count == 1
    assert traced.trace.route == "offer_materialization"


def test_trace_counts_repeated_skill_standard_materialization_prompts() -> None:
    prompt = "A. Expand it now: show one Proposal.\nB. Keep it transient: answer now."
    events = (
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "codecortex",
                "tool": "get_discussion_context",
                "result": {"materialization_status": "unmaterialized"},
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": prompt}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": prompt}},
    )

    traced = parse_sanitized_trace("\n".join(json.dumps(item) for item in events))

    assert traced.trace.materialization_prompt_count == 2


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


def test_trace_redacts_generic_paths_basic_auth_and_unkeyed_tokens() -> None:
    fake_basic = "Basic ZmFrZS11c2VyOmZha2UtcGFzc3dvcmQ="
    fake_openai = "sk-proj-fakefakefakefakefakefakefakefake"
    fake_github = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE1234"
    fake_jwt = "eyJmYWtlIjoidHJ1ZSJ9.ZmFrZS1wYXlsb2Fk.ZmFrZS1zaWduYXR1cmU"
    raw = json.dumps(
        {
            "path": "/workspace/private-project/src/app.py",
            "message": f"{fake_basic} {fake_openai} {fake_github} {fake_jwt}",
            "nested": {"refresh_token": "fake-refresh-value"},
            "input_tokens": 17,
            "output_tokens": 9,
        }
    )

    sanitized = sanitize_trace(raw)

    for secret in (fake_basic, fake_openai, fake_github, fake_jwt, "fake-refresh-value"):
        assert secret not in sanitized
    assert "/workspace/" not in sanitized
    assert "refresh_token" not in sanitized
    assert json.loads(sanitized)["input_tokens"] == 17
    assert json.loads(sanitized)["output_tokens"] == 9


def test_trace_redacts_paths_after_labels_and_markdown_delimiters() -> None:
    raw = (
        "cwd:/workspace/private/project\n"
        "[local checkout](/home/alice/private-repository)\n"
        "cwd:C:\\Users\\alice\\private-repository\n"
        "cwd:\\\\fileserver\\private-share\\repository\n"
        "documentation: https://example.com/reference/path\n"
        "normal slash text: and/or, ratio 1/2, use / as a separator"
    )

    sanitized = sanitize_trace(raw)

    assert "/workspace/private/project" not in sanitized
    assert "/home/alice/private-repository" not in sanitized
    assert r"C:\Users\alice\private-repository" not in sanitized
    assert r"\\fileserver\private-share\repository" not in sanitized
    assert sanitized.count("<machine-path>") == 4
    assert "https://example.com/reference/path" in sanitized
    assert "and/or, ratio 1/2, use / as a separator" in sanitized


def test_default_benchmark_run_records_an_explicit_opt_in_skip(tmp_path: Path) -> None:
    report = run_benchmark(_config(tmp_path))

    assert report.status == "skipped"
    assert report.skip_reason is not None
    persisted = json.loads((tmp_path / "artifacts" / "benchmark_report.json").read_text())
    assert persisted["status"] == "skipped"
    assert persisted["execution_status"] == "not_started"
    assert persisted["corpus_id"] == "tests/benchmark/questions.yaml"
    assert persisted["corpus_digest"] == corpus_file_digest(CORPUS)
    assert "corpus" not in persisted
    assert persisted["results"] == []


def test_default_benchmark_cli_skip_is_not_a_success_exit(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"

    exit_code = benchmark_main(["--artifact-dir", str(artifact_dir)])

    assert exit_code == 2
    persisted = json.loads((artifact_dir / "benchmark_report.json").read_text())
    assert persisted["status"] == "skipped"
    assert persisted["execution_status"] == "not_started"


def test_persisted_report_sanitizes_machine_paths(tmp_path: Path) -> None:
    report = BenchmarkReport(
        status="failed",
        execution_status="failed",
        corpus_id="external/questions.yaml",
        corpus_digest="sha256:" + "a" * 64,
        repetitions=3,
        error="child failed under /workspace/private/repository",
    )

    write_benchmark_report(tmp_path, report)

    persisted = (tmp_path / "benchmark_report.json").read_text(encoding="utf-8")
    assert "/workspace/" not in persisted
    assert "<machine-path>" in persisted


def test_successful_execution_remains_pending_until_blind_review_is_validated(
    tmp_path: Path,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    assert benchmark_acceptance_status(automated_failed=False, blind_review=None) == (
        "pending_human_review"
    )
    review_path = tmp_path / "blind-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus_digest": corpus_file_digest(CORPUS),
                "run_digest": "sha256:" + "a" * 64,
                "reviewed_case_ids": [case.id for case in harness.cases],
                "blinded": True,
                "decision": "accepted",
                "reviewer": "independent-reviewer-1",
                "reviewed_at": "2026-09-05T08:00:00Z",
                "notes": "Answers were reviewed without side labels.",
            }
        ),
        encoding="utf-8",
    )

    review = load_blind_review(
        review_path, harness.cases, CORPUS, "sha256:" + "a" * 64
    )

    assert review.decision == "accepted"
    assert benchmark_acceptance_status(
        automated_failed=False, blind_review=review
    ) == "accepted"


def test_blind_review_must_cover_the_exact_pinned_corpus(tmp_path: Path) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    review_path = tmp_path / "blind-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus_digest": corpus_file_digest(CORPUS),
                "run_digest": "sha256:" + "a" * 64,
                "reviewed_case_ids": [harness.cases[0].id],
                "blinded": True,
                "decision": "accepted",
                "reviewer": "independent-reviewer-1",
                "reviewed_at": "2026-09-05T08:00:00Z",
                "notes": "Incomplete review.",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exact frozen case set"):
        load_blind_review(
            review_path, harness.cases, CORPUS, "sha256:" + "a" * 64
        )


def test_same_corpus_review_cannot_accept_a_different_completed_run(
    tmp_path: Path,
) -> None:
    harness = BenchmarkHarness(_config(tmp_path))
    review_path = tmp_path / "blind-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus_digest": corpus_file_digest(CORPUS),
                "run_digest": "sha256:" + "a" * 64,
                "reviewed_case_ids": [case.id for case in harness.cases],
                "blinded": True,
                "decision": "accepted",
                "reviewer": "independent-reviewer-1",
                "reviewed_at": "2026-09-05T08:00:00Z",
                "notes": "Review of the prior run.",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="completed run digest"):
        load_blind_review(
            review_path, harness.cases, CORPUS, "sha256:" + "b" * 64
        )


def test_run_digest_changes_when_a_sanitized_trace_artifact_is_tampered(
    tmp_path: Path,
) -> None:
    artifact_dir, results = _completed_run_payload(tmp_path)

    before = benchmark_run_digest(results, 3, artifact_dir)
    (artifact_dir / "traces/case-a/1/codecortex.jsonl").write_text(
        sanitize_trace('{"message":"tampered answer"}'), encoding="utf-8"
    )
    after = benchmark_run_digest(results, 3, artifact_dir)

    assert before != after


def test_tampered_trace_cannot_receive_a_matching_blind_review(tmp_path: Path) -> None:
    artifact_dir, results = _completed_corpus_run_payload(tmp_path)
    run_digest = benchmark_run_digest(results, 3, artifact_dir)
    write_benchmark_report(
        artifact_dir,
        BenchmarkReport(
            status="pending_human_review",
            execution_status="completed",
            corpus_id="tests/benchmark/questions.yaml",
            corpus_digest=corpus_file_digest(CORPUS),
            repetitions=3,
            results=results,  # type: ignore[arg-type]
            run_digest=run_digest,
        ),
    )
    harness = BenchmarkHarness(_config(tmp_path))
    review_path = tmp_path / "blind-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus_digest": corpus_file_digest(CORPUS),
                "run_digest": run_digest,
                "reviewed_case_ids": [case.id for case in harness.cases],
                "blinded": True,
                "decision": "accepted",
                "reviewer": "independent-reviewer-1",
                "reviewed_at": "2026-09-05T08:00:00Z",
                "notes": "Review of the untampered artifacts.",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "traces/affected-source-first/1/codecortex.jsonl").write_text(
        sanitize_trace('{"message":"tampered after review"}'), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="stored run digest"):
        attach_blind_review_to_report(
            artifact_dir, review_path, CORPUS, FIXTURE_ROOT
        )

    persisted = json.loads((artifact_dir / "benchmark_report.json").read_text())
    assert persisted["status"] == "pending_human_review"
    assert persisted["blind_review"] is None


def test_matching_blind_review_attaches_to_the_completed_run(tmp_path: Path) -> None:
    artifact_dir, results = _completed_corpus_run_payload(tmp_path)
    run_digest = benchmark_run_digest(results, 3, artifact_dir)
    write_benchmark_report(
        artifact_dir,
        BenchmarkReport(
            status="pending_human_review",
            execution_status="completed",
            corpus_id="tests/benchmark/questions.yaml",
            corpus_digest=corpus_file_digest(CORPUS),
            repetitions=3,
            results=results,  # type: ignore[arg-type]
            run_digest=run_digest,
        ),
    )
    harness = BenchmarkHarness(_config(tmp_path))
    review_path = tmp_path / "blind-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus_digest": corpus_file_digest(CORPUS),
                "run_digest": run_digest,
                "reviewed_case_ids": [case.id for case in harness.cases],
                "blinded": True,
                "decision": "accepted",
                "reviewer": "independent-reviewer-1",
                "reviewed_at": "2026-09-05T08:00:00Z",
                "notes": "Review of this exact completed run.",
            }
        ),
        encoding="utf-8",
    )

    updated = attach_blind_review_to_report(
        artifact_dir, review_path, CORPUS, FIXTURE_ROOT
    )

    assert updated["status"] == "accepted"
    assert updated["run_digest"] == run_digest
    assert updated["blind_review"]["run_digest"] == run_digest


def test_incomplete_result_matrix_cannot_receive_blind_review(tmp_path: Path) -> None:
    artifact_dir, results = _completed_run_payload(tmp_path)
    run_digest = benchmark_run_digest(results, 3, artifact_dir)
    write_benchmark_report(
        artifact_dir,
        BenchmarkReport(
            status="pending_human_review",
            execution_status="completed",
            corpus_id="tests/benchmark/questions.yaml",
            corpus_digest=corpus_file_digest(CORPUS),
            repetitions=3,
            results=results,  # type: ignore[arg-type]
            run_digest=run_digest,
        ),
    )
    harness = BenchmarkHarness(_config(tmp_path))
    review_path = tmp_path / "blind-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus_digest": corpus_file_digest(CORPUS),
                "run_digest": run_digest,
                "reviewed_case_ids": [case.id for case in harness.cases],
                "blinded": True,
                "decision": "accepted",
                "reviewer": "independent-reviewer-1",
                "reviewed_at": "2026-09-05T08:00:00Z",
                "notes": "The report is missing most paired repetitions.",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="complete case/repetition matrix"):
        attach_blind_review_to_report(
            artifact_dir, review_path, CORPUS, FIXTURE_ROOT
        )


def test_approval_mode_uses_a_persistent_first_turn_and_resume(tmp_path: Path) -> None:
    harness = BenchmarkHarness(_config(tmp_path, execute=True))
    prepared = harness.prepare_case("unmaterialized-expand", repetition=1)

    first = harness.approval_first_command(prepared.case)
    resumed = harness.approval_resume_command("thread-123", "approve exact digest")

    assert "--ephemeral" not in first
    assert first[:3] == ["codex", "exec", "--json"]
    assert resumed[:4] == ["codex", "exec", "resume", "--json"]
    assert "thread-123" in resumed
    harness.cleanup(prepared)
