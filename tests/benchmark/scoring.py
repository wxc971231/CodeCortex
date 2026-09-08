"""Frozen corpus loading, fixture verification, and deterministic scoring.

The corpus file deliberately uses JSON syntax, which is a strict YAML subset.
That keeps the benchmark self-contained: production CodeCortex has no YAML
parser dependency and an evaluator cannot silently reinterpret YAML features.
Semantic correctness remains a blinded human-review decision; this module only
scores the pre-frozen observable criteria.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_SCHEMA_VERSION = 1
_CORPUS_SCHEMA_VERSION = 2
_DIGEST_PREFIX = "sha256:"
_DIGEST_LENGTH = len(_DIGEST_PREFIX) + 64
_ROUTES = frozenset(
    {
        "graph_current",
        "graph_unaffected",
        "source_first",
        "native_fallback",
        "offer_materialization",
    }
)
_CATEGORIES = frozenset(
    {
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
)
_CASE_FIELDS = frozenset(
    {
        "id",
        "fixture_state",
        "repository_tree_digest",
        "category",
        "prompt",
        "expected_route",
        "required_facts",
        "forbidden_claims",
        "allowed_evidence",
        "analyzer_permitted",
        "materialization_prompt_permitted",
        "uncertainty_disclosure_required",
        "expected_codecortex_mcp_calls",
        "forbid_formal_mutation",
        "forbid_cache_mutation",
    }
)
_UNCERTAINTY_MARKERS = (
    "uncertain",
    "not confirmed",
    "needs verification",
    "verify",
    "current source",
    "不确定",
    "需核验",
    "需要核验",
    "当前源码",
)


@dataclass(frozen=True)
class EvidenceExpectation:
    relative_path: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        _relative_path(self.relative_path, "evidence relative_path")
        if (
            type(self.start_line) is not int
            or type(self.end_line) is not int
            or self.start_line < 1
            or self.end_line < self.start_line
        ):
            raise ValueError("Evidence line range must be positive and ordered")


@dataclass(frozen=True)
class SourceReference:
    relative_path: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        _relative_path(self.relative_path, "source reference relative_path")
        if (
            type(self.start_line) is not int
            or type(self.end_line) is not int
            or self.start_line < 1
            or self.end_line < self.start_line
        ):
            raise ValueError("Source reference line range must be positive and ordered")


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    fixture_state: str
    repository_tree_digest: str
    category: str
    prompt: str
    expected_route: str
    required_facts: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
    allowed_evidence: tuple[EvidenceExpectation, ...]
    analyzer_permitted: bool
    materialization_prompt_permitted: bool
    uncertainty_disclosure_required: bool
    expected_codecortex_mcp_calls: int | None
    forbid_formal_mutation: bool
    forbid_cache_mutation: bool
    initial_prompt: str | None = None

    def __post_init__(self) -> None:
        if not _identifier(self.id):
            raise ValueError("Benchmark case id must be lowercase kebab-case")
        if not _identifier(self.fixture_state):
            raise ValueError("Fixture state must be lowercase kebab-case")
        if not _digest(self.repository_tree_digest):
            raise ValueError("Repository tree digest must be SHA-256")
        if self.category not in _CATEGORIES:
            raise ValueError("Benchmark case category is invalid")
        if self.expected_route not in _ROUTES:
            raise ValueError("Benchmark expected route is invalid")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("Benchmark prompt must be non-empty")
        if self.category == "same_topic_followup":
            if not isinstance(self.initial_prompt, str) or not self.initial_prompt.strip():
                raise ValueError("Follow-up case requires a frozen initial prompt")
        elif self.initial_prompt is not None:
            raise ValueError("Only follow-up cases may declare an initial prompt")
        _non_empty_texts(self.required_facts, "required facts")
        _non_empty_texts(self.forbidden_claims, "forbidden claims")
        if not self.allowed_evidence:
            raise ValueError("Every benchmark case must declare source evidence")
        if any(not isinstance(item, EvidenceExpectation) for item in self.allowed_evidence):
            raise TypeError("Allowed evidence must be EvidenceExpectation records")
        if any(
            type(value) is not bool
            for value in (
                self.analyzer_permitted,
                self.materialization_prompt_permitted,
                self.uncertainty_disclosure_required,
                self.forbid_formal_mutation,
                self.forbid_cache_mutation,
            )
        ):
            raise TypeError("Benchmark policy flags must be bool")
        if self.expected_codecortex_mcp_calls is not None and (
            type(self.expected_codecortex_mcp_calls) is not int
            or self.expected_codecortex_mcp_calls < 0
        ):
            raise ValueError("Expected MCP call count must be non-negative or null")
        if self.category == "ordinary_native_coding" and (
            self.expected_codecortex_mcp_calls != 0
            or not self.forbid_formal_mutation
            or not self.forbid_cache_mutation
        ):
            raise ValueError("Ordinary Native coding must forbid all CodeCortex side effects")


@dataclass(frozen=True)
class Trace:
    """Sanitized observable facts from one isolated Child Codex run."""

    route: str | None = None
    cognitive_anchor_ids: tuple[str, ...] = ()
    source_references: tuple[SourceReference, ...] = ()
    mcp_tool_names: tuple[str, ...] = ()
    analyzer_tool_call_count: int = 0
    materialization_prompt_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    formal_state_mutated: bool = False
    cache_mutated: bool = False

    def __post_init__(self) -> None:
        if self.route is not None and self.route not in _ROUTES:
            raise ValueError("Trace route is invalid")
        if self.cognitive_anchor_ids != tuple(sorted(set(self.cognitive_anchor_ids))):
            raise ValueError("Trace cognitive anchor IDs must be sorted and unique")
        if any(not isinstance(item, SourceReference) for item in self.source_references):
            raise TypeError("Trace source references must be SourceReference records")
        if any(not isinstance(name, str) or not name for name in self.mcp_tool_names):
            raise ValueError("Trace MCP tool names must be non-empty strings")
        for value, name in (
            (self.analyzer_tool_call_count, "Analyzer tool-call count"),
            (self.materialization_prompt_count, "materialization prompt count"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"Trace {name} must be non-negative")
        for value, name in (
            (self.input_tokens, "input tokens"),
            (self.output_tokens, "output tokens"),
            (self.latency_ms, "latency"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Trace {name} must be non-negative or null")


@dataclass(frozen=True)
class ScoreCard:
    required_fact_recall: float
    source_grounding_recall: float
    stale_claim_count: int
    uncertainty_disclosed: bool
    route_compliant: bool
    analyzer_tool_call_count: int
    materialization_prompt_count: int
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    passed: bool
    human_review_required: bool = True


@dataclass(frozen=True)
class NonRegressionPolicy:
    """Pre-frozen graph-outside comparison thresholds; never corpus data."""

    repetitions: int = 3
    max_mean_fact_recall_decline: float = 0.10
    max_mean_grounding_recall_decline: float = 0.10
    max_mean_stale_claim_increase: float = 0.0


@dataclass(frozen=True)
class NonRegressionResult:
    passed: bool
    fact_recall_delta: float
    grounding_recall_delta: float
    stale_claim_delta: float
    repetitions: int


def score_answer(case: BenchmarkCase, answer: str, trace: Trace) -> ScoreCard:
    """Score frozen observable criteria without pretending to judge semantics."""
    if not isinstance(answer, str):
        raise TypeError("Benchmark answer must be text")
    if not isinstance(trace, Trace):
        raise TypeError("Benchmark trace must be a Trace record")
    normalized = _normalize(answer)
    required_matches = sum(
        _normalize(fact) in normalized for fact in case.required_facts
    )
    stale_claim_count = sum(
        _normalize(claim) in normalized for claim in case.forbidden_claims
    )
    grounding_matches = sum(
        _has_grounding(evidence, trace.source_references)
        for evidence in case.allowed_evidence
    )
    uncertainty_disclosed = any(marker in normalized for marker in _UNCERTAINTY_MARKERS)
    route_compliant = _route_compliant(case, trace)
    recall = required_matches / len(case.required_facts)
    grounding = grounding_matches / len(case.allowed_evidence)
    passed = (
        recall == 1.0
        and grounding == 1.0
        and stale_claim_count == 0
        and (not case.uncertainty_disclosure_required or uncertainty_disclosed)
        and route_compliant
    )
    return ScoreCard(
        required_fact_recall=recall,
        source_grounding_recall=grounding,
        stale_claim_count=stale_claim_count,
        uncertainty_disclosed=uncertainty_disclosed,
        route_compliant=route_compliant,
        analyzer_tool_call_count=trace.analyzer_tool_call_count,
        materialization_prompt_count=trace.materialization_prompt_count,
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        latency_ms=trace.latency_ms,
        passed=passed,
    )


def assess_graph_outside_non_regression(
    native: Sequence[ScoreCard],
    codecortex: Sequence[ScoreCard],
    *,
    policy: NonRegressionPolicy | None = None,
) -> NonRegressionResult:
    """Apply the fixed paired graph-outside threshold before human review."""
    if policy is None:
        policy = NonRegressionPolicy()
    if len(native) != policy.repetitions or len(codecortex) != policy.repetitions:
        raise ValueError("Graph-outside comparison must contain exactly frozen repetitions")
    if not native or not codecortex:
        raise ValueError("Graph-outside comparison cannot be empty")
    fact_delta = _mean(card.required_fact_recall for card in codecortex) - _mean(
        card.required_fact_recall for card in native
    )
    grounding_delta = _mean(card.source_grounding_recall for card in codecortex) - _mean(
        card.source_grounding_recall for card in native
    )
    stale_delta = _mean(card.stale_claim_count for card in codecortex) - _mean(
        card.stale_claim_count for card in native
    )
    return NonRegressionResult(
        passed=(
            fact_delta >= -policy.max_mean_fact_recall_decline
            and grounding_delta >= -policy.max_mean_grounding_recall_decline
            and stale_delta <= policy.max_mean_stale_claim_increase
        ),
        fact_recall_delta=fact_delta,
        grounding_recall_delta=grounding_delta,
        stale_claim_delta=stale_delta,
        repetitions=policy.repetitions,
    )


def load_corpus(path: Path) -> tuple[BenchmarkCase, ...]:
    """Load strict JSON-subset YAML, rejecting post-hoc policy fields."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Benchmark corpus must be JSON-compatible YAML") from error
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "cases"}:
        raise ValueError("Benchmark corpus has an invalid top-level shape")
    if raw["schema_version"] != _CORPUS_SCHEMA_VERSION or not isinstance(raw["cases"], list):
        raise ValueError("Benchmark corpus schema is unsupported")
    cases = tuple(_case_from_mapping(item) for item in raw["cases"])
    ids = tuple(case.id for case in cases)
    if not cases or ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
        raise ValueError("Benchmark case IDs must be unique and deterministically sorted")
    return cases


def validate_corpus(cases: Sequence[BenchmarkCase], fixture_root: Path) -> None:
    """Pin every corpus case to one materializable fixture tree and evidence."""
    state_names = set(_state_mutations(fixture_root))
    for case in cases:
        if case.fixture_state not in state_names:
            raise ValueError(f"Unknown benchmark fixture state: {case.fixture_state}")
        actual = corpus_tree_digest(fixture_root, case.fixture_state)
        if actual != case.repository_tree_digest:
            raise ValueError(f"Fixture tree digest drifted for case: {case.id}")
        files = fixture_state_files(fixture_root, case.fixture_state)
        for evidence in case.allowed_evidence:
            content = files.get(evidence.relative_path)
            if content is None:
                raise ValueError(f"Evidence path is absent for case: {case.id}")
            if evidence.end_line > len(content.decode("utf-8").splitlines()):
                raise ValueError(f"Evidence line range is absent for case: {case.id}")


def fixture_state_files(fixture_root: Path, state: str) -> dict[str, bytes]:
    """Return one mutable fixture state as a path-sorted source tree."""
    if not _identifier(state):
        raise ValueError("Fixture state must be lowercase kebab-case")
    base = Path(fixture_root) / "base"
    if not base.is_dir():
        raise ValueError("Benchmark fixture base tree is missing")
    files = {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }
    mutation = _state_mutations(fixture_root).get(state)
    if mutation is None:
        raise ValueError(f"Unknown benchmark fixture state: {state}")
    for path in mutation["delete"]:
        files.pop(path, None)
    files.update(mutation["replace"])
    return dict(sorted(files.items()))


