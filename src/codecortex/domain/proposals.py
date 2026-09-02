"""Pure proposal, patch, revision, and approval invariants."""

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, validate_id

type JsonObject = dict[str, object]

SCHEMA_VERSION = 1
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RFC3339_UTC_PATTERN = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d+))?Z\Z"
)
_SEMANTIC_ID_PATTERN = re.compile(
    r"(?:responsibility|behavior|capability)\.[a-z0-9]+(?:-[a-z0-9]+)*\Z"
)
_NODE_KIND_PREFIX = {
    "responsibility": "responsibility.",
    "behavior": "behavior.",
    "capability": "capability.",
}


class ProposalStatus(StrEnum):
    """The explicit states in the cognitive proposal lifecycle."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    REVISED = "revised"
    STALE = "stale"


class PatchOperationKind(StrEnum):
    """The complete M0 patch operation set."""

    ADD_NODE = "add_node"
    UPDATE_NODE = "update_node"
    REMOVE_NODE = "remove_node"
    ADD_EDGE = "add_edge"
    UPDATE_EDGE = "update_edge"
    REMOVE_EDGE = "remove_edge"


@dataclass(frozen=True)
class PatchOperation:
    """One cognitive graph change addressed by a stable object ID."""

    kind: PatchOperationKind | str
    target_id: str
    value: JsonObject | None

    def __post_init__(self) -> None:
        try:
            kind = PatchOperationKind(self.kind)
        except (TypeError, ValueError) as error:
            raise _invalid_proposal("Unsupported patch operation kind") from error
        object.__setattr__(self, "kind", kind)

        is_node = kind.value.endswith("_node")
        is_remove = kind.value.startswith("remove_")
        if is_node:
            _validate_semantic_node_id(self.target_id)
        else:
            if not isinstance(self.target_id, str):
                raise CodeCortexError(
                    ErrorCode.INVALID_ID, "Edge ID has an invalid namespace"
                )
            validate_id(self.target_id, IdPrefix.EDGE)

        if is_remove:
            if self.value is not None:
                raise _invalid_proposal("Remove operations cannot contain a value")
            return
        if not isinstance(self.value, dict):
            raise _invalid_proposal("Add and update operations require an object value")
        if self.value.get("id") != self.target_id:
            raise _invalid_proposal("Patch value ID must match its target ID")
        if is_node:
            kind_value = self.value.get("kind")
            if (
                not isinstance(kind_value, str)
                or kind_value not in _NODE_KIND_PREFIX
                or not self.target_id.startswith(_NODE_KIND_PREFIX[kind_value])
            ):
                raise _invalid_proposal("Node value kind must match its stable ID")
        _require_json(self.value, "Patch value")

    def to_canonical_value(self) -> JsonObject:
        """Return the JSON value covered by the patch digest."""
        return {
            "kind": PatchOperationKind(self.kind).value,
            "target_id": self.target_id,
            "value": self.value,
        }


@dataclass(frozen=True)
class ProposalRevision:
    """An immutable record of one user-discussion-driven replacement."""

    revision_number: int
    reason: str
    previous_reason: str
    previous_patch_digest: str
    previous_operations: tuple[PatchOperation, ...]
    previous_analyzed_source_digest: str | None
    previous_source_preconditions: tuple[JsonObject, ...]
    previous_affected_nodes: tuple[str, ...]
    previous_evidence: tuple[JsonObject, ...]
    previous_uncertainties: tuple[object, ...]
    revised_at: str

    def __post_init__(self) -> None:
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise _invalid_proposal("Revision number must be a positive integer")
        _validate_reason(self.reason)
        _validate_reason(self.previous_reason)
        _validate_digest(self.previous_patch_digest, nullable=False)
        if not self.previous_operations:
            raise _invalid_proposal("A revision must preserve previous operations")
        _validate_digest(self.previous_analyzed_source_digest, nullable=True)
        _validate_source_preconditions(self.previous_source_preconditions)
        _validate_affected_nodes(self.previous_affected_nodes)
        _validate_json_objects(self.previous_evidence, "Revision evidence")
        _require_json(list(self.previous_uncertainties), "Revision uncertainties")
        _validate_rfc3339_utc(self.revised_at)


@dataclass(frozen=True)
class ApprovalRecord:
    """Structured representation of the user's explicit approval."""

    proposal_id: str
    patch_digest: str
    approved_by: str
    approved_at: str
    approval_summary: str


