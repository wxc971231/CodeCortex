import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import PatchOperation, ProposalStatus
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