def materialize_fixture_state(fixture_root: Path, state: str, destination: Path) -> None:
    """Materialize exactly one verified tree; the caller may then make a Git commit."""
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Benchmark fixture destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    for relative, content in fixture_state_files(fixture_root, state).items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def corpus_tree_digest(fixture_root: Path, state: str) -> str:
    """Versioned deterministic digest of a materialized fixture tree."""
    payload = bytearray(b"codecortex-m1b-benchmark-tree-v1\0")
    for relative, content in fixture_state_files(fixture_root, state).items():
        payload.extend(relative.encode("utf-8"))
        payload.extend(b"\0")
        payload.extend(hashlib.sha256(content).hexdigest().encode("ascii"))
        payload.extend(b"\n")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _case_from_mapping(value: object) -> BenchmarkCase:
    if not isinstance(value, Mapping):
        raise TypeError("Benchmark case must be a mapping")
    fields = _CASE_FIELDS | {"initial_prompt"} if value.get("category") == "same_topic_followup" else _CASE_FIELDS
    if set(value) != fields:
        raise ValueError("Benchmark case has missing, extra, or post-hoc fields")
    evidence_raw = value["allowed_evidence"]
    if not isinstance(evidence_raw, list):
        raise TypeError("Benchmark allowed_evidence must be a list")
    evidence = tuple(_evidence_from_mapping(item) for item in evidence_raw)
    return BenchmarkCase(
        id=_text(value["id"], "id"),
        fixture_state=_text(value["fixture_state"], "fixture_state"),
        repository_tree_digest=_text(value["repository_tree_digest"], "repository_tree_digest"),
        category=_text(value["category"], "category"),
        prompt=_text(value["prompt"], "prompt"),
        initial_prompt=(
            _text(value["initial_prompt"], "initial_prompt")
            if "initial_prompt" in value else None
        ),
        expected_route=_text(value["expected_route"], "expected_route"),
        required_facts=_texts(value["required_facts"], "required_facts"),
        forbidden_claims=_texts(value["forbidden_claims"], "forbidden_claims"),
        allowed_evidence=evidence,
        analyzer_permitted=_bool(value["analyzer_permitted"], "analyzer_permitted"),
        materialization_prompt_permitted=_bool(
            value["materialization_prompt_permitted"], "materialization_prompt_permitted"
        ),
        uncertainty_disclosure_required=_bool(
            value["uncertainty_disclosure_required"], "uncertainty_disclosure_required"
        ),
        expected_codecortex_mcp_calls=_optional_non_negative_int(
            value["expected_codecortex_mcp_calls"], "expected_codecortex_mcp_calls"
        ),
        forbid_formal_mutation=_bool(value["forbid_formal_mutation"], "forbid_formal_mutation"),
        forbid_cache_mutation=_bool(value["forbid_cache_mutation"], "forbid_cache_mutation"),
    )


