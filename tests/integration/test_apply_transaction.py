"""Integration tests for applying approved proposals as one formal transaction."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import ApprovalRecord, PatchOperation, Proposal
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views

PROPOSAL_ID = "prop_01J00000000000000000000000"
SECOND_PROPOSAL_ID = "prop_01J00000000000000000000001"
CREATED_AT = "2026-09-02T01:23:45Z"
APPROVED_AT = "2026-09-02T02:00:00Z"


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


def add_node(
    node_id: str = "behavior.answer-question",
    *,
    kind: str = "behavior",
    title: str = "Answer a question",
) -> PatchOperation:
    return PatchOperation("add_node", node_id, {"id": node_id, "kind": kind, "title": title})


def approval_for(proposal: Proposal) -> ApprovalRecord:
    return ApprovalRecord(
        proposal_id=proposal.proposal_id,
        patch_digest=proposal.patch_digest,
        approved_by="user",
        approved_at=APPROVED_AT,
        approval_summary="Approve current patch",
    )


@pytest.fixture
def approved_proposal(app: ApplicationServices) -> Proposal:
    app.initialize_repository()
    return app.create_cognitive_proposal(
        operations=(add_node(),),
        affected_nodes=("behavior.answer-question",),
        reason="Add repository question behavior",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(("d:" if path.is_dir() else "f:").encode())
        digest.update(relative.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_apply_writes_event_graph_views_and_manifest_as_one_revision(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """A partial apply would leave reviewed cognition half written across files."""
    result = app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )

    assert result.graph_revision == 1
    assert result.applied_proposal_id == PROPOSAL_ID
    event = json.loads(
        (repo_root / f".codecortex/history/events/{result.event_id}.json").read_text()
    )
    graph = json.loads((repo_root / ".codecortex/graph.json").read_text())
    assert event["event_id"] == result.event_id
    assert event["event_type"] == "cognitive_proposal_applied"
    assert event["proposal_snapshot"]["proposal_id"] == approved_proposal.proposal_id
    assert graph["graph_revision"] == 1
    assert graph["nodes"][0]["approval"]["approval_event_id"] == result.event_id
    assert graph["nodes"][0]["node_revision"] == 1
    manifest = json.loads((repo_root / ".codecortex/manifest.json").read_text())
    assert manifest["graph_revision"] == 1
    assert manifest["cognition_initialized"] is False
    tree = (repo_root / ".codecortex/views/TREE.md").read_text(encoding="utf-8")
    assert "behavior.answer-question" in tree
    assert (repo_root / ".codecortex/views/behaviors/answer-question.md").is_file()
    assert app.validate_graph().valid is True


def test_apply_keeps_revision_zero_source_baseline_byte_identical(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """M0 apply may advance the graph but must not claim real source cognition."""
    baseline_path = repo_root / ".codecortex/source_baseline.json"
    before = baseline_path.read_bytes()

    app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )

    assert baseline_path.read_bytes() == before
    baseline = json.loads(before)
    assert baseline["repository_source_digest"] is None
    assert baseline["files"] == []
    manifest = json.loads((repo_root / ".codecortex/manifest.json").read_text())
    assert manifest["cognition_initialized"] is False
    assert manifest["cognition_baseline"] is None
    entity_refs = json.loads((repo_root / ".codecortex/entity_refs.json").read_text())
    assert entity_refs["graph_revision"] == 1
    assert entity_refs["entities"] == []


def test_apply_consumes_pending_proposal_and_records_complete_snapshot(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """The immutable event is the audit copy; the pending file must not linger."""
    result = app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )

    pending_path = (
        repo_root / f".codecortex/.cache/pending_proposals/{PROPOSAL_ID}.json"
    )
    assert not pending_path.exists()
    with pytest.raises(CodeCortexError) as exc:
        app.cognitive_proposal(PROPOSAL_ID)
    assert exc.value.code is ErrorCode.PROPOSAL_STALE

    event = json.loads(
        (repo_root / f".codecortex/history/events/{result.event_id}.json").read_text()
    )
    assert event["proposal_id"] == PROPOSAL_ID
    assert event["patch_digest"] == approved_proposal.patch_digest
    assert event["base_graph_revision"] == 0
    assert event["graph_revision"] == 1
    assert event["reason"] == "Add repository question behavior"
    assert event["affected_nodes"] == ["behavior.answer-question"]
    assert event["approval"] == {
        "proposal_id": PROPOSAL_ID,
        "patch_digest": approved_proposal.patch_digest,
        "approved_by": "user",
        "approved_at": APPROVED_AT,
        "approval_summary": "Approve current patch",
    }
    snapshot = event["proposal_snapshot"]
    assert snapshot["status"] == "applied"
    assert snapshot["operations"] == [
        operation.to_canonical_value()
        for operation in approved_proposal.operations
    ]
    assert snapshot["patch_digest"] == approved_proposal.patch_digest
    assert snapshot["created_at"] == CREATED_AT


def test_apply_with_mismatched_approval_changes_nothing(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """Applying an unapproved patch would bypass the explicit approval boundary."""
    before = digest_tree(repo_root / ".codecortex")
    mismatched = ApprovalRecord(
        proposal_id=approved_proposal.proposal_id,
        patch_digest="sha256:" + "0" * 64,
        approved_by="user",
        approved_at=APPROVED_AT,
        approval_summary="Approve a different patch",
    )

    with pytest.raises(CodeCortexError) as exc:
        app.apply_cognitive_proposal(approved_proposal.proposal_id, mismatched)

    assert exc.value.code is ErrorCode.APPROVAL_MISMATCH
    assert digest_tree(repo_root / ".codecortex") == before
    assert not (repo_root / ".codecortex/.cache/transactions").exists()


def test_apply_with_stale_base_revision_is_rejected_without_changes(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """A proposal approved against an older graph must never apply onto a new one."""
    stale = app.create_cognitive_proposal(
        operations=(add_node("capability.render-markdown", kind="capability",
                             title="Render Markdown"),),
        affected_nodes=("capability.render-markdown",),
        reason="Add rendering capability",
        proposal_id=SECOND_PROPOSAL_ID,
        created_at=CREATED_AT,
    )
    app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )
    before = digest_tree(repo_root / ".codecortex")

    with pytest.raises(CodeCortexError) as exc:
        app.apply_cognitive_proposal(stale.proposal_id, approval_for(stale))

    assert exc.value.code is ErrorCode.GRAPH_REVISION_CONFLICT
    assert digest_tree(repo_root / ".codecortex") == before
    assert app.repository_overview().graph_revision == 1


def test_apply_rejects_unknown_or_consumed_proposal(
    app: ApplicationServices, approved_proposal: Proposal
) -> None:
    """A consumed pending proposal cannot be applied a second time."""
    with pytest.raises(CodeCortexError) as exc:
        app.apply_cognitive_proposal(
            "prop_01J0000000000000000000000Z", approval_for(approved_proposal)
        )
    assert exc.value.code is ErrorCode.PROPOSAL_STALE

    app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )

    with pytest.raises(CodeCortexError) as exc:
        app.apply_cognitive_proposal(
            approved_proposal.proposal_id, approval_for(approved_proposal)
        )
    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_views_are_a_deterministic_projection_of_the_committed_graph(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Views must regenerate to identical bytes from the same canonical graph."""
    app.initialize_repository()
    proposal = app.create_cognitive_proposal(
        operations=(
            add_node(),
            add_node(
                "capability.render-markdown",
                kind="capability",
                title="Render Markdown",
            ),
        ),
        affected_nodes=("behavior.answer-question", "capability.render-markdown"),
        reason="Add behavior and capability",
        proposal_id=PROPOSAL_ID,
        created_at=CREATED_AT,
    )
    app.apply_cognitive_proposal(proposal.proposal_id, approval_for(proposal))

    assert isinstance(app.formal_store, FormalStore)
    state = app.formal_store.load()
    rendered = render_views(state.graph)
    assert set(rendered) == {
        "views/TREE.md",
        "views/behaviors/answer-question.md",
        "views/capabilities/render-markdown.md",
    }
    for relative, payload in rendered.items():
        assert (repo_root / ".codecortex" / relative).read_bytes() == payload
    assert rendered == render_views(state.graph)


def test_history_events_are_immutable_across_later_applies(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """Rewriting a committed event would destroy the formal audit trail."""
    first = app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )
    first_path = repo_root / f".codecortex/history/events/{first.event_id}.json"
    first_bytes = first_path.read_bytes()

    follow_up = app.create_cognitive_proposal(
        operations=(add_node("capability.render-markdown", kind="capability",
                             title="Render Markdown"),),
        affected_nodes=("capability.render-markdown",),
        reason="Add rendering capability",
        proposal_id=SECOND_PROPOSAL_ID,
        created_at=CREATED_AT,
    )
    second = app.apply_cognitive_proposal(
        follow_up.proposal_id, approval_for(follow_up)
    )

    assert second.graph_revision == 2
    assert second.event_id != first.event_id
    assert first_path.read_bytes() == first_bytes
    events = sorted((repo_root / ".codecortex/history/events").iterdir())
    assert [path.name for path in events] == [
        f"{first.event_id}.json",
        f"{second.event_id}.json",
    ]
    assert app.validate_graph().valid is True


def test_commit_refuses_to_overwrite_an_existing_history_event(
    app: ApplicationServices, repo_root: Path, approved_proposal: Proposal
) -> None:
    """An apply reusing an event ID must fail before touching any formal file."""
    result = app.apply_cognitive_proposal(
        approved_proposal.proposal_id, approval_for(approved_proposal)
    )
    before = digest_tree(repo_root / ".codecortex")
    assert isinstance(app.formal_store, FormalStore)
    state = app.formal_store.load()
    duplicate_event = {
        "schema_version": 1,
        "event_id": result.event_id,
        "event_type": "cognitive_proposal_applied",
    }

    with pytest.raises(CodeCortexError) as exc:
        app.formal_store.commit(
            state, event=duplicate_event, views=render_views(state.graph)
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert digest_tree(repo_root / ".codecortex") == before
