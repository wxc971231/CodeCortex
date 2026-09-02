"""Cross-process coverage for the exclusive apply transaction lock."""

import multiprocessing
import subprocess
from pathlib import Path

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError
from codecortex.domain.proposals import ApprovalRecord, PatchOperation
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views


def _services(root: Path) -> ApplicationServices:
    repository = Repository(root)
    return ApplicationServices(repository, FormalStore(repository), RepositoryLock(root), pending_proposals=PendingProposalStore(repository), view_renderer=render_views)


def _apply_child(root: str, proposal_id: str, digest: str, queue: object) -> None:
    try:
        result = _services(Path(root)).apply_cognitive_proposal(proposal_id, ApprovalRecord(proposal_id, digest, "user", "2026-09-03T00:00:00Z", "Concurrent apply acceptance test"))
    except CodeCortexError as error:
        queue.put(error.code.value)  # type: ignore[union-attr]
    else:
        queue.put(f"APPLIED:{result.graph_revision}")  # type: ignore[union-attr]


def test_two_processes_apply_one_proposal_once(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    services = _services(root)
    services.initialize_repository()
    proposal = services.create_cognitive_proposal(
        operations=(PatchOperation("add_node", "behavior.concurrent-apply", {"id": "behavior.concurrent-apply", "kind": "behavior", "title": "Concurrent apply"}),),
        affected_nodes=("behavior.concurrent-apply",), reason="Exercise the process lock around apply.",
        proposal_id="prop_01J00000000000000000000002", created_at="2026-09-03T00:00:00Z",
    )
    queue = multiprocessing.Queue()
    children = [multiprocessing.Process(target=_apply_child, args=(str(root), proposal.proposal_id, proposal.patch_digest, queue)) for _ in range(2)]
    for child in children:
        child.start()
    outcomes = [queue.get(timeout=10) for _ in children]
    for child in children:
        child.join(timeout=10)
        assert child.exitcode == 0
    assert outcomes.count("APPLIED:1") == 1
    assert services.repository_overview().graph_revision == 1
    assert services.validate_graph().valid is True
