"""Decision and approval contracts for a no-graph-change baseline advance."""

from __future__ import annotations

import pytest

from codecortex.application.baseline import (
    BaselineAdvanceService,
    BaselineApprovalRecord,
    DecisionRecord,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode


def _decision(actor: str = "analyzer") -> DecisionRecord:
    return DecisionRecord(
        decided_by=actor,
        evidence_summary="Current source was reviewed; no graph change is needed.",
        decided_at="2026-09-04T08:00:00Z",
    )


def test_no_semantic_change_needs_decision_but_not_user_approval() -> None:
    decision = _decision()

    assert decision.decided_by == "analyzer"
    assert decision.evidence_summary


def test_user_accepted_requires_matching_change_set_approval() -> None:
    approval = BaselineApprovalRecord(
        change_set_id="chg_01J00000000000000000000000",
        source_digest="sha256:" + "a" * 64,
        approved_by="user",
        approved_at="2026-09-04T08:01:00Z",
        approval_summary="The current cognition is still valid.",
    )

    with pytest.raises(CodeCortexError) as raised:
        BaselineAdvanceService.verify_approval(
            approval,
            "chg_01J00000000000000000000001",
            "sha256:" + "a" * 64,
        )

    assert raised.value.code is ErrorCode.APPROVAL_MISMATCH


def test_user_accepted_requires_an_explicit_approval() -> None:
    with pytest.raises(CodeCortexError) as raised:
        BaselineAdvanceService.require_approval("user_accepted", None)

    assert raised.value.code is ErrorCode.APPROVAL_REQUIRED
