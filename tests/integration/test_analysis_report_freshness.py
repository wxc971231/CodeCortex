"""Freshness chain around Analyzer dispatch over real temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
