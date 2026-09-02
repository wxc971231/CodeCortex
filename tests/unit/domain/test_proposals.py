from dataclasses import replace

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import (
    ApprovalRecord,
    PatchOperation,
    Proposal,
    ProposalStatus,
    canonical_patch_digest,
)

PROPOSAL_ID = "prop_01J00000000000000000000000"
OTHER_PROPOSAL_ID = "prop_01J00000000000000000000001"
EDGE_ID = "edge_01J00000000000000000000000"
CREATED_AT = "2026-09-02T01:23:45Z"
REVISED_AT = "2026-09-02T01:24:45.123456Z"


def node_payload(node_id: str = "behavior.answer-question") -> dict[str, object]:
    return {
        "id": node_id,
        "kind": "behavior",
        "title": "Answer a repository question",
    }


def edge_payload() -> dict[str, object]:
    return {
        "id": EDGE_ID,
        "type": "contains",
        "source_id": "responsibility.repository-understanding",
        "target_id": "behavior.answer-question",
    }


def add_node_operation() -> PatchOperation:
    return PatchOperation("add_node", "behavior.answer-question", node_payload())


def make_proposal(
    *,
    operations: tuple[PatchOperation, ...] | None = None,
    status: ProposalStatus = ProposalStatus.PROPOSED,
) -> Proposal:
    proposal = Proposal.create(
        proposal_id=PROPOSAL_ID,
        base_graph_revision=0,
        analyzed_source_digest=None,
        source_preconditions=(),
        operations=operations or (add_node_operation(),),
        affected_nodes=("behavior.answer-question",),
        reason="Record how repository questions are answered",
        evidence=({"summary": "M0 acceptance fixture"},),
        uncertainties=("Real source mappings arrive in M1a",),
        created_at=CREATED_AT,
    )
    return replace(proposal, status=status)


def approval_for(
    proposal: Proposal,
    *,
    proposal_id: str | None = None,
    patch_digest: str | None = None,
    approved_by: str = "user",
    approved_at: str = "2026-09-02T02:00:00Z",
    approval_summary: str = "Approve the displayed cognitive patch",
) -> ApprovalRecord:
    return ApprovalRecord(
        proposal_id=proposal.proposal_id if proposal_id is None else proposal_id,
        patch_digest=proposal.patch_digest if patch_digest is None else patch_digest,
        approved_by=approved_by,
        approved_at=approved_at,
        approval_summary=approval_summary,
    )


@pytest.mark.parametrize(
    ("kind", "target_id", "value"),
    [
        ("add_node", "behavior.answer-question", node_payload()),
        ("update_node", "behavior.answer-question", node_payload()),
        ("remove_node", "behavior.answer-question", None),
        ("add_edge", EDGE_ID, edge_payload()),
        ("update_edge", EDGE_ID, edge_payload()),
        ("remove_edge", EDGE_ID, None),
    ],
)
def test_patch_supports_exactly_the_six_stable_id_operations(
    kind: str, target_id: str, value: dict[str, object] | None
) -> None:
    """Dropping an approved operation kind would reject a valid M0 cognitive patch."""
    operation = PatchOperation(kind, target_id, value)

    assert operation.kind == kind
    assert operation.target_id == target_id
    assert operation.value == value


@pytest.mark.parametrize(
    ("kind", "target_id", "value", "expected_code"),
    [
        (
            "move_node",
            "behavior.answer-question",
            node_payload(),
            ErrorCode.ANALYSIS_REPORT_INVALID,
        ),
        ("add_node", EDGE_ID, edge_payload(), ErrorCode.INVALID_ID),
        (
            "add_node",
            "behavior.answer.question",
            {
                "id": "behavior.answer.question",
                "kind": "behavior",
                "title": "Invalid dotted slug",
            },
            ErrorCode.INVALID_ID,
        ),
        (
            "add_edge",
            "behavior.answer-question",
            node_payload(),
            ErrorCode.INVALID_ID,
        ),
        ("add_edge", None, edge_payload(), ErrorCode.INVALID_ID),
        (
            "add_node",
            "behavior.answer-question",
            None,
            ErrorCode.ANALYSIS_REPORT_INVALID,
        ),
        (
            "remove_node",
            "behavior.answer-question",
            node_payload(),
            ErrorCode.ANALYSIS_REPORT_INVALID,
        ),
        (
            "update_node",
            "behavior.answer-question",
            node_payload("behavior.different"),
            ErrorCode.ANALYSIS_REPORT_INVALID,
        ),
    ],
)
def test_patch_rejects_unsupported_kinds_namespaces_and_payload_shapes(
    kind: str,
    target_id: object,
    value: dict[str, object] | None,
    expected_code: ErrorCode,
) -> None:
    """Weak operation validation would let ambiguous or misaddressed patches persist."""
    with pytest.raises(CodeCortexError) as exc:
        PatchOperation(kind, target_id, value)  # type: ignore[arg-type]

    assert exc.value.code is expected_code


def test_patch_digest_ignores_json_key_order() -> None:
    """Hashing insertion order would invalidate approval for semantically identical JSON."""
    first = PatchOperation(
        "add_node",
        "behavior.answer-question",
        {"id": "behavior.answer-question", "kind": "behavior", "title": "Answer"},
    )
    reordered = PatchOperation(
        "add_node",
        "behavior.answer-question",
        {"title": "Answer", "kind": "behavior", "id": "behavior.answer-question"},
    )

    assert canonical_patch_digest((first,)) == canonical_patch_digest((reordered,))
    assert canonical_patch_digest((first,)).startswith("sha256:")
    assert len(canonical_patch_digest((first,))) == 71


