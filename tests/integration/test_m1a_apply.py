"""M1a full-graph apply with source baselines over real temporary repositories."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.proposals import (
    ManagedSourceSnapshot,
    ProposalService,
)
from codecortex.application.services import ApplicationServices
from codecortex.domain.analysis import AnalysisReport, validate_analysis_report
from codecortex.domain.cognition import FormalState, Manifest, SourceBaseline
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.facts import DigestProfile, SourceConfig
from codecortex.domain.proposals import ApprovalRecord, PatchOperation, Proposal
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.persistence.graph_replica import (
    EntityRefRecord,
    GraphReplica,
    HistoryEventRecord,
)
from codecortex.infrastructure.python.digest import (
    digest_source_file,
    repository_digest,
)
from codecortex.infrastructure.python.discovery import discover_python_source_set
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_legacy_views, render_views
from tests.conftest import analysis_report_bytes, analysis_report_dict, analysis_ulid

APPROVED_AT = "2026-09-03T02:00:00Z"
EDGE_CONTAINS = analysis_ulid("edge", 1)
EDGE_USES = analysis_ulid("edge", 2)
MAP_ID = analysis_ulid("map", 1)
EVID_NODE = analysis_ulid("evid", 1)
EVID_EDGE = analysis_ulid("evid", 2)
EVID_CONTAINS = analysis_ulid("evid", 3)


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def probe_sources(repository: Repository) -> ManagedSourceSnapshot:
    """Recompute the managed-source digests straight from disk."""
    discovered = discover_python_source_set(repository, SourceConfig())
    files = [digest_source_file(source) for source in discovered.sources]
    return ManagedSourceSnapshot(
        repository_source_digest=repository_digest(files, DigestProfile()),
        file_digests={
            item.source.relative_path: item.content_digest for item in files
        },
    )


class M1aHarness:
    """Wire the M1a proposal service against one temporary repository."""

    def __init__(
        self,
        repo_root: Path,
        *,
        crash_hook=None,
        replica=None,
    ) -> None:
        self.repo_root = repo_root
        self.repository = Repository(repo_root)
        self.formal_store = FormalStore(self.repository, crash_hook=crash_hook)
        self.lock = RepositoryLock(repo_root)
        self.pending = PendingProposalStore(self.repository)
        self.fact_sync = FactSyncService(
            self.repository,
            repository_lock=self.lock,
            formal_store=self.formal_store,
        )
        self.facts = self.fact_sync.database
        self.replica = (
            replica
            if replica is not None
            else GraphReplica.create_new(
                repo_root / ".codecortex/.cache/graph_replica.sqlite3",
                entity_refs=self._entity_ref_records,
                history_events=self._history_event_records,
            )
        )
        self.service = ProposalService(
            formal_store=self.formal_store,
            repository_lock=self.lock,
            pending_proposals=self.pending,
            view_renderer=render_views,
            fact_sync=self.fact_sync,
            source_probe=lambda: probe_sources(self.repository),
            facts=self.facts,
            replica=self.replica,
        )

    def initialize_formal(self) -> None:
        self.formal_store.initialize(
            FormalState.empty(
                manifest=Manifest(
                    schema_version=1,
                    graph_revision=0,
                    cognition_initialized=False,
                    cognition_baseline=None,
                ),
                source_baseline=SourceBaseline.empty(),
            )
        )

    def m0_services(self, *, legacy_renderer: bool = False) -> ApplicationServices:
        return ApplicationServices(
            repository=self.repository,
            formal_store=self.formal_store,
            repository_lock=self.lock,
            pending_proposals=self.pending,
            view_renderer=render_legacy_views if legacy_renderer else render_views,
        )

    def _entity_ref_records(self) -> tuple[EntityRefRecord, ...]:
        state = self.formal_store.load()
        return tuple(
            EntityRefRecord(
                uid=str(entry["uid"]),
                last_known_address=str(entry["last_known_address"]),
                kind=str(entry["kind"]),
                relative_path=str(entry["relative_path"]),
                signature=(
                    None if entry.get("signature") is None else str(entry["signature"])
                ),
                fingerprint=str(entry["fingerprint"]),
                resolution_status=str(entry["resolution_status"]),
            )
            for entry in state.entity_refs.entities
        )

    def _history_event_records(self) -> tuple[HistoryEventRecord, ...]:
        state = self.formal_store.load()
        records = []
        for ref in state.history_events:
            event = self.formal_store.read_history_event(ref.event_id)
            records.append(
                HistoryEventRecord(
                    event_id=ref.event_id,
                    event_type=ref.event_type,
                    resulting_graph_revision=int(event["graph_revision"]),
                    created_at=str(event["applied_at"]),
                    relative_path=f"history/events/{ref.event_id}.json",
                )
            )
        return tuple(records)


def approval_for(proposal: Proposal) -> ApprovalRecord:
    return ApprovalRecord(
        proposal_id=proposal.proposal_id,
        patch_digest=proposal.patch_digest,
        approved_by="user",
        approved_at=APPROVED_AT,
        approval_summary="Approve current patch",
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _write(root, "pkg/a.py", "def answer_question(text):\n    return text.upper()\n")
    _write(root, "pkg/b.py", "def helper():\n    return 1\n")
    return root


@pytest.fixture
def harness(repo_root: Path) -> M1aHarness:
    harness = M1aHarness(repo_root)
    harness.initialize_formal()
    return harness


def _answer_uid(harness: M1aHarness) -> str:
    harness.fact_sync.sync()
    entities = harness.facts.entities_at_path("pkg/a.py", None, 10).items
    return next(entity.uid for entity in entities if entity.name == "answer_question")


def _full_report(harness: M1aHarness) -> AnalysisReport:
    sync = harness.fact_sync.sync()
    revision = harness.formal_store.load().graph.graph_revision
    uid = _answer_uid(harness)
    report = analysis_report_dict(
        base_graph_revision=revision,
        analyzed_source_digest=sync.repository_source_digest,
        candidate_nodes=[
            {"id": "responsibility.answering", "kind": "responsibility",
             "title": "Answering"},
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Answer questions",
                "evidence": [
                    {
                        "id": EVID_NODE,
                        "kind": "code_entity",
                        "entity_uid": uid,
                        "relative_path": "pkg/a.py",
                        "start_line": 1,
                        "end_line": 2,
                        "observation": "实现入口",
                    }
                ],
            },
            {"id": "capability.text-transform", "kind": "capability",
             "title": "Transform text"},
        ],
        candidate_edges=[
            {
                "id": EDGE_CONTAINS,
                "type": "contains",
                "source_id": "responsibility.answering",
                "target_id": "behavior.answer-question",
                "epistemic_status": "inferred",
                "evidence": [
                    {
                        "id": EVID_CONTAINS,
                        "kind": "code_entity",
                        "entity_uid": uid,
                        "relative_path": "pkg/a.py",
                        "start_line": 1,
                        "end_line": 1,
                    }
                ],
            },
            {
                "id": EDGE_USES,
                "type": "uses",
                "source_id": "behavior.answer-question",
                "target_id": "capability.text-transform",
                "epistemic_status": "inferred",
                "evidence": [
                    {
                        "id": EVID_EDGE,
                        "kind": "code_entity",
                        "entity_uid": uid,
                        "relative_path": "pkg/a.py",
                        "start_line": 2,
                        "end_line": 2,
                    }
                ],
            },
        ],
        candidate_flows=[
            {
                "behavior_id": "behavior.answer-question",
                "materialization_status": "materialized",
                "steps": [
                    {
                        "id": "behavior.answer-question#step.transform",
                        "order": 1,
                        "title": "Transform",
                        "uses_capabilities": ["capability.text-transform"],
                    }
                ],
            }
        ],
        candidate_mappings=[
            {
                "id": MAP_ID,
                "subject_kind": "node",
                "subject_id": "behavior.answer-question",
                "entity_uid": uid,
                "role": "primary",
                "resolution_status": "resolved",
                "evidence_note": "实现入口",
            }
        ],
        uncertainties=["仅覆盖 pkg 分区"],
    )
    return validate_analysis_report(
        analysis_report_bytes(report), revision, sync.repository_source_digest
    )


def _formal_fingerprint(root: Path) -> dict[str, str]:
    base = root / ".codecortex"
    return {
        name: (base / name).read_text(encoding="utf-8")
        for name in (
            "manifest.json",
            "graph.json",
            "entity_refs.json",
            "source_baseline.json",
        )
    }


def test_approved_initialization_advances_all_formal_state(
    harness: M1aHarness, repo_root: Path
) -> None:
    report = _full_report(harness)
    proposal = harness.service.create_proposal_from_analysis(
        report, "initialize understanding"
    )
    assert proposal.base_graph_revision == report.base_graph_revision
    assert proposal.analyzed_source_digest == report.analyzed_source_digest
    precondition_paths = [entry["relative_path"] for entry in proposal.source_preconditions]
    assert precondition_paths == ["pkg/a.py", "pkg/b.py"]

    result = harness.service.apply_cognitive_proposal(
        proposal.proposal_id, approval_for(proposal)
    )

    assert result.graph_revision == report.base_graph_revision + 1
    assert result.applied_proposal_id == proposal.proposal_id
    assert result.cache_warnings == ()
    state = harness.formal_store.load()
    assert state.manifest.cognition_initialized is True
    assert state.manifest.cognition_baseline == report.analyzed_source_digest
    assert (
        state.source_baseline.repository_source_digest
        == report.analyzed_source_digest
    )
    assert [
        entry["relative_path"] for entry in state.source_baseline.files
    ] == ["pkg/a.py", "pkg/b.py"]
    uid = _answer_uid(harness)
    assert {entry["uid"] for entry in state.entity_refs.entities} == {uid}
    ref = state.entity_refs.entities[0]
    assert ref["relative_path"] == "pkg/a.py"
    assert ref["fingerprint"].startswith("sha256:")
    nodes = {node["id"]: node for node in state.graph.nodes}
    assert nodes["behavior.answer-question"]["approval"] == {
        "approval_event_id": result.event_id
    }
    assert state.graph.logical_flows[0]["behavior_id"] == "behavior.answer-question"
    assert state.graph.implementation_mappings[0]["id"] == MAP_ID

    event = harness.formal_store.read_history_event(result.event_id)
    assert event["proposal_snapshot"]["proposal_id"] == proposal.proposal_id
    assert event["change_set_summary"]["after_source_digest"] == (
        report.analyzed_source_digest
    )
    with pytest.raises(CodeCortexError):
        harness.pending.load(proposal.proposal_id)

    assert harness.replica.metadata().graph_revision == result.graph_revision
    metadata = harness.facts.cache_metadata()
    assert metadata.baseline_entity_snapshot_completeness == "complete"
    assert metadata.graph_revision == result.graph_revision
    with harness.facts.open_read() as connection:
        rows = connection.execute(
            "SELECT uid, baseline_source_digest FROM baseline_entity_snapshots"
        ).fetchall()
    assert rows
    assert {row["baseline_source_digest"] for row in rows} == {
        report.analyzed_source_digest
    }
    assert harness.replica.search(
        "answer", ("behavior",), 10, result.graph_revision
    )
    view_manifest = json.loads(
        (repo_root / ".codecortex/view_manifest.json").read_text(encoding="utf-8")
    )
    assert view_manifest["graph_revision"] == result.graph_revision
    assert [entry["relative_path"] for entry in view_manifest["files"]] == sorted(
        entry["relative_path"] for entry in view_manifest["files"]
    )


def test_analysis_backed_reinitialize_updates_existing_graph_objects(
    harness: M1aHarness,
) -> None:
    initial = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(initial.proposal_id, approval_for(initial))
    sync = harness.fact_sync.sync()
    uid = _answer_uid(harness)
    report_payload = analysis_report_dict(
        base_graph_revision=1,
        analyzed_source_digest=sync.repository_source_digest,
        candidate_nodes=[
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Answer repository questions",
            }
        ],
        candidate_edges=[
            {
                "id": EDGE_USES,
                "type": "uses",
                "source_id": "behavior.answer-question",
                "target_id": "capability.text-transform",
                "evidence": [
                    {
                        "id": analysis_ulid("evid", 20),
                        "kind": "code_entity",
                        "entity_uid": uid,
                        "relative_path": "pkg/a.py",
                        "start_line": 1,
                        "end_line": 2,
                    }
                ],
            }
        ],
        candidate_flows=[
            {
                "behavior_id": "behavior.answer-question",
                "materialization_status": "unmaterialized",
            }
        ],
        candidate_mappings=[
            {
                "id": MAP_ID,
                "subject_kind": "node",
                "subject_id": "behavior.answer-question",
                "entity_uid": uid,
                "role": "supporting",
                "resolution_status": "resolved",
            }
        ],
        change_operations=[
            {
                "kind": "update_node",
                "target_id": "behavior.answer-question",
                "before_revision": 1,
                "change_kind": "update",
                "change_group": "refresh-behavior",
            },
            {
                "kind": "update_edge",
                "target_id": EDGE_USES,
                "before_revision": 1,
                "change_kind": "move",
                "change_group": "move-implementation",
            },
            {
                "kind": "set_logical_flow",
                "target_id": "behavior.answer-question",
                "before_revision": 1,
                "change_kind": "update",
                "change_group": "refresh-behavior",
            },
            {
                "kind": "update_mapping",
                "target_id": MAP_ID,
                "before_revision": 1,
                "change_kind": "move",
                "change_group": "move-implementation",
            },
        ],
    )
    report = validate_analysis_report(
        analysis_report_bytes(report_payload), 1, sync.repository_source_digest
    )

    proposal = harness.service.create_proposal_from_analysis(
        report, "reinitialize understanding"
    )
    assert [operation.kind.value for operation in proposal.operations] == [
        "update_node",
        "update_edge",
        "set_logical_flow",
        "update_mapping",
    ]
    assert [operation.expected_revision for operation in proposal.operations] == [
        1,
        1,
        1,
        1,
    ]
    assert harness.pending.load(proposal.proposal_id).operations == proposal.operations
    result = harness.service.apply_cognitive_proposal(
        proposal.proposal_id, approval_for(proposal)
    )

    assert result.graph_revision == 2
    graph = harness.formal_store.load().graph
    behavior = next(
        node for node in graph.nodes if node["id"] == "behavior.answer-question"
    )
    assert behavior["title"] == "Answer repository questions"
    assert graph.logical_flows[0]["materialization_status"] == "unmaterialized"
    assert graph.implementation_mappings[0]["role"] == "supporting"


def test_invalid_analysis_change_fails_before_pending_proposal_write(
    harness: M1aHarness,
) -> None:
    initial = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(initial.proposal_id, approval_for(initial))
    sync = harness.fact_sync.sync()
    payload = analysis_report_dict(
        base_graph_revision=1,
        analyzed_source_digest=sync.repository_source_digest,
        change_operations=[
            {
                "kind": "remove_node",
                "target_id": "behavior.answer-question",
                "before_revision": 1,
                "change_kind": "remove",
                "change_group": "remove-behavior",
            }
        ],
    )
    report = validate_analysis_report(
        analysis_report_bytes(payload), 1, sync.repository_source_digest
    )
    pending = harness.repo_root / ".codecortex/.cache/pending_proposals"
    before = set(pending.iterdir()) if pending.exists() else set()

    with pytest.raises(CodeCortexError) as excinfo:
        harness.service.create_proposal_from_analysis(
            report, "invalid implicit cascade"
        )

    assert excinfo.value.code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert (set(pending.iterdir()) if pending.exists() else set()) == before


def test_stale_analysis_target_precondition_fails_before_pending_write(
    harness: M1aHarness,
) -> None:
    initial = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(initial.proposal_id, approval_for(initial))
    sync = harness.fact_sync.sync()
    payload = analysis_report_dict(
        base_graph_revision=1,
        analyzed_source_digest=sync.repository_source_digest,
        candidate_nodes=[
            {
                "id": "behavior.answer-question",
                "kind": "behavior",
                "title": "Stale update",
            }
        ],
        change_operations=[
            {
                "kind": "update_node",
                "target_id": "behavior.answer-question",
                "before_revision": 99,
                "change_kind": "update",
                "change_group": "stale-update",
            }
        ],
    )
    report = validate_analysis_report(
        analysis_report_bytes(payload), 1, sync.repository_source_digest
    )
    pending = harness.repo_root / ".codecortex/.cache/pending_proposals"
    before = set(pending.iterdir()) if pending.exists() else set()

    with pytest.raises(CodeCortexError) as excinfo:
        harness.service.create_proposal_from_analysis(report, "stale target")

    assert excinfo.value.code is ErrorCode.ANALYSIS_REPORT_INVALID
    assert (set(pending.iterdir()) if pending.exists() else set()) == before


def test_m1a_view_manifest_rejects_hand_edited_managed_view(
    harness: M1aHarness, repo_root: Path
) -> None:
    proposal = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(proposal.proposal_id, approval_for(proposal))

    tree = repo_root / ".codecortex/views/TREE.md"
    tree.write_text("# hand edit\n", encoding="utf-8")

    with pytest.raises(CodeCortexError) as excinfo:
        harness.formal_store.load()
    assert excinfo.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_later_manual_apply_advances_existing_view_manifest(
    harness: M1aHarness, repo_root: Path
) -> None:
    proposal = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(proposal.proposal_id, approval_for(proposal))
    m0 = harness.m0_services()
    manual = m0.create_cognitive_proposal(
        operations=(
            PatchOperation(
                "add_node",
                "capability.manual-followup",
                {
                    "id": "capability.manual-followup",
                    "kind": "capability",
                    "title": "Manual follow-up",
                },
            ),
        ),
        affected_nodes=("capability.manual-followup",),
        reason="manual follow-up",
    )
    result = m0.apply_cognitive_proposal(manual.proposal_id, approval_for(manual))

    state = harness.formal_store.load()
    manifest = json.loads(
        (repo_root / ".codecortex/view_manifest.json").read_text(encoding="utf-8")
    )
    assert state.graph.graph_revision == result.graph_revision == 2
    assert manifest["graph_revision"] == 2


def test_m1a_rejects_pre_migration_views_not_rendered_by_m0(
    harness: M1aHarness,
) -> None:
    current_renderer_m0 = harness.m0_services()
    legacy = current_renderer_m0.create_cognitive_proposal(
        operations=(
            PatchOperation(
                "add_node",
                "capability.manual-note",
                {
                    "id": "capability.manual-note",
                    "kind": "capability",
                    "title": "Hand-reviewed note",
                },
            ),
        ),
        affected_nodes=("capability.manual-note",),
        reason="manual note rendered by the current renderer",
    )
    current_renderer_m0.apply_cognitive_proposal(
        legacy.proposal_id, approval_for(legacy)
    )
    proposal = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )

    with pytest.raises(CodeCortexError) as excinfo:
        harness.service.apply_cognitive_proposal(
            proposal.proposal_id, approval_for(proposal)
        )
    assert excinfo.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_view_manifest_rejects_unsorted_paths(harness: M1aHarness, repo_root: Path) -> None:
    proposal = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(proposal.proposal_id, approval_for(proposal))
    path = repo_root / ".codecortex/view_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = list(reversed(manifest["files"]))
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CodeCortexError) as excinfo:
        harness.formal_store.load()
    assert excinfo.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_any_source_change_makes_the_proposal_stale(
    harness: M1aHarness, repo_root: Path
) -> None:
    report = _full_report(harness)
    proposal = harness.service.create_proposal_from_analysis(
        report, "initialize understanding"
    )
    before = _formal_fingerprint(repo_root)

    _write(repo_root, "unrelated.py", "x = 1\n")
    with pytest.raises(CodeCortexError) as excinfo:
        harness.service.apply_cognitive_proposal(
            proposal.proposal_id, approval_for(proposal)
        )

    assert excinfo.value.code is ErrorCode.PROPOSAL_STALE
    assert _formal_fingerprint(repo_root) == before
    assert list((repo_root / ".codecortex/history/events").iterdir()) == []
    assert harness.pending.load(proposal.proposal_id).proposal_id == (
        proposal.proposal_id
    )


def test_unapproved_apply_writes_nothing(harness: M1aHarness, repo_root: Path) -> None:
    report = _full_report(harness)
    proposal = harness.service.create_proposal_from_analysis(
        report, "initialize understanding"
    )
    before = _formal_fingerprint(repo_root)
    mismatched = ApprovalRecord(
        proposal_id=proposal.proposal_id,
        patch_digest="sha256:" + "1" * 64,
        approved_by="user",
        approved_at=APPROVED_AT,
        approval_summary="Approve a different patch",
    )

    with pytest.raises(CodeCortexError) as excinfo:
        harness.service.apply_cognitive_proposal(proposal.proposal_id, mismatched)

    assert excinfo.value.code is ErrorCode.APPROVAL_MISMATCH
    assert _formal_fingerprint(repo_root) == before
    assert harness.pending.load(proposal.proposal_id).proposal_id == (
        proposal.proposal_id
    )


def test_base_revision_conflict_blocks_the_apply(harness: M1aHarness) -> None:
    first = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    second = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding again"
    )
    harness.service.apply_cognitive_proposal(first.proposal_id, approval_for(first))

    with pytest.raises(CodeCortexError) as excinfo:
        harness.service.apply_cognitive_proposal(
            second.proposal_id, approval_for(second)
        )

    assert excinfo.value.code is ErrorCode.GRAPH_REVISION_CONFLICT
    assert harness.formal_store.load().manifest.graph_revision == 1


def test_m0_approved_nodes_survive_initialization(harness: M1aHarness) -> None:
    m0 = harness.m0_services(legacy_renderer=True)
    legacy = m0.create_cognitive_proposal(
        operations=(
            PatchOperation(
                "add_node",
                "capability.manual-note",
                {
                    "id": "capability.manual-note",
                    "kind": "capability",
                    "title": "Hand-reviewed note",
                },
            ),
        ),
        affected_nodes=("capability.manual-note",),
        reason="Manually reviewed capability before analysis",
    )
    m0.apply_cognitive_proposal(legacy.proposal_id, approval_for(legacy))

    report = _full_report(harness)
    assert report.base_graph_revision == 1
    proposal = harness.service.create_proposal_from_analysis(
        report, "initialize understanding"
    )
    result = harness.service.apply_cognitive_proposal(
        proposal.proposal_id, approval_for(proposal)
    )

    state = harness.formal_store.load()
    assert result.graph_revision == 2
    assert state.manifest.cognition_initialized is True
    node_ids = {node["id"] for node in state.graph.nodes}
    assert "capability.manual-note" in node_ids


def test_second_apply_recomputes_entity_refs_without_orphans(
    harness: M1aHarness,
) -> None:
    first = harness.service.create_proposal_from_analysis(
        _full_report(harness), "initialize understanding"
    )
    harness.service.apply_cognitive_proposal(first.proposal_id, approval_for(first))
    digest = harness.formal_store.load().manifest.cognition_baseline
    uid = _answer_uid(harness)

    m0 = harness.m0_services()
    remap = m0.create_cognitive_proposal(
        operations=(PatchOperation("remove_mapping", MAP_ID, None),),
        affected_nodes=(),
        reason="Drop the only entity mapping",
    )
    harness.service.apply_cognitive_proposal(
        remap.proposal_id, approval_for(remap)
    )
    # The embedded evidence still references the entity, so its ref stays.
    state = harness.formal_store.load()
    assert [entry["uid"] for entry in state.entity_refs.entities] == [uid]

    removal = m0.create_cognitive_proposal(
        operations=(
            PatchOperation("remove_edge", EDGE_USES, None),
            PatchOperation("remove_edge", EDGE_CONTAINS, None),
            PatchOperation("set_logical_flow", "behavior.answer-question", None),
            PatchOperation("remove_node", "behavior.answer-question", None),
        ),
        affected_nodes=("behavior.answer-question",),
        reason="Remove the behavior and all of its references explicitly",
    )
    result = harness.service.apply_cognitive_proposal(
        removal.proposal_id, approval_for(removal)
    )

    state = harness.formal_store.load()
    assert result.graph_revision == 3
    assert state.entity_refs.entities == ()
    assert state.manifest.cognition_initialized is True
    assert state.manifest.cognition_baseline == digest
    event = harness.formal_store.read_history_event(result.event_id)
    assert event["change_set_summary"]["before_source_digest"] == digest
    assert event["change_set_summary"]["after_source_digest"] == digest


def test_interrupted_apply_recovers_and_stays_retryable(repo_root: Path) -> None:
    harness = M1aHarness(repo_root)
    harness.initialize_formal()
    report = _full_report(harness)
    proposal = harness.service.create_proposal_from_analysis(
        report, "initialize understanding"
    )

    def boom(stage: str) -> None:
        if stage == "graph":
            raise RuntimeError("simulated crash mid-commit")

    crashing = M1aHarness(repo_root, crash_hook=boom)
    with pytest.raises(RuntimeError):
        crashing.service.apply_cognitive_proposal(
            proposal.proposal_id, approval_for(proposal)
        )
    assert _formal_fingerprint(repo_root)["manifest.json"].count(
        '"graph_revision": 0'
    ) == 1

    recovered = M1aHarness(repo_root)
    result = recovered.service.apply_cognitive_proposal(
        proposal.proposal_id, approval_for(proposal)
    )
    assert result.graph_revision == 1
    state = recovered.formal_store.load()
    assert state.manifest.cognition_initialized is True
    assert recovered.replica.metadata().graph_revision == 1


def test_cache_refresh_failure_keeps_formal_truth_with_warning(
    repo_root: Path,
) -> None:
    class BrokenReplica:
        def rebuild(self, graph: object, graph_revision: int) -> None:
            raise OSError("simulated replica write failure")

    harness = M1aHarness(repo_root, replica=BrokenReplica())
    harness.initialize_formal()
    report = _full_report(harness)
    proposal = harness.service.create_proposal_from_analysis(
        report, "initialize understanding"
    )

    result = harness.service.apply_cognitive_proposal(
        proposal.proposal_id, approval_for(proposal)
    )

    assert result.graph_revision == 1
    assert result.cache_warnings
    assert ErrorCode.CACHE_REBUILD_REQUIRED.value in result.cache_warnings[0]
    state = harness.formal_store.load()
    assert state.manifest.cognition_initialized is True
    metadata = harness.facts.cache_metadata()
    assert metadata.graph_revision == 1
    assert metadata.baseline_entity_snapshot_completeness == "complete"