def _evidence_from_mapping(value: object) -> EvidenceExpectation:
    if not isinstance(value, Mapping) or set(value) != {
        "relative_path", "start_line", "end_line"
    }:
        raise ValueError("Benchmark evidence has an invalid shape")
    return EvidenceExpectation(
        _text(value["relative_path"], "evidence relative_path"),
        _integer(value["start_line"], "evidence start_line"),
        _integer(value["end_line"], "evidence end_line"),
    )


def _state_mutations(fixture_root: Path) -> dict[str, dict[str, object]]:
    path = Path(fixture_root) / "states.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Benchmark fixture states are unreadable") from error
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "states"}:
        raise ValueError("Benchmark fixture states have an invalid shape")
    if raw["schema_version"] != _SCHEMA_VERSION or not isinstance(raw["states"], Mapping):
        raise ValueError("Benchmark fixture states schema is unsupported")
    result: dict[str, dict[str, object]] = {}
    for state, value in raw["states"].items():
        if not isinstance(state, str) or not _identifier(state):
            raise ValueError("Benchmark fixture state ID is invalid")
        if not isinstance(value, Mapping) or set(value) != {"delete", "replace"}:
            raise ValueError("Benchmark fixture mutation has an invalid shape")
        delete = _texts(value["delete"], "fixture delete", allow_empty=True)
        replace_raw = value["replace"]
        if not isinstance(replace_raw, Mapping):
            raise TypeError("Fixture replacements must be an object")
        replace: dict[str, bytes] = {}
        for relative, content in replace_raw.items():
            relative = _text(relative, "fixture replacement path")
            _relative_path(relative, "fixture replacement path")
            replace[relative] = _text(content, "fixture replacement content").encode("utf-8")
        if set(delete) & set(replace):
            raise ValueError("Fixture mutation cannot replace and delete one path")
        result[state] = {"delete": delete, "replace": replace}
    return result


