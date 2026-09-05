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
    BenchmarkHarness,
    BenchmarkReport,
    benchmark_acceptance_status,
    corpus_file_digest,
    load_blind_review,
    run_benchmark,
    write_benchmark_report,
)
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
                "schema_version": 1,
                "corpus_digest": corpus_file_digest(CORPUS),
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

    review = load_blind_review(review_path, harness.cases, CORPUS)

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
                "schema_version": 1,
                "corpus_digest": corpus_file_digest(CORPUS),
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
        load_blind_review(review_path, harness.cases, CORPUS)


def test_approval_mode_uses_a_persistent_first_turn_and_resume(tmp_path: Path) -> None:
    harness = BenchmarkHarness(_config(tmp_path, execute=True))
    prepared = harness.prepare_case("unmaterialized-expand", repetition=1)

    first = harness.approval_first_command(prepared.case)
    resumed = harness.approval_resume_command("thread-123", "approve exact digest")

    assert "--ephemeral" not in first
    assert first[:3] == ["codex", "exec", "--json"]
    assert resumed[:4] == ["codex", "exec", "resume", "--json"]
    assert "thread-123" in resumed
