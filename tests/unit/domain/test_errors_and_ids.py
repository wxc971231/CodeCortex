import pytest

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, new_id, validate_id


def test_new_id_encodes_injected_ulid_components():
    """A swapped timestamp, entropy, or encoding branch must change this stable ID."""
    identifier = new_id(
        IdPrefix.PROPOSAL,
        now_ms=1_727_000_000_123,
        random_bytes=bytes.fromhex("00010203040506070809"),
    )

    assert identifier == "prop_01J8CKHDKV000G40R40M30E209"
    assert validate_id(identifier, IdPrefix.PROPOSAL) == identifier


def test_validate_id_rejects_cross_namespace():
    """Removing namespace validation would let one formal object impersonate another."""
    with pytest.raises(CodeCortexError) as exc:
        validate_id("prop_01J00000000000000000000000", IdPrefix.EVENT)

    assert exc.value.code is ErrorCode.INVALID_ID


@pytest.mark.parametrize(
    ("now_ms", "random_bytes"),
    [(-1, b"\x00" * 10), (2**48, b"\x00" * 10), (0, b"\x00" * 9)],
)
def test_new_id_rejects_components_outside_ulid_bounds(now_ms, random_bytes):
    """Removing component validation could emit IDs that cannot be safely decoded."""
    with pytest.raises(CodeCortexError) as exc:
        new_id(IdPrefix.ENTITY, now_ms=now_ms, random_bytes=random_bytes)

    assert exc.value.code is ErrorCode.INVALID_ID


def test_error_serializes_the_stable_core_error_shape():
    """Dropping an error field would break adapter error translation."""
    error = CodeCortexError(
        ErrorCode.APPROVAL_REQUIRED,
        "Approval is required",
        retryable=True,
        details={"proposal_id": "prop_01J00000000000000000000000"},
        suggested_action="Approve the proposal",
    )

    assert error.to_dict() == {
        "code": "APPROVAL_REQUIRED",
        "message": "Approval is required",
        "retryable": True,
        "details": {"proposal_id": "prop_01J00000000000000000000000"},
        "suggested_action": "Approve the proposal",
    }