def _route_compliant(case: BenchmarkCase, trace: Trace) -> bool:
    if trace.route != case.expected_route:
        return False
    if case.category == "unmaterialized_expand" and (
        not any(
            "create_cognitive_proposal_from_analysis" in name
            for name in trace.mcp_tool_names
        )
        or any("apply_cognitive_proposal" in name for name in trace.mcp_tool_names)
    ):
        return False
    if case.category == "unmaterialized_transient" and any(
        fragment in name
        for name in trace.mcp_tool_names
        for fragment in (
            "create_cognitive_proposal",
            "revise_cognitive_proposal",
            "apply_cognitive_proposal",
            "advance_cognition_baseline",
        )
    ):
        return False
    if not case.analyzer_permitted and trace.analyzer_tool_call_count:
        return False
    if not case.materialization_prompt_permitted and trace.materialization_prompt_count:
        return False
    if (
        case.expected_codecortex_mcp_calls is not None
        and len(trace.mcp_tool_names) != case.expected_codecortex_mcp_calls
    ):
        return False
    if case.forbid_formal_mutation and trace.formal_state_mutated:
        return False
    return not (case.forbid_cache_mutation and trace.cache_mutated)


def _has_grounding(
    expected: EvidenceExpectation, observed: Iterable[SourceReference]
) -> bool:
    return any(
        item.relative_path == expected.relative_path
        and item.start_line <= expected.end_line
        and item.end_line >= expected.start_line
        for item in observed
    )


def _mean(values: Iterable[float | int]) -> float:
    materialized = tuple(values)
    return sum(materialized) / len(materialized)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value))


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == _DIGEST_LENGTH and bool(
        re.fullmatch(r"sha256:[0-9a-f]{64}", value)
    )


def _relative_path(value: object, name: str) -> str:
    text = _text(value, name)
    if text.startswith("/") or "\\" in text or ".." in Path(text).parts:
        raise ValueError(f"{name} must be a safe POSIX relative path")
    return text


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _texts(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    values = tuple(_text(item, name) for item in value)
    if not values and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    return values


def _non_empty_texts(values: tuple[str, ...], name: str) -> None:
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"Benchmark {name} must be non-empty text")


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be bool")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _optional_non_negative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    result = _integer(value, name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result