def test_patch_digest_changes_when_operation_order_changes() -> None:
    """Treating an ordered patch as a set could approve a different resulting graph."""
    add = add_node_operation()
    remove = PatchOperation("remove_node", "behavior.answer-question", None)

    assert canonical_patch_digest((add, remove)) != canonical_patch_digest(
        (remove, add)
    )


def test_patch_rejects_python_values_that_do_not_round_trip_as_json() -> None:
    """Tuple coercion during persistence would make the restored patch differ in memory."""
    value = node_payload()
    value["aliases"] = ("ask", "explain")

    with pytest.raises(CodeCortexError) as exc:
        PatchOperation("add_node", "behavior.answer-question", value)

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_proposal_requires_nonblank_reason() -> None:
    """Persisting a blank reason would remove the human rationale from review."""
    with pytest.raises(CodeCortexError) as exc:
        Proposal.create(
            proposal_id=PROPOSAL_ID,
            base_graph_revision=0,
            analyzed_source_digest=None,
            source_preconditions=(),
            operations=(add_node_operation(),),
            affected_nodes=("behavior.answer-question",),
            reason=" \n ",
            evidence=(),
            uncertainties=(),
            created_at=CREATED_AT,
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_revision_changes_patch_digest_and_invalidates_old_approval() -> None:
    """Keeping an old approval valid would apply a patch the user never reviewed."""
    proposal = make_proposal()
    old_approval = approval_for(proposal)
    revised_operation = PatchOperation(
        "add_node",
        "behavior.new",
        {"id": "behavior.new", "kind": "behavior", "title": "New behavior"},
    )

    revised = proposal.revise(
        (revised_operation,),
        reason="Add the behavior discussed with the user",
        revised_at=REVISED_AT,
    )

    assert revised.patch_digest != proposal.patch_digest
    assert revised.status is ProposalStatus.PROPOSED
    assert revised.revision_log[0].revision_number == 1
    assert revised.revision_log[0].previous_patch_digest == proposal.patch_digest
    assert revised.revision_log[0].previous_operations == proposal.operations
    assert revised.revision_log[0].reason == "Add the behavior discussed with the user"
    assert revised.revision_log[0].revised_at == REVISED_AT
    with pytest.raises(CodeCortexError) as exc:
        revised.verify_approval(old_approval)
    assert exc.value.code is ErrorCode.APPROVAL_MISMATCH


def test_revision_supplies_a_strict_utc_time_when_caller_omits_it() -> None:
    """Requiring adapters to invent revision timestamps would fragment the domain API."""
    revised = make_proposal().revise(
        (PatchOperation("remove_node", "behavior.answer-question", None),),
        reason="Remove behavior after discussion",
    )

    assert revised.revision_log[-1].revised_at.endswith("Z")
    assert "+00:00" not in revised.revision_log[-1].revised_at


@pytest.mark.parametrize(
    "status",
    [ProposalStatus.APPROVED, ProposalStatus.APPLIED, ProposalStatus.REJECTED],
)
def test_terminal_or_approved_proposal_cannot_be_revised(
    status: ProposalStatus,
) -> None:
    """Revising after approval or completion would bypass a fresh review cycle."""
    proposal = make_proposal(status=status)

    with pytest.raises(CodeCortexError) as exc:
        proposal.revise(
            (add_node_operation(),), reason="Late revision", revised_at=REVISED_AT
        )

    assert exc.value.code is ErrorCode.PROPOSAL_STALE


def test_proposal_rejects_a_stale_graph_revision() -> None:
    """Applying against a newer graph would overwrite cognition the proposal never saw."""
    proposal = make_proposal()

    with pytest.raises(CodeCortexError) as exc:
        proposal.verify_base_graph_revision(1)

    assert exc.value.code is ErrorCode.GRAPH_REVISION_CONFLICT


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"proposal_id": OTHER_PROPOSAL_ID}, ErrorCode.APPROVAL_MISMATCH),
        ({"patch_digest": "sha256:" + "f" * 64}, ErrorCode.APPROVAL_MISMATCH),
        ({"approved_by": "main_codex"}, ErrorCode.APPROVAL_REQUIRED),
        ({"approved_at": "2026-09-02T02:00:00+00:00"}, ErrorCode.APPROVAL_REQUIRED),
        ({"approved_at": "2026-02-30T02:00:00Z"}, ErrorCode.APPROVAL_REQUIRED),
        ({"approval_summary": " \n"}, ErrorCode.APPROVAL_REQUIRED),
        ({"approval_summary": "批" * 501}, ErrorCode.APPROVAL_REQUIRED),
    ],
)
def test_approval_requires_current_identity_digest_and_explicit_user_record(
    overrides: dict[str, str], expected_code: ErrorCode
) -> None:
    """Relaxing approval fields would detach apply permission from the displayed patch."""
    proposal = make_proposal()

    with pytest.raises(CodeCortexError) as exc:
        proposal.verify_approval(approval_for(proposal, **overrides))

    assert exc.value.code is expected_code


def test_approval_accepts_strict_utc_time_and_500_unicode_code_points() -> None:
    """Counting UTF-8 bytes would reject a valid 500-character user summary."""
    proposal = make_proposal()
    approval = approval_for(
        proposal,
        approved_at="2026-09-02T02:00:00.123456Z",
        approval_summary="批" * 500,
    )

    proposal.verify_approval(approval)


def test_approval_rejects_operations_mutated_after_the_digest_was_created() -> None:
    """A mutable nested patch must not allow applying content outside the approved digest."""
    proposal = make_proposal()
    assert proposal.operations[0].value is not None
    proposal.operations[0].value["title"] = "Tampered after proposal creation"

    with pytest.raises(CodeCortexError) as exc:
        proposal.verify_approval(approval_for(proposal))

    assert exc.value.code is ErrorCode.APPROVAL_MISMATCH
