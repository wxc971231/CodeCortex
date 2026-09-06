"""Machine-local deterministic persistence for pending cognitive proposals."""

import errno
import json
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
from codecortex.infrastructure.jsonio import canonical_json_bytes
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
_OPERATION_FIELDS_WITH_PRECONDITION = _OPERATION_FIELDS | {"expected_revision"}
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
_PENDING_DIRECTORY_PARTS = (".codecortex", ".cache", "pending_proposals")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)
_WRITE_OPEN_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_UNSAFE_PATH_ERRNOS = {errno.ELOOP, errno.ENOTDIR}


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
        filename = path.name
        try:
            with self._open_pending_directory(
                create=True, proposal_id=proposal.proposal_id
            ) as directory_fd:
                if _entry_mode(directory_fd, filename, proposal.proposal_id) is not None:
                    raise CodeCortexError(
                        ErrorCode.ANALYSIS_REPORT_INVALID,
                        "A pending proposal with this ID already exists",
                    )
                _write_json_atomic_at(
                    directory_fd,
                    filename,
                    _proposal_to_json(proposal),
                )
        except CodeCortexError:
            raise
        except OSError as error:
            raise _pending_io_error("create", proposal.proposal_id) from error

    def load(self, proposal_id: str) -> Proposal:
        """Load a canonical pending snapshot and restore domain invariants."""
        path = self._path(proposal_id)
        filename = path.name
        try:
            with self._open_pending_directory(
                create=False, proposal_id=proposal_id
            ) as directory_fd:
                mode = _entry_mode(directory_fd, filename, proposal_id)
                if mode is None or not stat.S_ISREG(mode):
                    raise _missing_pending_proposal(proposal_id)
                payload = _read_bytes_no_follow(directory_fd, filename)
        except FileNotFoundError as error:
            raise _missing_pending_proposal(proposal_id) from error
        except CodeCortexError:
            raise
        except OSError as error:
            raise CodeCortexError(
                ErrorCode.ANALYSIS_REPORT_INVALID,
                "Pending cognitive proposal is invalid",
                details={"proposal_id": proposal_id},
                suggested_action="Delete or recreate the pending proposal",
            ) from error
        try:
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
        filename = path.name
        try:
            with self._open_pending_directory(
                create=False, proposal_id=proposal.proposal_id
            ) as directory_fd:
                mode = _entry_mode(directory_fd, filename, proposal.proposal_id)
                if mode is None or not stat.S_ISREG(mode):
                    raise _missing_pending_proposal(proposal.proposal_id)
                _write_json_atomic_at(
                    directory_fd,
                    filename,
                    _proposal_to_json(proposal),
                )
        except FileNotFoundError as error:
            raise _missing_pending_proposal(proposal.proposal_id) from error
        except CodeCortexError:
            raise
        except OSError as error:
            raise _pending_io_error("replace", proposal.proposal_id) from error

    def delete(self, proposal_id: str) -> None:
        """Remove a consumed pending proposal without touching other cache state."""
        path = self._path(proposal_id)
        filename = path.name
        try:
            with self._open_pending_directory(
                create=False, proposal_id=proposal_id
            ) as directory_fd:
                mode = _entry_mode(directory_fd, filename, proposal_id)
                if mode is None:
                    return
                os.unlink(filename, dir_fd=directory_fd)
                os.fsync(directory_fd)
        except FileNotFoundError:
            return
        except CodeCortexError:
            raise
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

    @contextmanager
    def _open_pending_directory(
        self, *, create: bool, proposal_id: str
    ) -> Iterator[int]:
        descriptors: list[int] = []
        try:
            descriptors.append(os.open(self._repository.root, _DIRECTORY_OPEN_FLAGS))
            for part in _PENDING_DIRECTORY_PARTS:
                parent_fd = descriptors[-1]
                try:
                    child_fd = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    child_fd = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
                descriptors.append(child_fd)
            yield descriptors[-1]
        except OSError as error:
            if error.errno in _UNSAFE_PATH_ERRNOS:
                raise _unsafe_pending_path(proposal_id) from error
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


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
    operation_fields = frozenset(data)
    if operation_fields not in {
        frozenset(_OPERATION_FIELDS),
        frozenset(_OPERATION_FIELDS_WITH_PRECONDITION),
    }:
        raise ValueError("patch operation fields are invalid")
    value = data["value"]
    if value is not None and not isinstance(value, dict):
        raise TypeError("patch operation value must be an object or null")
    return PatchOperation(
        kind=_string(data, "kind"),
        target_id=_string(data, "target_id"),
        value=value,
        expected_revision=_expected_revision(data),
    )


def _expected_revision(data: Mapping[str, object]) -> int | str | None:
    value = data.get("expected_revision")
    if value is None or isinstance(value, str) or type(value) is int:
        return value
    raise TypeError("patch operation expected_revision is invalid")


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


def _entry_mode(directory_fd: int, filename: str, proposal_id: str) -> int | None:
    try:
        mode = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        raise _unsafe_pending_path(proposal_id)
    return mode


def _read_bytes_no_follow(directory_fd: int, filename: str) -> bytes:
    descriptor = os.open(filename, _READ_OPEN_FLAGS, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _write_json_atomic_at(
    directory_fd: int, filename: str, value: object
) -> None:
    payload = canonical_json_bytes(value)
    temporary_name: str | None = None
    temporary_fd: int | None = None
    for _ in range(10):
        candidate = f".{filename}.{secrets.token_hex(8)}.tmp"
        try:
            temporary_fd = os.open(
                candidate,
                _WRITE_OPEN_FLAGS,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        temporary_name = candidate
        break
    if temporary_name is None or temporary_fd is None:
        raise OSError(errno.EEXIST, "Could not allocate pending proposal temp file")

    installed = False
    try:
        with os.fdopen(temporary_fd, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        installed = True
        os.fsync(directory_fd)
    finally:
        if not installed:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _missing_pending_proposal(proposal_id: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PROPOSAL_STALE,
        "Pending cognitive proposal was not found",
        details={"proposal_id": proposal_id},
        suggested_action="Recreate the proposal",
    )


def _unsafe_pending_path(proposal_id: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.PATH_OUTSIDE_REPOSITORY,
        "Pending proposal path cannot contain symbolic links",
        details={"proposal_id": proposal_id},
    )


def _pending_io_error(operation: str, proposal_id: str) -> CodeCortexError:
    return CodeCortexError(
        ErrorCode.ANALYSIS_REPORT_INVALID,
        f"Pending proposal {operation} failed",
        details={"proposal_id": proposal_id},
        suggested_action="Retry or recreate the pending proposal",
    )