@dataclass(frozen=True)
class Proposal:
    """The single current pending cognitive patch and its revision history."""

    schema_version: int
    proposal_id: str
    status: ProposalStatus
    base_graph_revision: int
    analyzed_source_digest: str | None
    source_preconditions: tuple[JsonObject, ...]
    operations: tuple[PatchOperation, ...]
    affected_nodes: tuple[str, ...]
    reason: str
    evidence: tuple[JsonObject, ...]
    uncertainties: tuple[object, ...]
    revision_log: tuple[ProposalRevision, ...]
    patch_digest: str
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise CodeCortexError(
                ErrorCode.UNSUPPORTED_SCHEMA,
                f"Proposal schema version {self.schema_version} is unsupported",
            )
        validate_id(self.proposal_id, IdPrefix.PROPOSAL)
        if not isinstance(self.status, ProposalStatus):
            raise _invalid_proposal("Proposal status is invalid")
        if type(self.base_graph_revision) is not int or self.base_graph_revision < 0:
            raise _invalid_proposal(
                "Base graph revision must be a non-negative integer"
            )
        _validate_digest(self.analyzed_source_digest, nullable=True)
        _validate_source_preconditions(self.source_preconditions)
        if not self.operations:
            raise _invalid_proposal("Proposal operations cannot be empty")
        if not all(isinstance(item, PatchOperation) for item in self.operations):
            raise _invalid_proposal("Proposal operations must be patch operations")
        _validate_affected_nodes(self.affected_nodes)
        _validate_reason(self.reason)
        _validate_json_objects(self.evidence, "Proposal evidence")
        _require_json(list(self.uncertainties), "Proposal uncertainties")
        if not all(isinstance(item, ProposalRevision) for item in self.revision_log):
            raise _invalid_proposal("Proposal revision log is invalid")
        expected_revision_numbers = tuple(range(1, len(self.revision_log) + 1))
        if (
            tuple(item.revision_number for item in self.revision_log)
            != expected_revision_numbers
        ):
            raise _invalid_proposal("Proposal revision numbers must be contiguous")
        _validate_digest(self.patch_digest, nullable=False)
        if self.patch_digest != canonical_patch_digest(self.operations):
            raise _invalid_proposal(
                "Proposal patch digest does not match its operations"
            )
        _validate_rfc3339_utc(self.created_at)

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        base_graph_revision: int,
        analyzed_source_digest: str | None,
        source_preconditions: tuple[JsonObject, ...],
        operations: tuple[PatchOperation, ...],
        affected_nodes: tuple[str, ...],
        reason: str,
        evidence: tuple[JsonObject, ...],
        uncertainties: tuple[object, ...],
        created_at: str,
    ) -> Proposal:
        """Create a proposed patch bound to the current formal revision."""
        return cls(
            schema_version=SCHEMA_VERSION,
            proposal_id=proposal_id,
            status=ProposalStatus.PROPOSED,
            base_graph_revision=base_graph_revision,
            analyzed_source_digest=analyzed_source_digest,
            source_preconditions=source_preconditions,
            operations=operations,
            affected_nodes=affected_nodes,
            reason=reason,
            evidence=evidence,
            uncertainties=uncertainties,
            revision_log=(),
            patch_digest=canonical_patch_digest(operations),
            created_at=created_at,
        )

    def revise(
        self,
        operations: tuple[PatchOperation, ...],
        *,
        reason: str,
        revised_at: str | None = None,
        analyzed_source_digest: str | None = None,
        source_preconditions: tuple[JsonObject, ...] | None = None,
        affected_nodes: tuple[str, ...] | None = None,
        evidence: tuple[JsonObject, ...] | None = None,
        uncertainties: tuple[object, ...] | None = None,
    ) -> Proposal:
        """Replace current analysis while retaining the complete prior candidate state."""
        if self.status not in {
            ProposalStatus.DRAFT,
            ProposalStatus.PROPOSED,
            ProposalStatus.REVISED,
            ProposalStatus.STALE,
        }:
            raise CodeCortexError(
                ErrorCode.PROPOSAL_STALE,
                f"Proposal in {self.status.value} state cannot be revised",
            )
        _validate_reason(reason)
        effective_revised_at = revised_at or _utc_now_rfc3339()
        _validate_rfc3339_utc(effective_revised_at)
        revision = ProposalRevision(
            revision_number=len(self.revision_log) + 1,
            reason=reason,
            previous_reason=self.reason,
            previous_patch_digest=self.patch_digest,
            previous_operations=self.operations,
            previous_analyzed_source_digest=self.analyzed_source_digest,
            previous_source_preconditions=self.source_preconditions,
            previous_affected_nodes=self.affected_nodes,
            previous_evidence=self.evidence,
            previous_uncertainties=self.uncertainties,
            revised_at=effective_revised_at,
        )
        return replace(
            self,
            status=ProposalStatus.PROPOSED,
            analyzed_source_digest=(
                self.analyzed_source_digest
                if analyzed_source_digest is None
                else analyzed_source_digest
            ),
            source_preconditions=(
                self.source_preconditions
                if source_preconditions is None
                else source_preconditions
            ),
            operations=operations,
            affected_nodes=self.affected_nodes
            if affected_nodes is None
            else affected_nodes,
            reason=reason,
            evidence=self.evidence if evidence is None else evidence,
            uncertainties=self.uncertainties
            if uncertainties is None
            else uncertainties,
            revision_log=(*self.revision_log, revision),
            patch_digest=canonical_patch_digest(operations),
        )

    def verify_base_graph_revision(self, current_graph_revision: int) -> None:
        """Reject use against a formal graph the proposal did not analyze."""
        if current_graph_revision != self.base_graph_revision:
            raise CodeCortexError(
                ErrorCode.GRAPH_REVISION_CONFLICT,
                "Proposal base graph revision no longer matches current graph",
                details={
                    "base_graph_revision": self.base_graph_revision,
                    "current_graph_revision": current_graph_revision,
                },
                suggested_action="Revise or recreate the proposal",
            )

    def verify_approval(self, approval: ApprovalRecord) -> None:
        """Verify explicit approval is bound to this current proposal patch."""
        if self.status not in {ProposalStatus.PROPOSED, ProposalStatus.REVISED}:
            raise CodeCortexError(
                ErrorCode.APPROVAL_REQUIRED,
                "Proposal is not in a state that can receive approval",
            )
        if canonical_patch_digest(self.operations) != self.patch_digest:
            raise CodeCortexError(
                ErrorCode.APPROVAL_MISMATCH,
                "Proposal operations no longer match the reviewed patch digest",
            )
        if (
            approval.proposal_id != self.proposal_id
            or approval.patch_digest != self.patch_digest
        ):
            raise CodeCortexError(
                ErrorCode.APPROVAL_MISMATCH,
                "Approval does not match current proposal",
            )
        if approval.approved_by != "user":
            raise _approval_required("Explicit user approval is required")
        if not isinstance(approval.approval_summary, str):
            raise _approval_required("Approval summary must be text")
        if not approval.approval_summary.strip():
            raise _approval_required("Approval summary cannot be blank")
        if len(approval.approval_summary) > 500:
            raise _approval_required("Approval summary exceeds 500 Unicode code points")
        try:
            _validate_rfc3339_utc(approval.approved_at)
        except CodeCortexError as error:
            raise _approval_required("Approval time must be RFC 3339 UTC") from error


