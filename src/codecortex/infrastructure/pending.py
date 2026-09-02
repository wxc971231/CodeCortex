"""Machine-local deterministic persistence for pending cognitive proposals."""

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, validate_id
from codecortex.domain.proposals import (
    PatchOperation,
    Proposal,
    ProposalRevision,
    ProposalStatus,
    json_value_to_mutable,
)
from codecortex.infrastructure.jsonio import canonical_json_bytes, write_json_atomic
from codecortex.infrastructure.repository import Repository

_PROPOSAL_FIELDS = {
    "schema_version",
    "proposal_id",
    "status",
    "base_graph_revision",
    "analyzed_source_digest",
    "source_preconditions",
    "operations",
    "affected_nodes",
    "reason",
    "evidence",
    "uncertainties",
    "revision_log",
    "patch_digest",
    "created_at",
}
_OPERATION_FIELDS = {"kind", "target_id", "value"}
_REVISION_FIELDS = {
    "revision_number",
    "reason",
    "previous_reason",
    "previous_patch_digest",
    "previous_operations",
    "previous_analyzed_source_digest",
    "previous_source_preconditions",
    "previous_affected_nodes",
    "previous_evidence",
    "previous_uncertainties",
    "revised_at",
}


class PendingProposalStore:
    """Store current proposal snapshots under the repository's deletable cache."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository
        self._directory = repository.resolve_relative(
            ".codecortex/.cache/pending_proposals"
        )

    def create(self, proposal: Proposal) -> None:
        """Create a pending file without silently replacing an existing proposal."""
        path = self._path(proposal.proposal_id)
        if os.path.lexists(path):
            raise CodeCortexError(
                ErrorCode.ANALYSIS_REPORT_INVALID,
                "A pending proposal with this ID already exists",
            )
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._ensure_safe_path(path)
            write_json_atomic(path, _proposal_to_json(proposal))
        except CodeCortexError:
            raise
        except OSError as error:
            raise _pending_io_error("create", proposal.proposal_id) from error

    def load(self, proposal_id: str) -> Proposal:
        """Load a canonical pending snapshot and restore domain invariants."""
        path = self._path(proposal_id)
        if not _is_regular_file(path):
            raise CodeCortexError(
                ErrorCode.PROPOSAL_STALE,
                "Pending cognitive proposal was not found",
                details={"proposal_id": proposal_id},
                suggested_action="Recreate the proposal",
            )
        try:
            payload = _read_bytes_no_follow(path)
            data = json.loads(payload)
            if canonical_json_bytes(data) != payload:
                raise ValueError("pending proposal is not canonical JSON")
            if not isinstance(data, dict) or set(data) != _PROPOSAL_FIELDS:
                raise ValueError("pending proposal fields are invalid")
            return _proposal_from_json(data)
        except CodeCortexError:
            raise
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise CodeCortexError(
                ErrorCode.ANALYSIS_REPORT_INVALID,
                "Pending cognitive proposal is invalid",
                details={"proposal_id": proposal_id},
                suggested_action="Delete or recreate the pending proposal",
            ) from error

    def replace(self, proposal: Proposal) -> None:
        """Atomically replace only the current file for an existing identity."""
        path = self._path(proposal.proposal_id)
        if not _is_regular_file(path):
            raise CodeCortexError(
                ErrorCode.PROPOSAL_STALE,
                "Pending cognitive proposal was not found",
                details={"proposal_id": proposal.proposal_id},
                suggested_action="Recreate the proposal",
            )
        try:
            write_json_atomic(path, _proposal_to_json(proposal))
        except OSError as error:
            raise _pending_io_error("replace", proposal.proposal_id) from error

    def delete(self, proposal_id: str) -> None:
        """Remove a consumed pending proposal without touching other cache state."""
        path = self._path(proposal_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise _pending_io_error("delete", proposal_id) from error

    def _path(self, proposal_id: str) -> Path:
        validate_id(proposal_id, IdPrefix.PROPOSAL)
        path = self._directory / f"{proposal_id}.json"
        self._ensure_safe_path(path)
        return path

    def _ensure_safe_path(self, path: Path) -> None:
        self._repository.to_relative(path)
        relative = path.relative_to(self._repository.root)
        cursor = self._repository.root
        for part in relative.parts:
            cursor /= part
            try:
                mode = cursor.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _pending_io_error("inspect", path.stem) from error
            if stat.S_ISLNK(mode):
                raise CodeCortexError(
                    ErrorCode.PATH_OUTSIDE_REPOSITORY,
                    "Pending proposal path cannot contain symbolic links",
                    details={"path": relative.as_posix()},
                )


def _proposal_to_json(proposal: Proposal) -> dict[str, object]:
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "status": proposal.status.value,
        "base_graph_revision": proposal.base_graph_revision,
        "analyzed_source_digest": proposal.analyzed_source_digest,
        "source_preconditions": [
            json_value_to_mutable(item) for item in proposal.source_preconditions
        ],
        "operations": [_operation_to_json(item) for item in proposal.operations],
        "affected_nodes": list(proposal.affected_nodes),
        "reason": proposal.reason,
        "evidence": [json_value_to_mutable(item) for item in proposal.evidence],
        "uncertainties": [
            json_value_to_mutable(item) for item in proposal.uncertainties
        ],
        "revision_log": [_revision_to_json(item) for item in proposal.revision_log],
        "patch_digest": proposal.patch_digest,
        "created_at": proposal.created_at,
    }


def _proposal_from_json(data: Mapping[str, object]) -> Proposal:
    return Proposal(
        schema_version=_integer(data, "schema_version"),
        proposal_id=_string(data, "proposal_id"),
        status=ProposalStatus(_string(data, "status")),
        base_graph_revision=_integer(data, "base_graph_revision"),
        analyzed_source_digest=_optional_string(data, "analyzed_source_digest"),
        source_preconditions=_object_tuple(data, "source_preconditions"),
        operations=tuple(
            _operation_from_json(item) for item in _mapping_list(data, "operations")
        ),
        affected_nodes=_string_tuple(data, "affected_nodes"),
        reason=_string(data, "reason"),
        evidence=_object_tuple(data, "evidence"),
        uncertainties=tuple(_list(data, "uncertainties")),
        revision_log=tuple(
            _revision_from_json(item) for item in _mapping_list(data, "revision_log")
        ),
        patch_digest=_string(data, "patch_digest"),
        created_at=_string(data, "created_at"),
    )


def _operation_to_json(operation: PatchOperation) -> dict[str, object]:
    return operation.to_canonical_value()


def _operation_from_json(data: Mapping[str, object]) -> PatchOperation:
    if set(data) != _OPERATION_FIELDS:
        raise ValueError("patch operation fields are invalid")
    value = data["value"]
    if value is not None and not isinstance(value, dict):
        raise TypeError("patch operation value must be an object or null")
    return PatchOperation(
        kind=_string(data, "kind"),
        target_id=_string(data, "target_id"),
        value=value,
    )


def _revision_to_json(revision: ProposalRevision) -> dict[str, object]:
    return {
        "revision_number": revision.revision_number,
        "reason": revision.reason,
        "previous_reason": revision.previous_reason,
        "previous_patch_digest": revision.previous_patch_digest,
        "previous_operations": [
            _operation_to_json(item) for item in revision.previous_operations
        ],
        "previous_analyzed_source_digest": revision.previous_analyzed_source_digest,
        "previous_source_preconditions": [
            json_value_to_mutable(item)
            for item in revision.previous_source_preconditions
        ],
        "previous_affected_nodes": list(revision.previous_affected_nodes),
        "previous_evidence": [
            json_value_to_mutable(item) for item in revision.previous_evidence
        ],
        "previous_uncertainties": [
            json_value_to_mutable(item) for item in revision.previous_uncertainties
        ],
        "revised_at": revision.revised_at,
    }


def _revision_from_json(data: Mapping[str, object]) -> ProposalRevision:
    if set(data) != _REVISION_FIELDS:
        raise ValueError("proposal revision fields are invalid")
    return ProposalRevision(
        revision_number=_integer(data, "revision_number"),
        reason=_string(data, "reason"),
        previous_reason=_string(data, "previous_reason"),
        previous_patch_digest=_string(data, "previous_patch_digest"),
        previous_operations=tuple(
            _operation_from_json(item)
            for item in _mapping_list(data, "previous_operations")
        ),
        previous_analyzed_source_digest=_optional_string(
            data, "previous_analyzed_source_digest"
        ),
        previous_source_preconditions=_object_tuple(
            data, "previous_source_preconditions"
        ),
        previous_affected_nodes=_string_tuple(data, "previous_affected_nodes"),
        previous_evidence=_object_tuple(data, "previous_evidence"),
        previous_uncertainties=tuple(_list(data, "previous_uncertainties")),
        revised_at=_string(data, "revised_at"),
    )


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _string(data: Mapping[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data[key]
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _list(data: Mapping[str, object], key: str) -> list[object]:
    value = data[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    return value


def _mapping_list(data: Mapping[str, object], key: str) -> list[dict[str, object]]:
    values = _list(data, key)
    if not all(isinstance(item, dict) for item in values):
        raise TypeError(f"{key} must be a list of objects")
    return cast(list[dict[str, object]], values)


def _object_tuple(
    data: Mapping[str, object], key: str
) -> tuple[dict[str, object], ...]:
    return tuple(_mapping_list(data, key))


def _string_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = _list(data, key)
    if not all(isinstance(item, str) for item in values):
        raise TypeError(f"{key} must be a list of strings")
    return tuple(cast(list[str], values))


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _pending_io_error("inspect", path.stem) from error


def _read_bytes_no_follow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _pending_io_error(operation: str, proposal_id: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.ANALYSIS_REPORT_INVALID,
        f"Pending proposal {operation} failed",
        details={"proposal_id": proposal_id},
        suggested_action="Retry or recreate the pending proposal",
    )
