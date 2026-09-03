"""Stable errors shared by CodeCortex's domain and adapters."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """M0 error codes which adapters may safely translate."""

    NOT_INITIALIZED = "NOT_INITIALIZED"
    PATH_OUTSIDE_REPOSITORY = "PATH_OUTSIDE_REPOSITORY"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    CACHE_REBUILD_REQUIRED = "CACHE_REBUILD_REQUIRED"
    ANALYSIS_REPORT_INVALID = "ANALYSIS_REPORT_INVALID"
    PROPOSAL_STALE = "PROPOSAL_STALE"
    GRAPH_REVISION_CONFLICT = "GRAPH_REVISION_CONFLICT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    FORMAL_STATE_CORRUPT = "FORMAL_STATE_CORRUPT"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    INVALID_ID = "INVALID_ID"


class CodeCortexError(Exception):
    """A structured, stable error for expected Core failures."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = {} if details is None else details
        self.suggested_action = suggested_action

    def to_dict(self) -> dict[str, Any]:
        """Return the adapter-facing stable error payload."""
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "suggested_action": self.suggested_action,
        }
