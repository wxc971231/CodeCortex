import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import ApprovalRecord, PatchOperation, ProposalStatus
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import Repository

PROPOSAL_ID = "prop_01J00000000000000000000000"
CREATED_AT = "2026-09-02T01:23:45Z"
REVISED_AT = "2026-09-02T01:24:45Z"


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def app(repo_root: Path) -> ApplicationServices:
    repository = Repository(repo_root)
    return ApplicationServices(
        repository=repository,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(repo_root),
        pending_proposals=PendingProposalStore(repository),
    )


def add_node(node_id: str = "behavior.answer-question") -> PatchOperation:
    return PatchOperation(
        "add_node",
        node_id,
        {"id": node_id, "kind": "behavior", "title": "Answer a question"},
    )


def test_create_persists_deterministic_pending_proposal_bound_to_current_graph(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Skipping persistence or revision binding would lose the reviewed pending state."""
    app.initialize_repository()

    proposal = app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Add repository question behavior",
        analyzed_source_digest=None,
        source_preconditions=(),
        evidence=({"summary": "M0 fixture"},),
        uncertainties=(),
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )

    path = repo_root / f".codecortex/.cache/pending_proposals/{PROPOSAL_ID}.json"
    payload = path.read_bytes()
    decoded = json.loads(payload)
    assert proposal.base_graph_revision == 0
    assert decoded["proposal_id"] == PROPOSAL_ID
    assert decoded["status"] == "proposed"
    assert decoded["created_at"] == CREATED_AT
    assert payload.endswith(b"\n")
    assert app.cognitive_proposal(PROPOSAL_ID) == proposal
    assert app.pending_proposals is not None
    app.pending_proposals.delete(PROPOSAL_ID)
    app.pending_proposals.create(proposal)
    assert path.read_bytes() == payload


def test_revise_atomically_replaces_current_state_and_preserves_discussion_history(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Appending a second current file could mix old and new Analyzer results."""
    app.initialize_repository()
    original = app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Initial interpretation",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )

    revised = app.revise_cognitive_proposal(
        PROPOSAL_ID,
        operations=(add_node("behavior.explain-project"),),
        reason="User asked to narrow the behavior name",
        revised_at=REVISED_AT,
    )

    directory = repo_root / ".codecortex/.cache/pending_proposals"
    files = list(directory.iterdir())
    assert files == [directory / f"{PROPOSAL_ID}.json"]
    assert revised.status is ProposalStatus.PROPOSED
    assert revised.patch_digest != original.patch_digest
    assert revised.revision_log[0].previous_operations == original.operations
    assert app.cognitive_proposal(PROPOSAL_ID) == revised


def test_pending_proposal_can_be_deleted_after_consumption(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Missing cleanup would leave obsolete proposals available to later sessions."""
    app.initialize_repository()
    app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Temporary pending cognition",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )

    app.pending_proposals.delete(PROPOSAL_ID)

    assert not (
        repo_root / f".codecortex/.cache/pending_proposals/{PROPOSAL_ID}.json"
    ).exists()
    with pytest.raises(CodeCortexError) as exc:
        app.cognitive_proposal(PROPOSAL_ID)
    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_pending_lookup_rejects_wrong_id_namespace_without_path_access(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Using an unchecked ID as a filename could escape or confuse pending storage."""
    app.initialize_repository()

    with pytest.raises(CodeCortexError) as exc:
        app.cognitive_proposal("evt_01J00000000000000000000000")

    assert exc.value.code is ErrorCode.INVALID_ID
    assert not (repo_root / ".codecortex/.cache/pending_proposals").exists()


def test_create_rejects_an_explicit_blank_timestamp(
    app: ApplicationServices,
) -> None:
    """Application create must preserve explicit invalid input for domain validation."""
    app.initialize_repository()

    with pytest.raises(CodeCortexError) as exc:
        app.create_cognitive_proposal(
            operations=(add_node(),),
            affected_nodes=("behavior.answer-question",),
            reason="Create with invalid time",
            proposal_id=PROPOSAL_ID,
            created_at="",
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_revise_rejects_an_explicit_blank_timestamp(
    app: ApplicationServices,
) -> None:
    """Application revise must not replace a supplied blank time with its clock."""
    app.initialize_repository()
    app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Initial proposal",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )

    with pytest.raises(CodeCortexError) as exc:
        app.revise_cognitive_proposal(
            PROPOSAL_ID,
            operations=(add_node("behavior.revised"),),
            reason="Revision with invalid time",
            revised_at="",
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_persisted_revised_status_cannot_receive_approval(
    app: ApplicationServices,
) -> None:
    """Only canonical persisted PROPOSED state may enter approval verification."""
    app.initialize_repository()
    proposal = app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Initial proposal",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )
    revised_state = replace(proposal, status=ProposalStatus.REVISED)
    assert app.pending_proposals is not None
    app.pending_proposals.replace(revised_state)
    restored = app.cognitive_proposal(PROPOSAL_ID)
    approval = ApprovalRecord(
        proposal_id=restored.proposal_id,
        patch_digest=restored.patch_digest,
        approved_by="user",
        approved_at="2026-09-02T02:00:00Z",
        approval_summary="Approve current patch",
    )

    with pytest.raises(CodeCortexError) as exc:
        restored.verify_approval(approval)

    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.parametrize("operation", ["create", "load", "replace", "delete"])
def test_pending_final_path_rejects_symlink_escape_for_every_operation(
    app: ApplicationServices,
    repo_root: Path,
    operation: str,
) -> None:
    """Following a final symlink could read or mutate a proposal outside the repository."""
    app.initialize_repository()
    proposal = app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Symlink safety fixture",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )
    assert app.pending_proposals is not None
    pending_path = (
        repo_root / f".codecortex/.cache/pending_proposals/{PROPOSAL_ID}.json"
    )
    outside_path = repo_root.parent / "outside-proposal.json"
    outside_payload = pending_path.read_bytes()
    outside_path.write_bytes(outside_payload)
    pending_path.unlink()
    pending_path.symlink_to(outside_path)

    with pytest.raises(CodeCortexError) as exc:
        if operation == "create":
            app.pending_proposals.create(proposal)
        elif operation == "load":
            app.pending_proposals.load(PROPOSAL_ID)
        elif operation == "replace":
            app.pending_proposals.replace(proposal)
        else:
            app.pending_proposals.delete(PROPOSAL_ID)

    assert exc.value.code is ErrorCode.PATH_OUTSIDE_REPOSITORY
    assert outside_path.read_bytes() == outside_payload
    assert pending_path.is_symlink()


def test_pending_delete_normalizes_unlink_failure(
    app: ApplicationServices,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filesystem delete failure must remain a stable adapter-facing Core error."""
    app.initialize_repository()
    app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Delete failure fixture",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )
    assert app.pending_proposals is not None
    pending_path = (
        repo_root / f".codecortex/.cache/pending_proposals/{PROPOSAL_ID}.json"
    )
    real_unlink = Path.unlink

    def fail_target_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == pending_path:
            raise PermissionError("injected unlink failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target_unlink)

    with pytest.raises(CodeCortexError) as exc:
        app.pending_proposals.delete(PROPOSAL_ID)

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert pending_path.is_file()
