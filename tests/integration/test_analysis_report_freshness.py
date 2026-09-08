"""Freshness chain around Analyzer dispatch over real temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.initialize import AnalysisCoordinate, InitializationService
from codecortex.application.services import ApplicationServices
from codecortex.domain.analysis import AnalysisReport
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import ApprovalRecord, PatchOperation
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views
from tests.conftest import analysis_report_bytes, analysis_report_dict

APPROVED_AT = "2026-09-03T02:00:00Z"


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repository(repo_root: Path) -> Repository:
    _write(repo_root, "pkg/a.py", "value = 1\n")
    _write(repo_root, "pkg/b.py", "other = 2\n")
    return Repository(repo_root)


@pytest.fixture
def app(repository: Repository) -> ApplicationServices:
    return ApplicationServices(
        repository=repository,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(repository.root),
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
    )


@pytest.fixture
def initialization(repository: Repository) -> InitializationService:
    return InitializationService(
        formal_store=FormalStore(repository),
        fact_sync=FactSyncService(repository),
        repository_lock=RepositoryLock(repository.root),
    )


def _fresh_report(coordinate: AnalysisCoordinate) -> bytes:
    return analysis_report_bytes(
        analysis_report_dict(
            base_graph_revision=coordinate.graph_revision,
            analyzed_source_digest=coordinate.source_digest,
        )
    )


def _advance_graph_revision(app: ApplicationServices) -> None:
    proposal = app.create_cognitive_proposal(
        operations=(
            PatchOperation(
                "add_node",
                "behavior.answer-question",
                {"id": "behavior.answer-question", "kind": "behavior", "title": "Answer"},
            ),
        ),
        affected_nodes=("behavior.answer-question",),
        reason="Unrelated cognition work",
    )
    app.apply_cognitive_proposal(
        proposal.proposal_id,
        ApprovalRecord(
            proposal_id=proposal.proposal_id,
            patch_digest=proposal.patch_digest,
            approved_by="user",
            approved_at=APPROVED_AT,
            approval_summary="Approve current patch",
        ),
    )


def test_begin_analysis_records_current_source_digest_and_revision(
    app: ApplicationServices,
    initialization: InitializationService,
    repository: Repository,
) -> None:
    app.initialize_repository()

    coordinate = initialization.begin_analysis()

    expected = FactSyncService(repository).sync()
    assert coordinate.graph_revision == 0
    assert coordinate.source_digest == expected.repository_source_digest


def test_begin_analysis_retries_when_source_changes_after_fact_sync(
    app: ApplicationServices,
    initialization: InitializationService,
    repo_root: Path,
) -> None:
    app.initialize_repository()
    sync = initialization._fact_sync
    original_probe = sync.probe_source_digest
    raced = False

    def mutate_before_first_final_probe() -> str:
        nonlocal raced
        if not raced:
            raced = True
            _write(repo_root, "pkg/a.py", "value = 3\n")
        return original_probe()

    with (
        patch.object(
            sync,
            "probe_source_digest",
            side_effect=mutate_before_first_final_probe,
        ),
        patch.object(sync, "sync", wraps=sync.sync) as synchronize,
    ):
        coordinate = initialization.begin_analysis()

    assert synchronize.call_count == 2
    assert coordinate.source_digest == original_probe()


def test_consume_fails_closed_when_live_digest_never_matches_synced_cache(
    app: ApplicationServices,
    initialization: InitializationService,
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()
    sync = initialization._fact_sync

    with patch.object(
        sync, "probe_source_digest", return_value="sha256:" + "f" * 64
    ), pytest.raises(CodeCortexError) as raised:
        initialization.consume_report(_fresh_report(coordinate))

    assert raised.value.code is ErrorCode.PROPOSAL_STALE


def test_consume_accepts_report_matching_the_recorded_coordinate(
    app: ApplicationServices, initialization: InitializationService
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()

    report = initialization.consume_report(_fresh_report(coordinate))

    assert isinstance(report, AnalysisReport)
    assert report.base_graph_revision == coordinate.graph_revision
    assert report.analyzed_source_digest == coordinate.source_digest


def test_consume_rejects_report_after_managed_source_changes(
    app: ApplicationServices,
    initialization: InitializationService,
    repo_root: Path,
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()
    payload = _fresh_report(coordinate)

    _write(repo_root, "pkg/a.py", "value = 2\n")

    with pytest.raises(CodeCortexError) as exc:
        initialization.consume_report(payload)
    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_consume_rejects_report_after_new_source_file_appears(
    app: ApplicationServices,
    initialization: InitializationService,
    repo_root: Path,
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()
    payload = _fresh_report(coordinate)

    _write(repo_root, "pkg/c.py", "extra = 3\n")

    with pytest.raises(CodeCortexError) as exc:
        initialization.consume_report(payload)
    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_consume_rejects_report_after_graph_revision_advances(
    app: ApplicationServices, initialization: InitializationService
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()
    payload = _fresh_report(coordinate)

    _advance_graph_revision(app)

    with pytest.raises(CodeCortexError) as exc:
        initialization.consume_report(payload)
    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_consume_rejects_forged_digest_even_without_any_change(
    app: ApplicationServices, initialization: InitializationService
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()
    forged = analysis_report_bytes(
        analysis_report_dict(
            base_graph_revision=coordinate.graph_revision,
            analyzed_source_digest="sha256:" + "f" * 64,
        )
    )

    with pytest.raises(CodeCortexError) as exc:
        initialization.consume_report(forged)
    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_consume_rejects_oversized_payload(
    app: ApplicationServices, initialization: InitializationService
) -> None:
    app.initialize_repository()
    coordinate = initialization.begin_analysis()
    payload = _fresh_report(coordinate)
    oversized = payload + b" " * (512 * 1024)

    with pytest.raises(CodeCortexError) as exc:
        initialization.consume_report(oversized)
    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_validated_report_creates_exactly_one_aggregate_proposal(
    app: ApplicationServices, repository: Repository
) -> None:
    class RecordingProposalService:
        def __init__(self) -> None:
            self.calls: list[tuple[AnalysisReport, str]] = []

        def create_proposal_from_analysis(
            self, report: AnalysisReport, reason: str
        ) -> object:
            self.calls.append((report, reason))
            return object()

    app.initialize_repository()
    recorder = RecordingProposalService()
    service = InitializationService(
        formal_store=FormalStore(repository),
        fact_sync=FactSyncService(repository),
        repository_lock=RepositoryLock(repository.root),
        proposal_service=recorder,
    )
    coordinate = service.begin_analysis()

    proposal = service.create_aggregate_proposal(
        _fresh_report(coordinate), "Initialize repository understanding"
    )

    assert proposal is not None
    assert len(recorder.calls) == 1
    report, reason = recorder.calls[0]
    assert report.analyzed_source_digest == coordinate.source_digest
    assert reason == "Initialize repository understanding"


def test_invalid_report_never_reaches_proposal_creation(
    app: ApplicationServices, repository: Repository
) -> None:
    class ForbiddenProposalService:
        def create_proposal_from_analysis(
            self, report: AnalysisReport, reason: str
        ) -> object:
            raise AssertionError("invalid report must not create a Proposal")

    app.initialize_repository()
    service = InitializationService(
        formal_store=FormalStore(repository),
        fact_sync=FactSyncService(repository),
        repository_lock=RepositoryLock(repository.root),
        proposal_service=ForbiddenProposalService(),
    )
    with pytest.raises(CodeCortexError) as exc:
        service.create_aggregate_proposal(b"{}", "Initialize")
    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID
