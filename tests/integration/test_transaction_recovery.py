"""Fault-injection tests for recoverable multi-file formal transactions."""

import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.ports import RecoveryResult
from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import ApprovalRecord, PatchOperation
from codecortex.infrastructure.formal import EMPTY_TREE_VIEW, FormalStore
from codecortex.infrastructure.jsonio import canonical_json_bytes
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views

PROPOSAL_ID = "prop_01J00000000000000000000000"
CREATED_AT = "2026-09-02T01:23:45Z"
APPROVED_AT = "2026-09-02T02:00:00Z"

ROLLBACK_STAGES = (
    "staged",
    "journal",
    "event",
    "graph",
    "entity_refs",
    "source_baseline",
    "views",
)


class SimulatedCrash(Exception):
    """Raised by the crash hook to abandon the process mid-transaction."""


class TransactionFixture:
    """Drive one crashing apply and the subsequent startup recovery."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.repository = Repository(repo_root)
        app = self._app(FormalStore(self.repository))
        app.initialize_repository()
        self.proposal = app.create_cognitive_proposal(
            operations=(
                PatchOperation(
                    "add_node",
                    "behavior.answer-question",
                    {
                        "id": "behavior.answer-question",
                        "kind": "behavior",
                        "title": "Answer a question",
                    },
                ),
            ),
            affected_nodes=("behavior.answer-question",),
            reason="Add repository question behavior",
            proposal_id=PROPOSAL_ID,
            created_at=CREATED_AT,
        )
        self.approval = ApprovalRecord(
            proposal_id=self.proposal.proposal_id,
            patch_digest=self.proposal.patch_digest,
            approved_by="user",
            approved_at=APPROVED_AT,
            approval_summary="Approve current patch",
        )

    def _app(self, formal_store: FormalStore) -> ApplicationServices:
        return ApplicationServices(
            repository=self.repository,
            formal_store=formal_store,
            repository_lock=RepositoryLock(self.repo_root),
            pending_proposals=PendingProposalStore(self.repository),
            view_renderer=render_views,
        )

    def crash_after(self, stage: str) -> None:
        """Run apply with a store that crashes right after the named stage."""

        def hook(stage_name: str) -> None:
            if stage_name == stage:
                raise SimulatedCrash(stage)

        app = self._app(FormalStore(self.repository, crash_hook=hook))
        with pytest.raises(SimulatedCrash):
            app.apply_cognitive_proposal(self.proposal.proposal_id, self.approval)

    def restart_and_recover(self) -> RecoveryResult:
        """Simulate a process restart: a new store resolves what is on disk."""
        return FormalStore(self.repository).recover()

    def healthy_app(self) -> ApplicationServices:
        return self._app(FormalStore(self.repository))

    @property
    def codecortex(self) -> Path:
        return self.repo_root / ".codecortex"

    @property
    def pending_path(self) -> Path:
        return self.codecortex / f".cache/pending_proposals/{PROPOSAL_ID}.json"


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def transaction_fixture(repo_root: Path) -> TransactionFixture:
    return TransactionFixture(repo_root)


@pytest.mark.parametrize(
    "fail_after", ["event", "graph", "entity_refs", "source_baseline", "views"]
)
def test_recovery_exposes_only_old_or_new_revision(
    transaction_fixture: TransactionFixture, fail_after: str
) -> None:
    transaction_fixture.crash_after(fail_after)
    recovered = transaction_fixture.restart_and_recover()
    assert recovered.visible_revision in {0, 1}
    assert recovered.is_internally_consistent


@pytest.mark.parametrize("fail_after", ROLLBACK_STAGES)
def test_crash_before_commit_marker_restores_complete_old_revision(
    transaction_fixture: TransactionFixture, fail_after: str
) -> None:
    """No mixture of old and new formal files may survive an interrupted apply."""
    transaction_fixture.crash_after(fail_after)

    recovered = transaction_fixture.restart_and_recover()

    assert recovered.visible_revision == 0
    assert recovered.is_internally_consistent
    assert recovered.restored_transactions or recovered.completed_transactions
    codecortex = transaction_fixture.codecortex
    assert list((codecortex / "history/events").iterdir()) == []
    assert (codecortex / "views/TREE.md").read_bytes() == EMPTY_TREE_VIEW
    assert not any((codecortex / "views/behaviors").iterdir())
    assert json.loads((codecortex / "graph.json").read_text())["nodes"] == []
    assert json.loads((codecortex / "manifest.json").read_text())["graph_revision"] == 0
    transactions_root = codecortex / ".cache/transactions"
    assert not transactions_root.exists() or not any(transactions_root.iterdir())
    # The proposal was never consumed, so the user can approve and retry.
    assert transaction_fixture.pending_path.is_file()
    app = transaction_fixture.healthy_app()
    assert app.validate_graph().valid is True
    assert app.repository_overview().graph_revision == 0


def test_crash_after_commit_marker_completes_the_new_revision(
    transaction_fixture: TransactionFixture,
) -> None:
    """Once the manifest moved, recovery must finish cleanup, never roll back."""
    transaction_fixture.crash_after("manifest")

    recovered = transaction_fixture.restart_and_recover()

    assert recovered.visible_revision == 1
    assert recovered.is_internally_consistent
    assert recovered.completed_transactions
    codecortex = transaction_fixture.codecortex
    events = list((codecortex / "history/events").iterdir())
    assert len(events) == 1
    graph = json.loads((codecortex / "graph.json").read_text())
    assert graph["nodes"][0]["id"] == "behavior.answer-question"
    transactions_root = codecortex / ".cache/transactions"
    assert not transactions_root.exists() or not any(transactions_root.iterdir())
    # The committed revision can never be applied to again by the same proposal.
    app = transaction_fixture.healthy_app()
    with pytest.raises(CodeCortexError) as exc:
        app.apply_cognitive_proposal(
            transaction_fixture.proposal.proposal_id, transaction_fixture.approval
        )
    assert exc.value.code is ErrorCode.GRAPH_REVISION_CONFLICT


def test_recovery_without_interrupted_transactions_is_a_noop(
    transaction_fixture: TransactionFixture,
) -> None:
    """Startup recovery on a clean repository must not touch formal files."""
    recovered = transaction_fixture.restart_and_recover()

    assert recovered.visible_revision == 0
    assert recovered.is_internally_consistent
    assert recovered.restored_transactions == ()
    assert recovered.completed_transactions == ()


def test_main_recovery_entry_repairs_state_before_later_reads(
    transaction_fixture: TransactionFixture,
) -> None:
    """Main's startup entry, not a hidden apply retry, owns recovery."""
    transaction_fixture.crash_after("graph")
    app = transaction_fixture.healthy_app()

    with pytest.raises(CodeCortexError) as exc:
        app.repository_overview()
    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT

    recovered = app.recover_formal_state()

    assert recovered.visible_revision == 0
    assert app.repository_overview().graph_revision == 0
    assert app.validate_graph().valid is True


def test_recovery_is_idempotent_after_a_rollback(
    transaction_fixture: TransactionFixture,
) -> None:
    """A crash during recovery itself must leave recovery repeatable."""
    transaction_fixture.crash_after("graph")

    first = transaction_fixture.restart_and_recover()
    second = transaction_fixture.restart_and_recover()

    assert first.visible_revision == 0
    assert second.visible_revision == 0
    assert second.is_internally_consistent
    assert second.restored_transactions == ()
    assert transaction_fixture.healthy_app().validate_graph().valid is True


def test_recovery_rejects_an_unprovable_revision_mixture(
    transaction_fixture: TransactionFixture,
) -> None:
    """A manifest matching neither journal revision must never be guessed at."""
    transaction_fixture.crash_after("views")
    codecortex = transaction_fixture.codecortex
    manifest = json.loads((codecortex / "manifest.json").read_text())
    manifest["graph_revision"] = 7
    (codecortex / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(CodeCortexError) as exc:
        transaction_fixture.restart_and_recover()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
    # The unknown mixture is preserved as evidence, not silently repaired.
    assert json.loads((codecortex / "manifest.json").read_text())["graph_revision"] == 7
    assert any((codecortex / ".cache/transactions").iterdir())


def test_recovery_rejects_an_unreadable_journal(
    transaction_fixture: TransactionFixture,
) -> None:
    """A journal that cannot be parsed proves nothing about the mixture."""
    transaction_fixture.crash_after("graph")
    codecortex = transaction_fixture.codecortex
    journals = list((codecortex / ".cache/transactions").glob("*/journal.json"))
    assert len(journals) == 1
    journals[0].write_bytes(b"{not-json\n")

    with pytest.raises(CodeCortexError) as exc:
        transaction_fixture.restart_and_recover()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
