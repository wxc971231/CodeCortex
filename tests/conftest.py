"""Shared fixtures for CodeCortex tests."""

from __future__ import annotations

import pytest

from codecortex.domain.graph import (
    Approval,
    Behavior,
    Capability,
    CognitiveEdge,
    CognitiveGraph,
    EdgeType,
    Evidence,
    FlowStep,
    ImplementationMapping,
    LogicalFlow,
    MappingRole,
    MappingSubjectKind,
    MaterializationStatus,
    ResolutionStatus,
    Responsibility,
)

REPLICA_EVENT_ID = "evt_01J00000000000000000000090"
ENTITY_ALPHA = "ent_01J0000000000000000000000A"
ENTITY_BRAVO = "ent_01J0000000000000000000000B"
ENTITY_CHARLIE = "ent_01J0000000000000000000000C"


def replica_ulid(prefix: str, suffix: str) -> str:
    """Build a valid ULID-shaped test ID with a readable suffix."""
    payload = "01J" + "0" * (23 - len(suffix)) + suffix
    return f"{prefix}_{payload}"


def build_discussion_graph(graph_revision: int = 1) -> CognitiveGraph:
    """Return a fully valid typed graph exercising every replica section."""
    approval = Approval(REPLICA_EVENT_ID)
    responsibility = Responsibility(
        id="responsibility.repository-understanding",
        title="Repository understanding",
        aliases=("项目理解",),
        summary="Understand the repository before editing code.",
        evidence=(
            Evidence(
                id=replica_ulid("evid", "D01"),
                kind="repository_document",
                relative_path="docs/architecture.md",
                observation="Architecture overview document.",
            ),
        ),
        approval=approval,
    )
    behavior = Behavior(
        id="behavior.checkpoint-resume",
        title="Checkpoint resume",
        aliases=("resume",),
        summary="Resume an interrupted checkpoint transaction.",
        observed="恢复流程先校验 journal 再选择方向",
        evidence=(
            Evidence(
                id=replica_ulid("evid", "E01"),
                kind="code_entity",
                entity_uid=ENTITY_ALPHA,
                relative_path="src/codecortex/infrastructure/formal.py",
                start_line=290,
                end_line=340,
                observation="commit 事务入口",
            ),
        ),
        approval=approval,
    )
    retrieval = Capability(
        id="capability.source-retrieval",
        title="Source retrieval",
        aliases=("源码检索",),
        summary="Locate the real implementation behind a conclusion.",
        approval=approval,
    )
    journal = Capability(
        id="capability.journal-recovery",
        title="Journal recovery",
        summary="Recover staged content from a commit journal.",
        approval=approval,
    )
    edge_uses = CognitiveEdge(
        id=replica_ulid("edge", "E02"),
        type=EdgeType.USES,
        source_id=behavior.id,
        target_id=retrieval.id,
        epistemic_status="inferred",
        evidence=(
            Evidence(
                id=replica_ulid("evid", "E02"),
                kind="code_entity",
                entity_uid=ENTITY_ALPHA,
                relative_path="src/codecortex/infrastructure/formal.py",
                start_line=341,
                end_line=360,
                observation="commit 读取 staged 内容",
            ),
        ),
        approval=approval,
    )
    step_load = FlowStep(
        id="behavior.checkpoint-resume#step.load-journal",
        order=1,
        title="Load journal",
        summary="Read the interrupted transaction journal.",
        uses_capabilities=(journal.id,),
        evidence=(
            Evidence(
                id=replica_ulid("evid", "E03"),
                kind="code_entity",
                entity_uid=ENTITY_BRAVO,
                relative_path="src/codecortex/infrastructure/locking.py",
                start_line=10,
                end_line=30,
                observation="锁与 journal 读取",
            ),
        ),
        approval=approval,
    )
    step_replay = FlowStep(
        id="behavior.checkpoint-resume#step.replay-operation",
        order=2,
        title="Replay operation",
        summary="Replay or roll back the staged operation.",
        uses_capabilities=(retrieval.id,),
        approval=approval,
    )
    mapping_node = ImplementationMapping(
        id=replica_ulid("map", "M01"),
        subject_kind=MappingSubjectKind.NODE,
        subject_id=behavior.id,
        entity_uid=ENTITY_ALPHA,
        role=MappingRole.PRIMARY,
        resolution_status=ResolutionStatus.RESOLVED,
        evidence_note="commit 实现恢复事务",
        evidence=(
            Evidence(
                id=replica_ulid("evid", "E04"),
                kind="code_entity",
                entity_uid=ENTITY_BRAVO,
                relative_path="src/codecortex/infrastructure/formal.py",
                start_line=430,
                end_line=460,
                observation="恢复路径选择",
            ),
        ),
        approval=approval,
    )
    mapping_step = ImplementationMapping(
        id=replica_ulid("map", "M02"),
        subject_kind=MappingSubjectKind.FLOW_STEP,
        subject_id=step_load.id,
        entity_uid=ENTITY_BRAVO,
        role=MappingRole.SUPPORTING,
        resolution_status=ResolutionStatus.MISSING,
        evidence_note="锁实现已迁移",
        approval=approval,
    )
    mapping_ambiguous = ImplementationMapping(
        id=replica_ulid("map", "M03"),
        subject_kind=MappingSubjectKind.NODE,
        subject_id=behavior.id,
        entity_uid=ENTITY_CHARLIE,
        role=MappingRole.SUPPORTING,
        resolution_status=ResolutionStatus.AMBIGUOUS,
        evidence_note="恢复入口存在多个候选",
        approval=approval,
    )
    return CognitiveGraph(
        graph_revision=graph_revision,
        nodes=(responsibility, behavior, retrieval, journal),
        semantic_edges=(
            CognitiveEdge(
                id=replica_ulid("edge", "E01"),
                type=EdgeType.CONTAINS,
                source_id=responsibility.id,
                target_id=behavior.id,
                epistemic_status="established",
                approval=approval,
            ),
            edge_uses,
            CognitiveEdge(
                id=replica_ulid("edge", "E03"),
                type=EdgeType.USES,
                source_id=behavior.id,
                target_id=journal.id,
                epistemic_status="established",
                approval=approval,
            ),
            CognitiveEdge(
                id=replica_ulid("edge", "E04"),
                type=EdgeType.DEPENDS_ON,
                source_id=journal.id,
                target_id=retrieval.id,
                epistemic_status="established",
                approval=approval,
            ),
        ),
        logical_flows=(
            LogicalFlow(
                behavior_id=behavior.id,
                materialization_status=MaterializationStatus.MATERIALIZED,
                flow_revision=1,
                steps=(step_load, step_replay),
                approval=approval,
            ),
        ),
        implementation_mappings=(mapping_node, mapping_step, mapping_ambiguous),
        entity_uids=frozenset({ENTITY_ALPHA, ENTITY_BRAVO, ENTITY_CHARLIE}),
    )


