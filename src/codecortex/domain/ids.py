"""ULID-based identifiers with CodeCortex namespace validation."""

import secrets
import time
from enum import StrEnum

from codecortex.domain.errors import CodeCortexError, ErrorCode

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_PAYLOAD_LENGTH = 26
_TIMESTAMP_BITS = 48
_RANDOM_BYTES_LENGTH = 10


class IdPrefix(StrEnum):
    """Prefixes reserved for stable formal-state identifiers."""

    ENTITY = "ent"
    EDGE = "edge"
    MAPPING = "map"
    EVIDENCE = "evid"
    PROPOSAL = "prop"
    CHANGE_SET = "chg"
    EVENT = "evt"


def new_id(
    prefix: IdPrefix,
    *,
    now_ms: int | None = None,
    random_bytes: bytes | None = None,
) -> str:
    """Create a namespaced ULID using UTC milliseconds and secure entropy."""
    timestamp = _utc_now_ms() if now_ms is None else now_ms
    entropy = secrets.token_bytes(_RANDOM_BYTES_LENGTH) if random_bytes is None else random_bytes
    _validate_components(timestamp, entropy)
    payload = (timestamp << (_RANDOM_BYTES_LENGTH * 8)) | int.from_bytes(entropy, "big")
    return f"{prefix.value}_{_encode_crockford_26(payload)}"


def validate_id(value: str, prefix: IdPrefix) -> str:
    """Validate a formal identifier and return it unchanged when it is valid."""
    expected_prefix = f"{prefix.value}_"
    payload = value.removeprefix(expected_prefix)
    if not value.startswith(expected_prefix) or len(payload) != _ULID_PAYLOAD_LENGTH:
        _raise_invalid_id("Identifier has an invalid namespace or length")
    if any(character not in _CROCKFORD_BASE32 for character in payload):
        _raise_invalid_id("Identifier payload is not uppercase Crockford Base32")

    numeric_payload = 0
    for character in payload:
        numeric_payload = (numeric_payload << 5) | _CROCKFORD_BASE32.index(character)
    if numeric_payload >= 1 << (_TIMESTAMP_BITS + _RANDOM_BYTES_LENGTH * 8):
        _raise_invalid_id("Identifier payload exceeds ULID bounds")
    return value


def _utc_now_ms() -> int:
    return time.time_ns() // 1_000_000


def _validate_components(now_ms: int, random_bytes: bytes) -> None:
    if not 0 <= now_ms < 1 << _TIMESTAMP_BITS:
        _raise_invalid_id("ULID timestamp is outside the 48-bit millisecond range")
    if len(random_bytes) != _RANDOM_BYTES_LENGTH:
        _raise_invalid_id("ULID entropy must contain exactly 10 bytes")


def _encode_crockford_26(payload: int) -> str:
    return "".join(
        _CROCKFORD_BASE32[(payload >> (5 * index)) & 0b11111]
        for index in range(_ULID_PAYLOAD_LENGTH - 1, -1, -1)
    )


def _raise_invalid_id(message: str) -> None:
    raise CodeCortexError(ErrorCode.INVALID_ID, message)
