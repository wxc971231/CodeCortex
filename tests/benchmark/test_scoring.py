"""Deterministic checks for the frozen M1b benchmark scorer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.benchmark.scoring import (
    BenchmarkCase,
    EvidenceExpectation,
    NonRegressionPolicy,
    SourceReference,
    Trace,
    assess_graph_outside_non_regression,
    corpus_tree_digest,
    load_corpus,
    score_answer,
    validate_corpus,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "m1b_repo"
CORPUS_PATH = Path(__file__).with_name("questions.yaml")


def _case(**overrides: object) -> BenchmarkCase:
    values: dict[str, object] = {
        "id": "score-case",
        "fixture_state": "fresh",
        "repository_tree_digest": "sha256:" + "1" * 64,
        "category": "graph_covered_fresh",
        "prompt": "Where is checkpoint persistence implemented?",
        "expected_route": "graph_current",
        "required_facts": ("checkpoint is atomic",),
        "forbidden_claims": ("legacy writer is current",),
        "allowed_evidence": (
            EvidenceExpectation("src/forge/checkpoint.py", 10, 20),
        ),
        "analyzer_permitted": False,
        "materialization_prompt_permitted": False,
        "uncertainty_disclosure_required": False,
        "expected_codecortex_mcp_calls": None,
        "forbid_formal_mutation": True,
        "forbid_cache_mutation": False,
    }
    values.update(overrides)
    return BenchmarkCase(**values)  # type: ignore[arg-type]


def test_score_penalizes_stale_claim_even_when_keywords_match() -> None:
    case = _case()

    score = score_answer(
        case,
        "Checkpoint is atomic. The legacy writer is current.",
        Trace(route="graph_current"),
    )

    assert score.required_fact_recall == 1.0
    assert score.stale_claim_count == 1
    assert score.passed is False
    assert score.human_review_required is True


def test_score_requires_allowed_source_grounding_and_route_compliance() -> None:
    case = _case(uncertainty_disclosure_required=True)
    trace = Trace(
        route="source_first",
        source_references=(SourceReference("src/forge/checkpoint.py", 11, 16),),
    )

    score = score_answer(
        case,
        "Checkpoint is atomic, but the current source needs verification.",
        trace,
    )

    assert score.source_grounding_recall == 1.0
    assert score.uncertainty_disclosed is True
    assert score.route_compliant is False
    assert score.passed is False


def test_score_enforces_native_coding_zero_codecortex_side_effects() -> None:
    case = _case(
        id="ordinary-coding",
        category="ordinary_native_coding",
        expected_route="native_fallback",
        expected_codecortex_mcp_calls=0,
        forbid_cache_mutation=True,
    )
    score = score_answer(
        case,
        "I would update the command parser in src/forge/cli.py.",
        Trace(
            route="native_fallback",
            mcp_tool_names=("repository_overview",),
            cache_mutated=True,
        ),
    )

    assert score.route_compliant is False
    assert score.passed is False


def test_frozen_corpus_validates_fixture_digests_and_ordering() -> None:
    cases = load_corpus(CORPUS_PATH)

    validate_corpus(cases, FIXTURE_ROOT)

    assert [case.id for case in cases] == sorted(case.id for case in cases)
    assert corpus_tree_digest(FIXTURE_ROOT, "fresh").startswith("sha256:")
    assert {case.category for case in cases} >= {
        "graph_covered_fresh",
        "dynamic_l3_l4",
        "unmaterialized_expand",
        "unmaterialized_transient",
        "affected_source_first",
        "pending_unaffected",
        "unknown_scope",
        "graph_outside",
        "cross_responsibility",
        "cognition_source_conflict",
        "cache_deleted",
        "same_topic_followup",
        "ordinary_native_coding",
    }


@pytest.mark.parametrize(
    "mutator",
    (
        lambda payload: payload.__setitem__("threshold", 0.9),
        lambda payload: payload.__setitem__("expected_route", "made_up_route"),
        lambda payload: payload.__setitem__("repository_tree_digest", "sha256:bad"),
    ),
)
def test_corpus_rejects_post_hoc_or_invalid_case_fields(
    tmp_path: Path, mutator: object
) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    first = payload["cases"][0]
    mutator(first)  # type: ignore[operator]
    broken = tmp_path / "questions.yaml"
    broken.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_corpus(broken)


def test_corpus_rejects_duplicate_or_missing_evidence_and_digest(tmp_path: Path) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    first = payload["cases"][0]
    payload["cases"][1]["id"] = first["id"]
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(duplicate)

    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["allowed_evidence"] = []
    no_evidence = tmp_path / "no-evidence.yaml"
    no_evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(no_evidence)

    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["repository_tree_digest"] = ""
    no_digest = tmp_path / "no-digest.yaml"
    no_digest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(no_digest)


def test_corpus_rejects_unknown_fixture_state_after_loading() -> None:
    cases = list(load_corpus(CORPUS_PATH))
    original = cases[0]
    cases[0] = BenchmarkCase(
        **{**original.__dict__, "fixture_state": "missing-state"}
    )

    with pytest.raises(ValueError):
        validate_corpus(cases, FIXTURE_ROOT)


def test_scoring_is_repeatable() -> None:
    case = _case()
    trace = Trace(
        route="graph_current",
        source_references=(SourceReference("src/forge/checkpoint.py", 10, 20),),
        input_tokens=12,
        output_tokens=7,
        latency_ms=123,
    )

    first = score_answer(case, "Checkpoint is atomic.", trace)
    second = score_answer(case, "Checkpoint is atomic.", trace)

    assert first == second


def test_graph_outside_non_regression_uses_pre_frozen_paired_thresholds() -> None:
    case = _case(expected_route="native_fallback")
    native = tuple(
        score_answer(
            case,
            "Checkpoint is atomic.",
            Trace(
                route="native_fallback",
                source_references=(SourceReference("src/forge/checkpoint.py", 10, 12),),
            ),
        )
        for _ in range(3)
    )
    augmented = tuple(
        score_answer(
            case,
            "Checkpoint is atomic.",
            Trace(
                route="native_fallback",
                source_references=(SourceReference("src/forge/checkpoint.py", 10, 12),),
            ),
        )
        for _ in range(3)
    )

    result = assess_graph_outside_non_regression(native, augmented)

    assert result.passed is True
    assert result.repetitions == NonRegressionPolicy().repetitions
    with pytest.raises(ValueError):
        assess_graph_outside_non_regression(native[:2], augmented)