@pytest.fixture
def discussion_graph() -> CognitiveGraph:
    return build_discussion_graph()


@pytest.fixture
def replica_entity_refs():
    """Formal entity-ref records mirroring `.codecortex/entity_refs.json`."""
    from codecortex.infrastructure.persistence.graph_replica import EntityRefRecord

    return (
        EntityRefRecord(
            uid=ENTITY_ALPHA,
            last_known_address="codecortex.infrastructure.formal:FormalStore.commit",
            kind="method",
            relative_path="src/codecortex/infrastructure/formal.py",
            signature="(self, state, event, views) -> None",
            fingerprint="sha256:" + "a" * 64,
            resolution_status="resolved",
        ),
        EntityRefRecord(
            uid=ENTITY_BRAVO,
            last_known_address="codecortex.infrastructure.locking:RepositoryLock.acquire",
            kind="method",
            relative_path="src/codecortex/infrastructure/locking.py",
            signature="(self) -> None",
            fingerprint="sha256:" + "b" * 64,
            resolution_status="missing",
        ),
        EntityRefRecord(
            uid=ENTITY_CHARLIE,
            last_known_address="codecortex.infrastructure.formal:recover_interrupted",
            kind="function",
            relative_path="src/codecortex/infrastructure/formal.py",
            signature=None,
            fingerprint="sha256:" + "c" * 64,
            resolution_status="ambiguous",
        ),
    )


@pytest.fixture
def replica_history_events():
    """Formal history-event records mirroring `history/events/*.json`."""
    from codecortex.infrastructure.persistence.graph_replica import HistoryEventRecord

    return (
        HistoryEventRecord(
            event_id=REPLICA_EVENT_ID,
            event_type="cognitive_proposal_applied",
            resulting_graph_revision=1,
            created_at="2026-09-03T00:00:00+00:00",
            relative_path=f"history/events/{REPLICA_EVENT_ID}.json",
        ),
    )