def canonical_patch_digest(operations: tuple[PatchOperation, ...]) -> str:
    """Hash an ordered patch using compact key-sorted UTF-8 JSON."""
    if not isinstance(operations, tuple) or not all(
        isinstance(operation, PatchOperation) for operation in operations
    ):
        raise _invalid_proposal("Patch operations must be a tuple of operations")
    payload = [operation.to_canonical_value() for operation in operations]
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _invalid_proposal("Patch operations are not canonical JSON") from error
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_semantic_node_id(value: object) -> None:
    if not isinstance(value, str) or _SEMANTIC_ID_PATTERN.fullmatch(value) is None:
        raise CodeCortexError(ErrorCode.INVALID_ID, "Node ID has an invalid namespace")


def _validate_affected_nodes(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple):
        raise _invalid_proposal("Affected nodes must be a tuple")
    for value in values:
        _validate_semantic_node_id(value)
    if len(set(values)) != len(values):
        raise _invalid_proposal("Affected node IDs must be unique")


def _validate_reason(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_proposal("Proposal reason cannot be blank")


def _validate_digest(value: object, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise _invalid_proposal("Digest must be lowercase SHA-256")


def _validate_source_preconditions(values: tuple[JsonObject, ...]) -> None:
    _validate_json_objects(values, "Source preconditions")


def _validate_json_objects(values: tuple[JsonObject, ...], label: str) -> None:
    if not isinstance(values, tuple) or not all(
        isinstance(item, dict) for item in values
    ):
        raise _invalid_proposal(f"{label} must be a tuple of objects")
    _require_json(list(values), label)


def _require_json(value: object, label: str) -> None:
    if not _is_json_value(value):
        raise _invalid_proposal(f"{label} must contain only JSON-native values")
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise _invalid_proposal(f"{label} must contain finite JSON values") from error


def _is_json_value(value: object) -> bool:
    if value is None or type(value) in {bool, str, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


def _validate_rfc3339_utc(value: object) -> None:
    if not isinstance(value, str):
        raise _invalid_proposal("Time must be RFC 3339 UTC text")
    match = _RFC3339_UTC_PATTERN.fullmatch(value)
    if match is None:
        raise _invalid_proposal("Time must use RFC 3339 UTC with a Z suffix")
    try:
        datetime.fromisoformat(f"{match.group('date')}+00:00")
    except ValueError as error:
        raise _invalid_proposal("Time contains an invalid calendar value") from error


def _invalid_proposal(message: str) -> CodeCortexError:
    return CodeCortexError(ErrorCode.ANALYSIS_REPORT_INVALID, message)


def _approval_required(message: str) -> CodeCortexError:
    return CodeCortexError(ErrorCode.APPROVAL_REQUIRED, message)


def _utc_now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
