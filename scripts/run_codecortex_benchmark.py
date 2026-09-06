"""Run the opt-in, isolated Native-versus-CodeCortex Child Codex benchmark.

The default is deliberately non-executing: it validates the frozen corpus and
writes an explicit ``skipped`` artifact.  Passing ``--execute --copy-auth`` is
an intentional user action that may consume model quota; no CI command or
ordinary pytest invocation can trigger a Child Codex process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

_PROJECT_ROOT = Path(__file__).parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.proposals import ManagedSourceSnapshot, ProposalService
from codecortex.domain.analysis import validate_analysis_report
from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    Manifest,
    SourceBaseline,
)
from codecortex.domain.facts import DigestProfile, SourceConfig
from codecortex.domain.proposals import ApprovalRecord
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.python.digest import (
    digest_source_file,
    repository_digest,
)
from codecortex.infrastructure.python.discovery import discover_python_source_set
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views
from codecortex.integrations.codex.install import CONFIG_RELATIVE, install_codex
from tests.benchmark.scoring import (
    BenchmarkCase,
    NonRegressionPolicy,
    ScoreCard,
    assess_graph_outside_non_regression,
    fixture_state_files,
    load_corpus,
    materialize_fixture_state,
    score_answer,
    validate_corpus,
)
from tests.e2e.trace import ParsedTrace, parse_sanitized_trace, sanitize_trace

_DEFAULT_CORPUS = _PROJECT_ROOT / "tests" / "benchmark" / "questions.yaml"
_DEFAULT_FIXTURE_ROOT = _PROJECT_ROOT / "tests" / "fixtures" / "m1b_repo"
_FORMAL_FILES = ("manifest.json", "graph.json", "entity_refs.json", "source_baseline.json")
_FORMAL_GRAPH_TEMPLATE = "formal_graph.json"
_FORMAL_GRAPH_TEMPLATE_DIGEST = (
    "sha256:f5c84c4645ac883bd721f93a607e691e379eeafaa63685403d27fc6d440f3b13"
)


@dataclass(frozen=True)
class BenchmarkConfig:
    corpus: Path = _DEFAULT_CORPUS
    fixture_root: Path = _DEFAULT_FIXTURE_ROOT
    artifact_dir: Path = Path("/tmp/codecortex-m1b-final")
    repetitions: int = NonRegressionPolicy().repetitions
    execute: bool = False
    copy_auth: bool = False
    approval_case_id: str | None = None
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "medium"
    sandbox: str = "read-only"
    timeout_seconds: int = 900
    blind_review_path: Path | None = None

    def __post_init__(self) -> None:
        if self.repetitions != NonRegressionPolicy().repetitions:
            raise ValueError("Benchmark repetitions must equal the pre-frozen policy value")
        if self.timeout_seconds < 1:
            raise ValueError("Benchmark timeout must be positive")


@dataclass(frozen=True)
class PreparedCase:
    case: BenchmarkCase
    repetition: int
    native_repo: Path
    codecortex_repo: Path
    native_commit: str
    codecortex_commit: str
    native_codex_home: Path
    codecortex_codex_home: Path
    workspace: Path


@dataclass(frozen=True)
class ChildResult:
    side: Literal["native", "codecortex"]
    returncode: int
    answer: str
    trace: ParsedTrace
    score: ScoreCard
    trace_path: str


@dataclass(frozen=True)
class BenchmarkCaseResult:
    case_id: str
    repetition: int
    commit: str
    native: ChildResult
    codecortex: ChildResult


@dataclass(frozen=True)
class BenchmarkReport:
    status: Literal["skipped", "pending_human_review", "accepted", "failed"]
    execution_status: Literal["not_started", "completed", "failed"]
    corpus_id: str
    corpus_digest: str
    repetitions: int
    results: tuple[BenchmarkCaseResult, ...] = ()
    graph_outside: dict[str, object] = field(default_factory=dict)
    approval: dict[str, object] = field(default_factory=dict)
    skip_reason: str | None = None
    error: str | None = None
    blind_review: dict[str, object] | None = None
    run_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class BenchmarkExecutionError(RuntimeError):
    """An opted-in real benchmark ran but did not meet its evidence contract."""


@dataclass(frozen=True)
class BlindReview:
    """A human decision bound to the exact frozen corpus and all case IDs."""

    corpus_digest: str
    run_digest: str
    reviewed_case_ids: tuple[str, ...]
    blinded: Literal[True]
    decision: Literal["accepted", "rejected"]
    reviewer: str
    reviewed_at: str
    notes: str
    schema_version: Literal[2] = 2


def corpus_file_digest(path: Path) -> str:
    """Return the stable digest that binds a blind review to corpus bytes."""
    return f"sha256:{hashlib.sha256(Path(path).read_bytes()).hexdigest()}"


def corpus_identifier(path: Path) -> str:
    """Return a stable non-machine-specific corpus identifier."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(_PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return f"external/{resolved.name}"


def benchmark_run_digest(
    results: Iterable[BenchmarkCaseResult | Mapping[str, object]],
    repetitions: int,
    artifact_dir: Path,
) -> str:
    """Bind paired result records to the exact sanitized trace artifacts."""
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("Benchmark run digest requires positive repetitions")
    normalized: list[dict[str, object]] = []
    coordinates: set[tuple[str, int]] = set()
    for result in results:
        payload = asdict(result) if isinstance(result, BenchmarkCaseResult) else dict(result)
        case_id = payload.get("case_id")
        repetition = payload.get("repetition")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Benchmark run result case ID is invalid")
        if type(repetition) is not int or not 1 <= repetition <= repetitions:
            raise ValueError("Benchmark run result repetition is invalid")
        coordinate = (case_id, repetition)
        if coordinate in coordinates:
            raise ValueError("Benchmark run contains a duplicate case repetition")
        coordinates.add(coordinate)
        trace_artifacts: list[dict[str, str]] = []
        for side in ("native", "codecortex"):
            arm = payload.get(side)
            if not isinstance(arm, Mapping) or arm.get("side") != side:
                raise ValueError("Benchmark run result does not contain both paired arms")
            trace_path = arm.get("trace_path")
            if not isinstance(trace_path, str):
                raise TypeError("Benchmark run trace identity must be text")
            trace_artifacts.append(
                {
                    "side": side,
                    "trace_path": trace_path,
                    "content_digest": _sanitized_trace_artifact_digest(
                        artifact_dir, trace_path
                    ),
                }
            )
        normalized.append(
            {
                "case_id": case_id,
                "repetition": repetition,
                "result": payload,
                "trace_artifacts": trace_artifacts,
            }
        )
    if not normalized:
        raise ValueError("Benchmark run digest requires completed paired results")
    normalized.sort(key=lambda item: (str(item["case_id"]), int(item["repetition"])))
    canonical = json.dumps(
        {
            "schema_version": 1,
            "repetitions": repetitions,
            "paired_results": normalized,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _sanitized_trace_artifact_digest(artifact_dir: Path, relative: str) -> str:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("Benchmark trace path must stay within the artifact directory")
    root = Path(artifact_dir).resolve()
    path = root.joinpath(relative_path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("Benchmark trace artifact is missing") from error
    if not resolved.is_relative_to(root) or path.is_symlink() or not resolved.is_file():
        raise ValueError("Benchmark trace artifact is unsafe")
    content = resolved.read_text(encoding="utf-8")
    if sanitize_trace(content) != content:
        raise ValueError("Benchmark trace artifact is not canonically sanitized")
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def load_blind_review(
    path: Path,
    cases: Iterable[BenchmarkCase],
    corpus: Path,
    completed_run_digest: str,
) -> BlindReview:
    """Validate one review against the exact completed run it observed."""
    _validate_sha256_digest(completed_run_digest, "Completed run digest")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    expected_fields = {
        "schema_version",
        "corpus_digest",
        "run_digest",
        "reviewed_case_ids",
        "blinded",
        "decision",
        "reviewer",
        "reviewed_at",
        "notes",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("Blind review has missing or extra fields")
    expected_ids = tuple(sorted(case.id for case in cases))
    reviewed_ids = value["reviewed_case_ids"]
    if not isinstance(reviewed_ids, list) or tuple(reviewed_ids) != expected_ids:
        raise ValueError("Blind review must cover the exact frozen case set")
    if value["schema_version"] != 2:
        raise ValueError("Blind review schema version is unsupported")
    if value["corpus_digest"] != corpus_file_digest(corpus):
        raise ValueError("Blind review corpus digest does not match")
    _validate_sha256_digest(value["run_digest"], "Blind review run digest")
    if value["run_digest"] != completed_run_digest:
        raise ValueError("Blind review completed run digest does not match")
    if value["blinded"] is not True:
        raise ValueError("Blind review must attest that side labels were hidden")
    if value["decision"] not in ("accepted", "rejected"):
        raise ValueError("Blind review decision is invalid")
    for field_name in ("reviewer", "reviewed_at", "notes"):
        if not isinstance(value[field_name], str) or not value[field_name].strip():
            raise ValueError(f"Blind review {field_name} must be non-empty")
    try:
        reviewed_at = datetime.fromisoformat(value["reviewed_at"])
    except ValueError as error:
        raise ValueError("Blind review timestamp must be RFC3339") from error
    if reviewed_at.tzinfo is None:
        raise ValueError("Blind review timestamp must include a timezone")
    return BlindReview(
        corpus_digest=value["corpus_digest"],
        run_digest=value["run_digest"],
        reviewed_case_ids=expected_ids,
        blinded=True,
        decision=value["decision"],
        reviewer=value["reviewer"],
        reviewed_at=value["reviewed_at"],
        notes=value["notes"],
    )


def _validate_sha256_digest(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name} is invalid")


def benchmark_acceptance_status(
    *, automated_failed: bool, blind_review: BlindReview | None
) -> Literal["failed", "pending_human_review", "accepted"]:
    """Keep execution completion distinct from benchmark acceptance."""
    if automated_failed or (
        blind_review is not None and blind_review.decision == "rejected"
    ):
        return "failed"
    if blind_review is None:
        return "pending_human_review"
    return "accepted"


class BenchmarkHarness:
    """Create identical source commits and strictly separated Child Codex homes."""

    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.cases = load_corpus(config.corpus)
        validate_corpus(self.cases, config.fixture_root)

    def prepare_case(self, case_id: str, *, repetition: int) -> PreparedCase:
        case = next((item for item in self.cases if item.id == case_id), None)
        if case is None:
            raise ValueError(f"Unknown benchmark case: {case_id}")
        workspace = Path(
            tempfile.mkdtemp(
                prefix=f"codecortex-m1b-{case.id}-{repetition}-",
                dir=self._workspace_parent(),
            )
        )
        try:
            seed = workspace / "seed"
            materialize_fixture_state(self.config.fixture_root, "fresh", seed)
            self._seed_formal_baseline(seed)
            self._replace_source_state(seed, case.fixture_state)
            self._commit_seed(seed)
            native = workspace / "native"
            augmented = workspace / "codecortex"
            self._clone(seed, native)
            self._clone(seed, augmented)
            if case.fixture_state == "cache-deleted":
                cache = augmented / ".codecortex" / ".cache"
                if cache.exists():
                    shutil.rmtree(cache)
            native_home = workspace / "native-codex-home"
            augmented_home = workspace / "codecortex-codex-home"
            native_home.mkdir()
            augmented_home.mkdir()
            native_commit = self._git(native, "rev-parse", "HEAD")
            augmented_commit = self._git(augmented, "rev-parse", "HEAD")
            if native_commit != augmented_commit:
                raise BenchmarkExecutionError(
                    "Native and CodeCortex copies diverged before Child Codex execution"
                )
            return PreparedCase(
                case=case,
                repetition=repetition,
                native_repo=native,
                codecortex_repo=augmented,
                native_commit=native_commit,
                codecortex_commit=augmented_commit,
                native_codex_home=native_home,
                codecortex_codex_home=augmented_home,
                workspace=workspace,
            )
        except BaseException as preparation_error:
            try:
                self._remove_workspace(workspace)
            except BenchmarkExecutionError as cleanup_error:
                raise cleanup_error from preparation_error
            raise

    def run_case(self, prepared: PreparedCase) -> BenchmarkCaseResult:
        self._install_codecortex_test_home(prepared.codecortex_codex_home)
        native = self._run_child(prepared, "native")
        augmented = self._run_child(prepared, "codecortex")
        return BenchmarkCaseResult(
            case_id=prepared.case.id,
            repetition=prepared.repetition,
            commit=prepared.native_commit,
            native=native,
            codecortex=augmented,
        )

    def approval_first_command(self, case: BenchmarkCase) -> list[str]:
        """Build the first persistent turn of the separate approval test."""
        prompt = (
            f"{case.prompt}\nPrepare at most one CodeCortex Proposal if it is needed. "
            "Do not apply anything in this turn. Stop after displaying the exact "
            "proposal_id and patch_digest; a user will decide in a resumed turn."
        )
        return self._persistent_command(prompt)

    def approval_resume_command(self, session_id: str, prompt: str) -> list[str]:
        """Build the second, same-session approval turn without `--ephemeral`."""
        if not session_id:
            raise ValueError("Approval resume requires a Child Codex session ID")
        return [
            "codex",
            "exec",
            "resume",
            "--json",
            "--model",
            self.config.model,
            "-c",
            f'model_reasoning_effort="{self.config.reasoning_effort}"',
            session_id,
            prompt,
        ]

    def run_approval_case(self, prepared: PreparedCase) -> dict[str, object]:
        """Run an opt-in, two-turn user-approval proof in a throwaway home.

        This is intentionally separate from one-shot score runs.  A missing
        pending Proposal, missing session ID, graph mutation before user
        approval, or malformed applied event is a hard failure—not a skipped
        or synthetic success.
        """
        self._install_codecortex_test_home(prepared.codecortex_codex_home)
        root = prepared.codecortex_repo
        before = _tree_fingerprint(root / ".codecortex", _FORMAL_FILES)
        first = self._execute(
            self.approval_first_command(prepared.case), root, prepared.codecortex_codex_home
        )
        self._write_auxiliary_trace(prepared, "approval-first", first.stdout, first.stderr)
        if first.returncode != 0:
            raise BenchmarkExecutionError("Approval first turn failed")
        if before != _tree_fingerprint(root / ".codecortex", _FORMAL_FILES):
            raise BenchmarkExecutionError("Approval first turn mutated formal cognition")
        pending = sorted((root / ".codecortex" / ".cache" / "pending_proposals").glob("prop_*.json"))
        if len(pending) != 1:
            raise BenchmarkExecutionError("Approval first turn did not create exactly one pending Proposal")
        record = json.loads(pending[0].read_text(encoding="utf-8"))
        proposal_id = record.get("proposal_id")
        patch_digest = record.get("patch_digest")
        if not isinstance(proposal_id, str) or not isinstance(patch_digest, str):
            raise BenchmarkExecutionError("Pending Proposal lacks an exact ID/digest")
        session_id = _session_id(first.stdout)
        if session_id is None:
            raise BenchmarkExecutionError("Approval first turn did not expose a resumable Child Codex session ID")
        second = self._execute(
            self.approval_resume_command(
                session_id,
                f"The user explicitly approves proposal {proposal_id} with current patch_digest "
                f"{patch_digest}. Apply only that exact Proposal using a user approval record.",
            ),
            root,
            prepared.codecortex_codex_home,
        )
        self._write_auxiliary_trace(prepared, "approval-resume", second.stdout, second.stderr)
        if second.returncode != 0:
            raise BenchmarkExecutionError("Approval resume turn failed")
        event = _applied_event(root, proposal_id)
        approval = event.get("approval") if isinstance(event, dict) else None
        if not isinstance(approval, dict) or approval.get("approved_by") != "user":
            raise BenchmarkExecutionError("Applied Proposal lacks a valid user approval record")
        return {"proposal_id": proposal_id, "patch_digest": patch_digest, "event_id": event["event_id"]}

    def cleanup(self, prepared: PreparedCase) -> None:
        """Remove the whole temporary workspace, including copied auth and Git data."""
        self._remove_workspace(prepared.workspace)

    @staticmethod
    def _remove_workspace(workspace: Path) -> None:
        try:
            shutil.rmtree(workspace)
        except OSError as error:
            raise BenchmarkExecutionError(
                f"Benchmark workspace cleanup failed for {workspace.name}"
            ) from error
        if workspace.exists():
            raise BenchmarkExecutionError(
                f"Benchmark workspace cleanup failed for {workspace.name}"
            )

    def _workspace_parent(self) -> str:
        return tempfile.gettempdir()

    def _seed_formal_baseline(self, root: Path) -> None:
        """Apply the pinned, pre-approved semantic fixture to the fresh source tree."""
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        repository = Repository(root)
        lock = RepositoryLock(root)
        sync = FactSyncService(repository, repository_lock=lock)
        first = sync.sync("full")
        FormalStore(repository).initialize(
            FormalState(
                manifest=Manifest(1, 0, False, None),
                graph=CognitiveGraph.empty(),
                entity_refs=EntityRefs.empty(),
                source_baseline=SourceBaseline.empty(),
            )
        )
        self._apply_formal_graph_fixture(repository, lock, sync, first.repository_source_digest)
        (root / ".gitignore").write_text(".codecortex/.cache/\n", encoding="utf-8")

    def _apply_formal_graph_fixture(
        self,
        repository: Repository,
        lock: RepositoryLock,
        sync: FactSyncService,
        source_digest: str,
    ) -> None:
        template_path = self.config.fixture_root / _FORMAL_GRAPH_TEMPLATE
        payload = template_path.read_bytes()
        actual_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if actual_digest != _FORMAL_GRAPH_TEMPLATE_DIGEST:
            raise BenchmarkExecutionError("Pinned formal graph fixture digest drifted")
        template = json.loads(payload)
        if not isinstance(template, dict) or set(template) != {
            "schema_version",
            "template_revision",
            "candidate_nodes",
            "candidate_edges",
            "candidate_flows",
            "candidate_mappings",
        }:
            raise BenchmarkExecutionError("Formal graph fixture shape is invalid")
        if template["schema_version"] != 1 or template["template_revision"] != 1:
            raise BenchmarkExecutionError("Formal graph fixture version is unsupported")
        entities = {item.address: item.uid for item in sync.database.current_entity_snapshots()}
        mappings: list[dict[str, object]] = []
        for raw in template["candidate_mappings"]:
            if not isinstance(raw, dict):
                raise BenchmarkExecutionError("Formal graph mapping fixture is invalid")
            mapping = dict(raw)
            address = mapping.pop("entity_address", None)
            uid = entities.get(address) if isinstance(address, str) else None
            if uid is None:
                raise BenchmarkExecutionError(
                    f"Formal graph mapping address is not present in baseline facts: {address}"
                )
            mapping["entity_uid"] = uid
            mappings.append(mapping)
        change_operations = [
            {
                "kind": kind,
                "target_id": str(candidate[id_field]),
                "before_revision": None,
                "change_kind": "add",
                "change_group": f"seed-{object_kind}-{index:04d}",
            }
            for object_kind, kind, id_field, candidates in (
                ("node", "add_node", "id", template["candidate_nodes"]),
                ("edge", "add_edge", "id", template["candidate_edges"]),
                (
                    "flow",
                    "set_logical_flow",
                    "behavior_id",
                    template["candidate_flows"],
                ),
                ("mapping", "add_mapping", "id", mappings),
            )
            for index, candidate in enumerate(candidates, start=1)
            if isinstance(candidate, dict)
        ]
        report_payload = json.dumps(
            {
                "schema_version": 1,
                "base_graph_revision": 0,
                "analyzed_source_digest": source_digest,
                "analysis_scope": {
                    "mode": "repository",
                    "files": len(sync.database.source_file_digests()),
                    "modules": len(sync.database.source_file_digests()),
                },
                "coverage": {
                    "analyzed_partitions": ["src/forge"],
                    "unexamined_partitions": [],
                },
                "candidate_nodes": template["candidate_nodes"],
                "candidate_edges": template["candidate_edges"],
                "candidate_flows": template["candidate_flows"],
                "candidate_mappings": mappings,
                "change_operations": change_operations,
                "evidence": [],
                "uncertainties": [],
                "unmapped_regions": ["src/forge/cli.py"],
                "diagnostics": [],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        report = validate_analysis_report(report_payload, 0, source_digest)
        formal_store = FormalStore(repository)
        pending = PendingProposalStore(repository)
        service = ProposalService(
            formal_store=formal_store,
            repository_lock=lock,
            pending_proposals=pending,
            view_renderer=render_views,
            fact_sync=sync,
            source_probe=lambda: _probe_managed_sources(repository),
            facts=sync.database,
        )
        proposal = service.create_proposal_from_analysis(
            report,
            "Apply the reviewed M1b benchmark cognition fixture",
            proposal_id="prop_01J00000000000000000000001",
            created_at="2026-09-05T00:00:00Z",
        )
        service.apply_cognitive_proposal(
            proposal.proposal_id,
            ApprovalRecord(
                proposal_id=proposal.proposal_id,
                patch_digest=proposal.patch_digest,
                approved_by="user",
                approved_at="2026-09-05T00:01:00Z",
                approval_summary="Pre-approve the pinned benchmark fixture only.",
            ),
        )
        state = formal_store.load()
        if state.graph.graph_revision != 1 or not state.graph.implementation_mappings:
            raise BenchmarkExecutionError("Formal graph fixture did not materialize")

    def _replace_source_state(self, root: Path, state: str) -> None:
        current = fixture_state_files(self.config.fixture_root, state)
        baseline = fixture_state_files(self.config.fixture_root, "fresh")
        for relative in set(baseline) - set(current):
            (root / relative).unlink()
        for relative, content in current.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def _commit_seed(self, root: Path) -> None:
        self._git(root, "add", "-A")
        self._git(
            root,
            "-c",
            "user.name=CodeCortex Benchmark",
            "-c",
            "user.email=benchmark@codecortex.invalid",
            "commit",
            "-qm",
            "Frozen M1b benchmark fixture",
        )

    @staticmethod
    def _clone(seed: Path, destination: Path) -> None:
        subprocess.run(["git", "clone", "-q", "--no-local", str(seed), str(destination)], check=True)

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def _install_codecortex_test_home(self, home: Path) -> None:
        executable = shutil.which("codecortex")
        if executable is None:
            raise BenchmarkExecutionError("CodeCortex executable is unavailable")
        install_codex(home, Path(executable), dry_run=False, force=False)
        config = home / CONFIG_RELATIVE
        if "approval_mode = \"prompt\"" not in config.read_text(encoding="utf-8"):
            raise BenchmarkExecutionError("Product MCP configuration must retain prompt approval")
        # A Child process runs with non-interactive Codex host approvals.  This
        # affects only a throwaway home; the installed product resource stays
        # at `approval_mode = "prompt"`.
        from tests.e2e.conftest import auto_approve_codecortex_tools

        auto_approve_codecortex_tools(home)
        self._copy_auth(home)

    def _copy_auth(self, home: Path) -> None:
        if not self.config.copy_auth:
            raise BenchmarkExecutionError("Copying browser auth requires explicit --copy-auth")
        source = Path.home() / ".codex" / "auth.json"
        if not source.is_file() or source.is_symlink():
            raise BenchmarkExecutionError("Browser authentication is unavailable for isolated benchmark home")
        target = home / ".codex" / "auth.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)

    def _run_child(self, prepared: PreparedCase, side: Literal["native", "codecortex"]) -> ChildResult:
        repository = prepared.native_repo if side == "native" else prepared.codecortex_repo
        home = prepared.native_codex_home if side == "native" else prepared.codecortex_codex_home
        before_formal = _tree_fingerprint(repository / ".codecortex", _FORMAL_FILES)
        before_cache = _tree_fingerprint(repository / ".codecortex" / ".cache")
        command = self._command(prepared.case, side)
        started = time.perf_counter()
        completed = self._execute(command, repository, home)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        raw = completed.stdout + ("\n" if completed.stdout and completed.stderr else "") + completed.stderr
        sanitized = sanitize_trace(raw)
        trace_path = self._trace_path(prepared, side)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(sanitized, encoding="utf-8")
        parsed = parse_sanitized_trace(sanitized, elapsed_ms=elapsed_ms)
        parsed = ParsedTrace(
            answer=parsed.answer,
            trace=replace(
                parsed.trace,
                route=(
                    "native_fallback"
                    if side == "native"
                    or (side == "codecortex" and not parsed.trace.mcp_tool_names)
                    else parsed.trace.route
                ),
                formal_state_mutated=before_formal != _tree_fingerprint(repository / ".codecortex", _FORMAL_FILES),
                cache_mutated=before_cache != _tree_fingerprint(repository / ".codecortex" / ".cache"),
            ),
        )
        score_case = _native_case(prepared.case) if side == "native" else prepared.case
        return ChildResult(
            side=side,
            returncode=completed.returncode,
            answer=parsed.answer,
            trace=parsed,
            score=score_answer(score_case, parsed.answer, parsed.trace),
            trace_path=trace_path.relative_to(self.config.artifact_dir).as_posix(),
        )

    def _command(self, case: BenchmarkCase, side: Literal["native", "codecortex"]) -> list[str]:
        prompt = case.prompt
        if side == "codecortex":
            prompt += "\nCite accessed source as path:start-end."
        else:
            prompt += "\nCite accessed source as path:start-end."
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--json",
            "--model",
            self.config.model,
            "--sandbox",
            self.config.sandbox,
            "-c",
            f'model_reasoning_effort="{self.config.reasoning_effort}"',
        ]
        if side == "native":
            command.append("--ignore-user-config")
        command.append(prompt)
        return command

    def _persistent_command(self, prompt: str) -> list[str]:
        return [
            "codex",
            "exec",
            "--json",
            "--model",
            self.config.model,
            "--sandbox",
            self.config.sandbox,
            "-c",
            f'model_reasoning_effort="{self.config.reasoning_effort}"',
            prompt,
        ]

    def _execute(
        self, command: list[str], repository: Path, home: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=repository,
            env={**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")},
            capture_output=True,
            text=True,
            check=False,
            timeout=self.config.timeout_seconds,
        )

    def _write_auxiliary_trace(
        self, prepared: PreparedCase, label: str, stdout: str, stderr: str
    ) -> None:
        path = self.config.artifact_dir / "traces" / prepared.case.id / str(prepared.repetition) / f"{label}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sanitize_trace(stdout + ("\n" if stdout and stderr else "") + stderr), encoding="utf-8")

    def _trace_path(self, prepared: PreparedCase, side: str) -> Path:
        return self.config.artifact_dir / "traces" / prepared.case.id / str(prepared.repetition) / f"{side}.jsonl"


def run_benchmark(config: BenchmarkConfig) -> BenchmarkReport:
    """Validate frozen inputs, then run only after explicit paid-model opt-in."""
    if config.blind_review_path is not None:
        raise ValueError(
            "Blind review is a post-execution attachment; do not start a new benchmark run"
        )
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    harness = BenchmarkHarness(config)
    if not config.execute:
        report = BenchmarkReport(
            status="skipped",
            execution_status="not_started",
            corpus_id=corpus_identifier(config.corpus),
            corpus_digest=corpus_file_digest(config.corpus),
            repetitions=config.repetitions,
            skip_reason=(
                "Real Child Codex evaluation is opt-in; rerun with --execute --copy-auth "
                "only when model quota use is authorized."
            ),
        )
        write_benchmark_report(config.artifact_dir, report)
        return report
    if not config.copy_auth:
        report = BenchmarkReport(
            status="skipped",
            execution_status="not_started",
            corpus_id=corpus_identifier(config.corpus),
            corpus_digest=corpus_file_digest(config.corpus),
            repetitions=config.repetitions,
            skip_reason="Real evaluation also requires --copy-auth for the temporary isolated home.",
        )
        write_benchmark_report(config.artifact_dir, report)
        return report
    if shutil.which("codex") is None or shutil.which("codecortex") is None:
        report = BenchmarkReport(
            status="skipped",
            execution_status="not_started",
            corpus_id=corpus_identifier(config.corpus),
            corpus_digest=corpus_file_digest(config.corpus),
            repetitions=config.repetitions,
            skip_reason="Codex or CodeCortex executable is unavailable; no Child process was started.",
        )
        write_benchmark_report(config.artifact_dir, report)
        return report
    if config.approval_case_id is not None:
        prepared = harness.prepare_case(config.approval_case_id, repetition=1)
        try:
            approval = harness.run_approval_case(prepared)
            report = BenchmarkReport(
                status="pending_human_review",
                execution_status="completed",
                corpus_id=corpus_identifier(config.corpus),
                corpus_digest=corpus_file_digest(config.corpus),
                repetitions=config.repetitions,
                approval=approval,
            )
        except (BenchmarkExecutionError, subprocess.SubprocessError, OSError, TimeoutError) as error:
            report = BenchmarkReport(
                status="failed",
                execution_status="failed",
                corpus_id=corpus_identifier(config.corpus),
                corpus_digest=corpus_file_digest(config.corpus),
                repetitions=config.repetitions,
                error=str(error),
            )
        try:
            harness.cleanup(prepared)
        except BenchmarkExecutionError as error:
            report = BenchmarkReport(
                status="failed",
                execution_status="failed",
                corpus_id=corpus_identifier(config.corpus),
                corpus_digest=corpus_file_digest(config.corpus),
                repetitions=config.repetitions,
                error=str(error),
            )
        write_benchmark_report(config.artifact_dir, report)
        return report
    results: list[BenchmarkCaseResult] = []
    try:
        for case in harness.cases:
            for repetition in range(1, config.repetitions + 1):
                prepared = harness.prepare_case(case.id, repetition=repetition)
                try:
                    results.append(harness.run_case(prepared))
                finally:
                    harness.cleanup(prepared)
        graph_outside = _graph_outside_results(results)
        failed = any(
            result.native.returncode != 0
            or result.codecortex.returncode != 0
            or not result.codecortex.score.passed
            for result in results
        ) or not all(bool(item["passed"]) for item in graph_outside.values())
        run_digest = benchmark_run_digest(
            results, config.repetitions, config.artifact_dir
        )
        report = BenchmarkReport(
            status=benchmark_acceptance_status(automated_failed=failed, blind_review=None),
            execution_status="completed",
            corpus_id=corpus_identifier(config.corpus),
            corpus_digest=corpus_file_digest(config.corpus),
            repetitions=config.repetitions,
            results=tuple(results),
            graph_outside=graph_outside,
            run_digest=run_digest,
        )
    except (BenchmarkExecutionError, subprocess.SubprocessError, OSError, TimeoutError) as error:
        report = BenchmarkReport(
            status="failed",
            execution_status="failed",
            corpus_id=corpus_identifier(config.corpus),
            corpus_digest=corpus_file_digest(config.corpus),
            repetitions=config.repetitions,
            results=tuple(results),
            error=str(error),
        )
    write_benchmark_report(config.artifact_dir, report)
    return report


def attach_blind_review_to_report(
    artifact_dir: Path,
    review_path: Path,
    corpus: Path = _DEFAULT_CORPUS,
    fixture_root: Path = _DEFAULT_FIXTURE_ROOT,
) -> dict[str, object]:
    """Attach human acceptance only to verified artifacts from one completed run."""
    cases = load_corpus(corpus)
    validate_corpus(cases, fixture_root)
    report_path = Path(artifact_dir) / "benchmark_report.json"
    value = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Benchmark report must be a JSON object")
    if value.get("execution_status") != "completed":
        raise ValueError("Blind review requires a completed benchmark execution")
    if value.get("status") != "pending_human_review":
        raise ValueError("Benchmark report is not awaiting human review")
    if value.get("corpus_id") != corpus_identifier(corpus):
        raise ValueError("Benchmark report corpus identifier does not match")
    if value.get("corpus_digest") != corpus_file_digest(corpus):
        raise ValueError("Benchmark report corpus digest does not match")
    repetitions = value.get("repetitions")
    results = value.get("results")
    stored_run_digest = value.get("run_digest")
    if type(repetitions) is not int or not isinstance(results, list):
        raise ValueError("Benchmark report completed-run evidence is malformed")
    _validate_completed_result_matrix(results, cases, repetitions)
    _validate_sha256_digest(stored_run_digest, "Benchmark report run digest")
    actual_run_digest = benchmark_run_digest(results, repetitions, artifact_dir)
    if stored_run_digest != actual_run_digest:
        raise ValueError("Benchmark report stored run digest does not match its artifacts")
    review = load_blind_review(review_path, cases, corpus, actual_run_digest)
    value["blind_review"] = asdict(review)
    value["status"] = benchmark_acceptance_status(
        automated_failed=False, blind_review=review
    )
    return _write_benchmark_payload(artifact_dir, value)


def _validate_completed_result_matrix(
    results: Iterable[object], cases: Iterable[BenchmarkCase], repetitions: int
) -> None:
    expected = {
        (case.id, repetition)
        for case in cases
        for repetition in range(1, repetitions + 1)
    }
    actual: set[tuple[str, int]] = set()
    for result in results:
        if not isinstance(result, Mapping):
            raise TypeError("Benchmark report result must be an object")
        case_id = result.get("case_id")
        repetition = result.get("repetition")
        if not isinstance(case_id, str) or type(repetition) is not int:
            raise ValueError("Benchmark report result coordinate is malformed")
        actual.add((case_id, repetition))
    if actual != expected or len(actual) != len(expected):
        raise ValueError("Benchmark report lacks the complete case/repetition matrix")


def _native_case(case: BenchmarkCase) -> BenchmarkCase:
    """Native is evaluated for answer quality, not CodeCortex routing policy."""
    return replace(
        case,
        expected_route="native_fallback",
        analyzer_permitted=True,
        materialization_prompt_permitted=True,
        expected_codecortex_mcp_calls=None,
    )


def _probe_managed_sources(repository: Repository) -> ManagedSourceSnapshot:
    discovered = discover_python_source_set(repository, SourceConfig())
    files = tuple(digest_source_file(source) for source in discovered.sources)
    return ManagedSourceSnapshot(
        repository_source_digest=repository_digest(files, DigestProfile()),
        file_digests={
            item.source.relative_path: item.content_digest for item in files
        },
    )


def _graph_outside_results(results: Iterable[BenchmarkCaseResult]) -> dict[str, object]:
    grouped: dict[str, list[BenchmarkCaseResult]] = {}
    for result in results:
        if result.case_id.startswith("graph-outside-"):
            grouped.setdefault(result.case_id, []).append(result)
    outcome: dict[str, object] = {}
    for case_id, entries in grouped.items():
        ordered = sorted(entries, key=lambda item: item.repetition)
        comparison = assess_graph_outside_non_regression(
            [item.native.score for item in ordered], [item.codecortex.score for item in ordered]
        )
        outcome[case_id] = asdict(comparison)
    return outcome


def _session_id(raw: str) -> str | None:
    """Find the documented JSONL session/thread field without persisting raw output."""
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key, item in _walk_json(value):
            if key in {"session_id", "thread_id"} and isinstance(item, str) and item:
                return item
    return None


def _walk_json(value: object) -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _applied_event(root: Path, proposal_id: str) -> dict[str, object]:
    for path in sorted((root / ".codecortex" / "history" / "events").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("event_type") == "cognitive_proposal_applied" and value.get("proposal_id") == proposal_id:
            return value
    raise BenchmarkExecutionError("Approval resume did not create a matching applied-history event")


def _tree_fingerprint(root: Path, allowed: tuple[str, ...] | None = None) -> tuple[tuple[str, str], ...]:
    if not root.exists():
        return ()
    files = (
        (root / relative for relative in allowed)
        if allowed is not None
        else (path for path in root.rglob("*") if path.is_file())
    )
    records = []
    for path in files:
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            records.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(sorted(records))


def write_benchmark_report(artifact_dir: Path, report: BenchmarkReport) -> None:
    """Persist only a structurally sanitized benchmark report."""
    _write_benchmark_payload(artifact_dir, report.to_dict())


def _write_benchmark_payload(
    artifact_dir: Path, payload: Mapping[str, object]
) -> dict[str, object]:
    serialized = sanitize_trace(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    )
    sanitized = json.loads(serialized)
    (artifact_dir / "benchmark_report.json").write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sanitized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=_DEFAULT_CORPUS)
    parser.add_argument("--fixture-root", type=Path, default=_DEFAULT_FIXTURE_ROOT)
    parser.add_argument("--repetitions", type=int, default=NonRegressionPolicy().repetitions)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="explicitly allow real Child Codex processes")
    parser.add_argument("--copy-auth", action="store_true", help="copy browser auth into throwaway homes")
    parser.add_argument(
        "--approval-case",
        help="run only the separate persistent approval/resume proof for this frozen case ID",
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--sandbox", default="read-only")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--blind-review",
        type=Path,
        help="validated blinded human-review JSON for the exact frozen corpus",
    )
    arguments = parser.parse_args(argv)
    if arguments.blind_review is not None:
        if arguments.execute or arguments.approval_case is not None:
            parser.error(
                "--blind-review attaches to an existing completed report; "
                "do not combine it with --execute or --approval-case"
            )
        try:
            attached = attach_blind_review_to_report(
                arguments.artifact_dir,
                arguments.blind_review,
                arguments.corpus,
                arguments.fixture_root,
            )
        except (OSError, TypeError, ValueError) as error:
            print(
                json.dumps(
                    {
                        "status": "pending_human_review",
                        "error": sanitize_trace(str(error)).strip(),
                    }
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "status": attached["status"],
                    "artifact": str(
                        arguments.artifact_dir / "benchmark_report.json"
                    ),
                }
            )
        )
        return 1 if attached["status"] == "failed" else 0
    report = run_benchmark(
        BenchmarkConfig(
            corpus=arguments.corpus,
            fixture_root=arguments.fixture_root,
            artifact_dir=arguments.artifact_dir,
            repetitions=arguments.repetitions,
            execute=arguments.execute,
            copy_auth=arguments.copy_auth,
            approval_case_id=arguments.approval_case,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            sandbox=arguments.sandbox,
            timeout_seconds=arguments.timeout_seconds,
        )
    )
    print(json.dumps({"status": report.status, "artifact": str(arguments.artifact_dir / "benchmark_report.json")}))
    if report.status == "skipped":
        return 2
    return 1 if report.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
