from dataclasses import FrozenInstanceError, replace

import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.proposals import (
    ApprovalRecord,
    FrozenJsonArray,
    FrozenJsonObject,
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


def test_analysis_precondition_is_covered_by_the_patch_digest() -> None:
    unbound = PatchOperation("add_node", "behavior.answer-question", node_payload())
    bound = PatchOperation(
        "add_node",
        "behavior.answer-question",
        node_payload(),
        expected_revision="absent",
    )

    assert canonical_patch_digest((unbound,)) != canonical_patch_digest((bound,))
    assert bound.to_canonical_value()["expected_revision"] == "absent"


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


def test_revision_rejects_a_reason_only_change_with_the_same_patch() -> None:
    """A reason-only revision must not leave an old patch approval reusable."""
    proposal = make_proposal()

    with pytest.raises(CodeCortexError) as exc:
        proposal.revise(
            proposal.operations,
            reason="Only the explanation changed",
            revised_at=REVISED_AT,
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_revision_rejects_an_evidence_only_change_with_the_same_patch() -> None:
    """Evidence-only revision cannot claim to invalidate an unchanged patch digest."""
    proposal = make_proposal()

    with pytest.raises(CodeCortexError) as exc:
        proposal.revise(
            proposal.operations,
            reason="Replace supporting evidence",
            evidence=({"summary": "Different evidence"},),
            revised_at=REVISED_AT,
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_revision_rejects_equivalent_new_operations_with_the_same_digest() -> None:
    """Rebuilding equal operations must not disguise a no-op revision."""
    proposal = make_proposal()
    equivalent = PatchOperation(
        "add_node",
        "behavior.answer-question",
        {
            "title": "Answer a repository question",
            "kind": "behavior",
            "id": "behavior.answer-question",
        },
    )

    with pytest.raises(CodeCortexError) as exc:
        proposal.revise(
            (equivalent,), reason="Equivalent operation", revised_at=REVISED_AT
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_revision_supplies_a_strict_utc_time_when_caller_omits_it() -> None:
    """Requiring adapters to invent revision timestamps would fragment the domain API."""
    revised = make_proposal().revise(
        (PatchOperation("remove_node", "behavior.answer-question", None),),
        reason="Remove behavior after discussion",
    )

    assert revised.revision_log[-1].revised_at.endswith("Z")
    assert "+00:00" not in revised.revision_log[-1].revised_at


def test_revision_rejects_an_explicit_blank_time() -> None:
    """A supplied blank timestamp must not be replaced with the current clock."""
    proposal = make_proposal()

    with pytest.raises(CodeCortexError) as exc:
        proposal.revise(
            (PatchOperation("remove_node", "behavior.answer-question", None),),
            reason="Remove behavior after discussion",
            revised_at="",
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


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


def test_proposal_deep_snapshots_json_before_and_after_approval_verification() -> None:
    """Caller-owned nested containers must never mutate an approved Proposal snapshot."""
    aliases = ["ask"]
    operation_value = node_payload()
    operation_value["metadata"] = {"aliases": aliases}
    operation = PatchOperation("add_node", "behavior.answer-question", operation_value)
    fingerprints = ["fp-one"]
    source_precondition = {
        "relative_path": "src/query.py",
        "fingerprints": fingerprints,
    }
    locations = ["src/query.py:10"]
    evidence = {"summary": "Query behavior", "locations": locations}
    questions = ["Does fallback apply?"]
    uncertainty = {"questions": questions}
    proposal = Proposal.create(
        proposal_id=PROPOSAL_ID,
        base_graph_revision=0,
        analyzed_source_digest=None,
        source_preconditions=(source_precondition,),
        operations=(operation,),
        affected_nodes=("behavior.answer-question",),
        reason="Snapshot nested candidate data",
        evidence=(evidence,),
        uncertainties=(uncertainty,),
        created_at=CREATED_AT,
    )
    approval = approval_for(proposal)

    aliases.append("explain-before")
    fingerprints.append("fp-two")
    proposal.verify_approval(approval)
    locations.append("src/query.py:20")
    questions.append("Was the graph consulted?")
    proposal.verify_approval(approval)

    assert proposal.operations[0].to_canonical_value()["value"] == {
        "id": "behavior.answer-question",
        "kind": "behavior",
        "metadata": {"aliases": ["ask"]},
        "title": "Answer a repository question",
    }
    assert proposal.source_preconditions[0]["fingerprints"] == ("fp-one",)
    assert proposal.evidence[0]["locations"] == ("src/query.py:10",)
    assert proposal.uncertainties[0]["questions"] == ("Does fallback apply?",)


def test_revision_validates_the_preserved_operations_digest() -> None:
    """A forged revision record must not detach its previous digest from its patch."""
    revised = make_proposal().revise(
        (PatchOperation("remove_node", "behavior.answer-question", None),),
        reason="Remove behavior",
        revised_at=REVISED_AT,
    )

    with pytest.raises(CodeCortexError) as exc:
        replace(
            revised.revision_log[0],
            previous_patch_digest="sha256:" + "f" * 64,
        )

    assert exc.value.code is ErrorCode.ANALYSIS_REPORT_INVALID


def test_existing_frozen_json_is_resnapshotted_before_approval_and_history() -> None:
    """Treating a caller-made frozen container as trusted could mutate approved history."""
    alias = {"name": "ask"}
    external = FrozenJsonObject(
        (
            ("id", "behavior.answer-question"),
            ("kind", "behavior"),
            (
                "metadata",
                FrozenJsonObject(
                    (("aliases", FrozenJsonArray((alias,))),)
                ),
            ),
            ("title", "Answer a repository question"),
        )
    )
    proposal = make_proposal(
        operations=(
            PatchOperation("add_node", "behavior.answer-question", external),
        )
    )
    approval = approval_for(proposal)

    alias["name"] = "changed-before-verification"
    proposal.verify_approval(approval)
    revised = proposal.revise(
        (PatchOperation("remove_node", "behavior.answer-question", None),),
        reason="Remove the behavior",
        revised_at=REVISED_AT,
    )
    alias["name"] = "changed-after-verification"

    history = revised.revision_log[0]
    assert canonical_patch_digest(history.previous_operations) == (
        history.previous_patch_digest
    )
    assert history.previous_operations[0].to_canonical_value()["value"] == {
        "id": "behavior.answer-question",
        "kind": "behavior",
        "metadata": {"aliases": [{"name": "ask"}]},
        "title": "Answer a repository question",
    }
    revised.verify_approval(approval_for(revised))


def test_frozen_json_object_rejects_normal_reassignment_before_and_after_use() -> None:
    """A writable frozen-object attribute would let callers replace domain JSON state."""
    external = FrozenJsonObject((("summary", "Initial evidence"),))

    with pytest.raises((FrozenInstanceError, AttributeError)):
        external._items = (("summary", "mutated before use"),)  # type: ignore[misc]

    proposal = Proposal.create(
        proposal_id=PROPOSAL_ID,
        base_graph_revision=0,
        analyzed_source_digest=None,
        source_preconditions=(),
        operations=(add_node_operation(),),
        affected_nodes=("behavior.answer-question",),
        reason="Keep evidence immutable",
        evidence=(external,),
        uncertainties=(),
        created_at=CREATED_AT,
    )
    proposal.verify_approval(approval_for(proposal))

    with pytest.raises((FrozenInstanceError, AttributeError)):
        external._items = (("summary", "mutated after use"),)  # type: ignore[misc]

    assert proposal.evidence[0]["summary"] == "Initial evidence"
