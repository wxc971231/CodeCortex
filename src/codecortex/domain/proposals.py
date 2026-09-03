"""Pure proposal, patch, revision, and approval invariants."""

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, validate_id

type JsonObject = Mapping[str, object]

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


class FrozenJsonArray(tuple[object, ...]):
    """Marker type for an immutable snapshot of a JSON array."""


@dataclass(frozen=True, slots=True, eq=False)
class FrozenJsonObject(Mapping[str, object]):
    """Small immutable mapping used for domain-owned JSON snapshots."""

    _items: tuple[tuple[str, object], ...]

    def __getitem__(self, key: str) -> object:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(self.items()) == dict(other.items())

    def __repr__(self) -> str:
        return repr(dict(self._items))


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
    """The complete M1a patch operation set addressed by stable object IDs."""

    ADD_NODE = "add_node"
    UPDATE_NODE = "update_node"
    REMOVE_NODE = "remove_node"
    ADD_EDGE = "add_edge"
    UPDATE_EDGE = "update_edge"
    REMOVE_EDGE = "remove_edge"
    SET_LOGICAL_FLOW = "set_logical_flow"
    ADD_MAPPING = "add_mapping"
    UPDATE_MAPPING = "update_mapping"
    REMOVE_MAPPING = "remove_mapping"


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
        is_mapping = kind.value.endswith("_mapping")
        is_flow = kind is PatchOperationKind.SET_LOGICAL_FLOW
        is_remove = kind.value.startswith("remove_")
        if is_node or is_flow:
            _validate_semantic_node_id(self.target_id)
            if is_flow and not self.target_id.startswith("behavior."):
                raise _invalid_proposal(
                    "Logical flow operations must target a behavior node"
                )
        else:
            if not isinstance(self.target_id, str):
                raise CodeCortexError(
                    ErrorCode.INVALID_ID, "Target ID has an invalid namespace"
                )
            validate_id(
                self.target_id, IdPrefix.MAPPING if is_mapping else IdPrefix.EDGE
            )

        if is_remove:
            if self.value is not None:
                raise _invalid_proposal("Remove operations cannot contain a value")
            return
        if self.value is None and is_flow:
            # A null set_logical_flow value deletes the behavior's flow.
            return
        if not isinstance(self.value, Mapping):
            raise _invalid_proposal(
                "Add, update, and set operations require an object value"
            )
        identity_key = "behavior_id" if is_flow else "id"
        if self.value.get(identity_key) != self.target_id:
            raise _invalid_proposal("Patch value ID must match its target ID")
        if is_node:
            kind_value = self.value.get("kind")
            if (
                not isinstance(kind_value, str)
                or kind_value not in _NODE_KIND_PREFIX
                or not self.target_id.startswith(_NODE_KIND_PREFIX[kind_value])
            ):
                raise _invalid_proposal("Node value kind must match its stable ID")
        object.__setattr__(
            self,
            "value",
            _freeze_json_object(self.value, "Patch value"),
        )

    def to_canonical_value(self) -> dict[str, object]:
        """Return the JSON value covered by the patch digest."""
        return {
            "kind": PatchOperationKind(self.kind).value,
            "target_id": self.target_id,
            "value": json_value_to_mutable(self.value),
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
        if (
            canonical_patch_digest(self.previous_operations)
            != self.previous_patch_digest
        ):
            raise _invalid_proposal(
                "Revision previous patch digest does not match its operations"
            )
        _validate_digest(self.previous_analyzed_source_digest, nullable=True)
        object.__setattr__(
            self,
            "previous_source_preconditions",
            _freeze_json_objects(
                self.previous_source_preconditions, "Revision source preconditions"
            ),
        )
        _validate_affected_nodes(self.previous_affected_nodes)
        object.__setattr__(
            self,
            "previous_evidence",
            _freeze_json_objects(self.previous_evidence, "Revision evidence"),
        )
        object.__setattr__(
            self,
            "previous_uncertainties",
            _freeze_json_items(
                self.previous_uncertainties, "Revision uncertainties"
            ),
        )
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
        object.__setattr__(
            self,
            "source_preconditions",
            _freeze_json_objects(self.source_preconditions, "Source preconditions"),
        )
        if not self.operations:
            raise _invalid_proposal("Proposal operations cannot be empty")
        if not all(isinstance(item, PatchOperation) for item in self.operations):
            raise _invalid_proposal("Proposal operations must be patch operations")
        _validate_affected_nodes(self.affected_nodes)
        _validate_reason(self.reason)
        object.__setattr__(
            self,
            "evidence",
            _freeze_json_objects(self.evidence, "Proposal evidence"),
        )
        object.__setattr__(
            self,
            "uncertainties",
            _freeze_json_items(self.uncertainties, "Proposal uncertainties"),
        )
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
        new_patch_digest = canonical_patch_digest(operations)
        if new_patch_digest == self.patch_digest:
            raise _invalid_proposal(
                "A revision must change the current patch digest"
            )
        effective_revised_at = (
            _utc_now_rfc3339() if revised_at is None else revised_at
        )
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
            patch_digest=new_patch_digest,
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
        if self.status is not ProposalStatus.PROPOSED:
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


def _freeze_json_objects(
    values: tuple[JsonObject, ...], label: str
) -> tuple[FrozenJsonObject, ...]:
    if not isinstance(values, tuple) or not all(
        isinstance(item, Mapping) for item in values
    ):
        raise _invalid_proposal(f"{label} must be a tuple of objects")
    return tuple(_freeze_json_object(item, label) for item in values)


def _freeze_json_items(values: tuple[object, ...], label: str) -> tuple[object, ...]:
    if not isinstance(values, tuple):
        raise _invalid_proposal(f"{label} must be a tuple")
    return tuple(_freeze_json_value(value, label) for value in values)


def _freeze_json_object(value: Mapping[str, object], label: str) -> FrozenJsonObject:
    if not isinstance(value, Mapping):
        raise _invalid_proposal(f"{label} must contain only JSON-native values")
    items: list[tuple[str, object]] = []
    keys: set[str] = set()
    for key, item in value.items():
        if not isinstance(key, str):
            raise _invalid_proposal(f"{label} must use string object keys")
        if key in keys:
            raise _invalid_proposal(f"{label} must use unique object keys")
        keys.add(key)
        items.append((key, _freeze_json_value(item, label)))
    return FrozenJsonObject(tuple(items))


def _freeze_json_value(value: object, label: str) -> object:
    if isinstance(value, FrozenJsonObject):
        return _freeze_json_object(value, label)
    if isinstance(value, FrozenJsonArray):
        return FrozenJsonArray(_freeze_json_value(item, label) for item in value)
    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid_proposal(f"{label} must contain finite JSON numbers")
        return value
    if isinstance(value, list):
        return FrozenJsonArray(_freeze_json_value(item, label) for item in value)
    if isinstance(value, dict):
        return _freeze_json_object(value, label)
    raise _invalid_proposal(f"{label} must contain only JSON-native values")


def json_value_to_mutable(value: object) -> object:
    """Thaw a domain JSON snapshot only for a serialization boundary."""
    if isinstance(value, FrozenJsonObject):
        return {
            key: json_value_to_mutable(item) for key, item in value.items()
        }
    if isinstance(value, FrozenJsonArray):
        return [json_value_to_mutable(item) for item in value]
    return value


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
