"""Filesystem persistence for the canonical CodeCortex formal state."""

import json
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import NoReturn

from codecortex.application.ports import RecoveryResult
from codecortex.domain.cognition import (
    SCHEMA_VERSION,
    CognitiveGraph,
    EntityRefs,
    FormalState,
    HistoryEventRef,
    JsonObject,
    Manifest,
    SourceBaseline,
    ValidationIssueCode,
    ValidationResult,
    ViewManifest,
    validate_formal_state,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, validate_id
from codecortex.domain.proposals import PatchOperation, canonical_patch_digest
from codecortex.infrastructure.jsonio import canonical_json_bytes, write_json_atomic
from codecortex.infrastructure.repository import Repository

DEFAULT_CONFIG = b"""schema_version = 1

[source]
include = ["**/*.py"]
exclude = []
respect_gitignore = true
python_min = "3.9"
python_max = "3.14"

[query]
default_depth = 2
max_nodes = 40
max_entities = 80
max_evidence = 80

[cache]
lock_timeout_seconds = 10
"""

PROJECT_TEMPLATE = b"# Project Context\n\nDescribe project goals and constraints here.\n"
EMPTY_TREE_VIEW = b"# CodeCortex Cognitive Tree\n\nNo cognition has been initialized.\n"

_FORMAL_FILES = (
    "manifest.json",
    "config.toml",
    "graph.json",
    "entity_refs.json",
    "source_baseline.json",
    "PROJECT.md",
)
_VIEW_MANIFEST_FIELDS = {"schema_version", "graph_revision", "files"}
_VIEW_MANIFEST_FILE_FIELDS = {"relative_path", "content_digest"}
_FORMAL_DIRECTORIES = (
    "history",
    "history/events",
    "views",
    "views/responsibilities",
    "views/behaviors",
    "views/capabilities",
)
_MANIFEST_FIELDS = {
    "schema_version",
    "minimum_core_version",
    "graph_revision",
    "cognition_initialized",
    "digest_profile_version",
    "managed_source_set_version",
    "cognition_baseline",
}
_GRAPH_FIELDS = {
    "schema_version",
    "graph_revision",
    "nodes",
    "semantic_edges",
    "logical_flows",
    "implementation_mappings",
}
_ENTITY_REFS_FIELDS = {"schema_version", "graph_revision", "entities"}
_SOURCE_BASELINE_FIELDS = {
    "schema_version",
    "digest_profile_version",
    "managed_source_set_version",
    "repository_source_digest",
    "files",
}
_JSON_BOMS = (
    b"\xef\xbb\xbf",
    b"\xff\xfe\x00\x00",
    b"\x00\x00\xfe\xff",
    b"\xff\xfe",
    b"\xfe\xff",
)
_COMMIT_STAGE_ORDER = ("event", "graph", "entity_refs", "source_baseline", "views")
_JOURNAL_FIELDS = {
    "schema_version",
    "transaction_id",
    "event_id",
    "base_graph_revision",
    "target_graph_revision",
    "transaction_kind",
    "base_cognition_baseline",
    "target_cognition_baseline",
    "targets",
    "removals",
}
_JOURNAL_TARGET_FIELDS = {"path", "existed_before"}
_JOURNAL_REMOVAL_FIELDS = {"path"}
_APPLIED_EVENT_REQUIRED_FIELDS = {
    "schema_version",
    "event_id",
    "event_type",
    "proposal_id",
    "base_graph_revision",
    "graph_revision",
    "reason",
    "patch_digest",
    "affected_nodes",
    "proposal_snapshot",
    "approval",
    "change_set_summary",
    "applied_at",
}
_PROPOSAL_SNAPSHOT_REQUIRED_FIELDS = {
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
_CHANGE_SET_SUMMARY_REQUIRED_FIELDS = {
    "before_source_digest",
    "after_source_digest",
    "changed_files",
    "changed_entities",
    "affected_nodes",
    "scope_confidence",
    "unmapped_changes",
}
_OPERATION_FIELDS = {"kind", "target_id", "value"}
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEMANTIC_NODE_ID_PATTERN = re.compile(
    r"(?:responsibility|behavior|capability)\.[a-z0-9]+(?:-[a-z0-9]+)*\Z"
)


class _DuplicateJsonKeyError(ValueError):
    pass


@dataclass(frozen=True)
class _JournalTarget:
    path: str
    existed_before: bool


@dataclass(frozen=True)
class _Journal:
    transaction_id: str
    event_id: str
    base_graph_revision: int
    target_graph_revision: int
    transaction_kind: str
    base_cognition_baseline: str | None
    target_cognition_baseline: str | None
    targets: tuple[_JournalTarget, ...]
    removals: tuple[str, ...]


class FormalStore:
    """Load and initialize Git-portable formal files for one repository."""

    def __init__(
        self,
        repository: Repository,
        *,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._repository = repository
        self._root = repository.resolve_relative(".codecortex")
        self._crash_hook = crash_hook

    def initialize(self, state: FormalState) -> FormalState:
        """Create formal revision zero, or return an existing valid state unchanged."""
        existing = self._formal_presence()
        if existing:
            if not self._all_required_paths_exist():
                self._raise_corrupt("Formal state is only partially initialized")
            return self.load()

        result = self._validate_state(state)
        if not result.valid:
            self._raise_validation(result.issues)
        if state.manifest.graph_revision != 0:
            self._raise_corrupt("Initial formal state must be revision zero")

        self._create_directories()
        write_json_atomic(self._root / "manifest.json", _manifest_to_json(state.manifest))
        write_json_atomic(self._root / "graph.json", _graph_to_json(state.graph))
        write_json_atomic(
            self._root / "entity_refs.json", _entity_refs_to_json(state.entity_refs)
        )
        write_json_atomic(
            self._root / "source_baseline.json",
            _source_baseline_to_json(state.source_baseline),
        )
        _write_bytes_atomic(self._root / "config.toml", DEFAULT_CONFIG)
        _write_bytes_atomic(self._root / "PROJECT.md", PROJECT_TEMPLATE)
        _write_bytes_atomic(self._root / "views/TREE.md", EMPTY_TREE_VIEW)
        return self.load()

    def load(self) -> FormalState:
        """Load one complete, validated formal snapshot."""
        if not self._formal_presence():
            raise CodeCortexError(
                ErrorCode.NOT_INITIALIZED,
                "CodeCortex formal state is not initialized",
            )
        if not self._all_required_paths_exist():
            self._raise_corrupt("Formal state is only partially initialized")
        # Git does not preserve empty directories.  The directory names are
        # structural containers rather than formal payload, so a fresh clone
        # may recreate only these empty paths before loading the canonical
        # files they contain.
        self._create_directories()

        self._validate_config()
        self._read_text(self._root / "PROJECT.md")
        manifest_data = self._read_object(self._root / "manifest.json")
        self._check_schema(manifest_data, "manifest.json")
        self._check_fields(manifest_data, _MANIFEST_FIELDS, "manifest.json")
        graph_data = self._read_object(self._root / "graph.json")
        self._check_schema(graph_data, "graph.json")
        self._check_fields(graph_data, _GRAPH_FIELDS, "graph.json")
        refs_data = self._read_object(self._root / "entity_refs.json")
        self._check_schema(refs_data, "entity_refs.json")
        self._check_fields(refs_data, _ENTITY_REFS_FIELDS, "entity_refs.json")
        baseline_data = self._read_object(self._root / "source_baseline.json")
        self._check_schema(baseline_data, "source_baseline.json")
        self._check_fields(
            baseline_data, _SOURCE_BASELINE_FIELDS, "source_baseline.json"
        )
        view_manifest = self._load_view_manifest()

        try:
            state = FormalState(
                manifest=_manifest_from_json(manifest_data),
                graph=_graph_from_json(graph_data),
                entity_refs=_entity_refs_from_json(refs_data),
                source_baseline=_source_baseline_from_json(baseline_data),
                history_events=self._load_history_events(),
                view_manifest=view_manifest,
            )
        except (KeyError, TypeError, ValueError) as error:
            self._raise_corrupt("Formal state has an invalid data shape", cause=error)

        result = self._validate_state(state)
        if not result.valid:
            self._raise_validation(result.issues)
        self._validate_empty_views(state)
        self._validate_view_manifest(state)
        return state

    def formal_file_presence(self) -> dict[str, bool]:
        """Report required formal paths without exposing machine-local cache paths."""
        return {
            relative: (self._root / relative).is_file() for relative in _FORMAL_FILES
        }

    def read_history_event(self, event_id: str) -> dict[str, object]:
        """Read one immutable event from a fully validated formal snapshot."""
        validate_id(event_id, IdPrefix.EVENT)
        state = self.load()
        if event_id not in {event.event_id for event in state.history_events}:
            raise CodeCortexError(
                ErrorCode.ANALYSIS_REPORT_INVALID,
                f"History event does not exist: {event_id}",
                suggested_action="Use a History event ID returned by CodeCortex",
            )
        path = self._root / "history/events" / f"{event_id}.json"
        data = self._read_object(path)
        self._check_schema(data, path.relative_to(self._root).as_posix())
        if data.get("event_id") != event_id:
            self._raise_corrupt("History event identity does not match its filename")
        return data

    def commit(
        self,
        state: FormalState,
        event: Mapping[str, object],
        views: Mapping[str, bytes],
        *,
        baseline_advance: bool = False,
        preserve_views: bool = False,
    ) -> None:
        """Commit one validated formal revision as a journaled transaction.

        All targets are staged and fsynced, old-file backups and a journal
        are persisted, and the manifest is replaced last as the commit
        marker, so an interruption is recoverable to either the complete
        old or the complete new revision. History events are immutable:
        an event file that already exists is never overwritten.
        """
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise CodeCortexError(
                ErrorCode.INVALID_ID, "History event ID must be text"
            )
        validate_id(event_id, IdPrefix.EVENT)
        if event_id not in {ref.event_id for ref in state.history_events}:
            raise ValueError("Committed state does not reference its event")
        event_relative = f"history/events/{event_id}.json"
        if (self._root / event_relative).exists():
            raise CodeCortexError(
                ErrorCode.ANALYSIS_REPORT_INVALID,
                "History events are immutable and cannot be overwritten",
                details={"event_id": event_id},
            )
        base_revision = self._manifest_revision()
        expected_revision = base_revision if baseline_advance else base_revision + 1
        if state.manifest.graph_revision != expected_revision:
            self._raise_corrupt(
                "Commit revision does not follow the on-disk graph revision"
            )
        if baseline_advance and event.get("event_type") != "cognition_baseline_advanced":
            self._raise_corrupt("Baseline transaction has the wrong event type")

        payloads: dict[str, bytes] = {
            event_relative: canonical_json_bytes(dict(event)),
            "graph.json": canonical_json_bytes(_graph_to_json(state.graph)),
            "entity_refs.json": canonical_json_bytes(
                _entity_refs_to_json(state.entity_refs)
            ),
            "source_baseline.json": canonical_json_bytes(
                _source_baseline_to_json(state.source_baseline)
            ),
            "manifest.json": canonical_json_bytes(_manifest_to_json(state.manifest)),
        }
        if state.view_manifest is not None and not preserve_views:
            self._validate_rendered_view_manifest(state.view_manifest, views)
            payloads["view_manifest.json"] = canonical_json_bytes(
                _view_manifest_to_json(state.view_manifest)
            )
        for relative, payload in views.items():
            if not relative.startswith("views/"):
                self._raise_corrupt("Rendered view paths must stay under views/")
            payloads[relative] = payload
        for relative in payloads:
            _checked_relative(self._root, relative)
        removals = [] if preserve_views else self._view_removals(set(views))

        transaction_root = self._transactions_root()
        transaction_directory = transaction_root / event_id
        if transaction_directory.exists():
            self._raise_corrupt("A transaction with this event ID already exists")
        staged_root = transaction_directory / "staged"
        for relative, payload in payloads.items():
            _write_staged(staged_root / relative, payload)
        _fsync_tree(staged_root)
        _fsync_directory(transaction_directory)
        self._crash("staged")

        backup_root = transaction_directory / "backup"
        journal_targets = [
            {
                "path": relative,
                "existed_before": self._backup_existing(backup_root, relative),
            }
            for relative in payloads
        ]
        journal_removals = []
        for relative in removals:
            self._backup_existing(backup_root, relative)
            journal_removals.append({"path": relative})
        _fsync_tree(backup_root)
        journal = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": event_id,
            "event_id": event_id,
            "base_graph_revision": base_revision,
            "target_graph_revision": state.manifest.graph_revision,
            "transaction_kind": "baseline_advance" if baseline_advance else "graph_apply",
            "base_cognition_baseline": self._manifest_baseline(),
            "target_cognition_baseline": state.manifest.cognition_baseline,
            "targets": journal_targets,
            "removals": journal_removals,
        }
        write_json_atomic(transaction_directory / "journal.json", journal)
        _fsync_directory(transaction_directory)
        self._crash("journal")

        stage_targets: dict[str, list[str]] = {
            "event": [event_relative],
            "graph": ["graph.json"],
            "entity_refs": ["entity_refs.json"],
            "source_baseline": ["source_baseline.json"],
            "views": sorted(
                [
                    *views,
                    *(
                        ["view_manifest.json"]
                        if state.view_manifest is not None and not preserve_views
                        else []
                    ),
                ]
            ),
        }
        for stage in _COMMIT_STAGE_ORDER:
            for relative in stage_targets[stage]:
                os.replace(staged_root / relative, self._root / relative)
            if stage == "views":
                for relative in removals:
                    (self._root / relative).unlink(missing_ok=True)
            self._crash(stage)
        os.replace(staged_root / "manifest.json", self._root / "manifest.json")
        self._crash("manifest")

        self._fsync_formal_parents((*payloads, *removals))
        shutil.rmtree(transaction_directory)
        _fsync_directory(transaction_root)

    def commit_baseline_advance(
        self, state: FormalState, event: Mapping[str, object]
    ) -> None:
        """Commit a source-baseline-only transaction without replacing views."""
        self.commit(state, event, {}, baseline_advance=True, preserve_views=True)

    def recover(self) -> RecoveryResult:
        """Resolve interrupted formal transactions to a provable revision.

        A manifest still at the base revision rolls the transaction back
        from its backups; a manifest at the target revision means the
        commit marker landed and only cleanup remains. A journal-less
        staging directory never began replacements and is discarded after
        the formal state is proven intact. Anything that cannot be proven
        consistent raises FORMAL_STATE_CORRUPT instead of being repaired.
        """
        transaction_root = self._transactions_root()
        restored: list[str] = []
        completed: list[str] = []
        if transaction_root.is_dir():
            for directory in sorted(
                (path for path in transaction_root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            ):
                self._recover_transaction(directory, restored, completed)
        state = self.load()
        return RecoveryResult(
            visible_revision=state.manifest.graph_revision,
            is_internally_consistent=True,
            restored_transactions=tuple(restored),
            completed_transactions=tuple(completed),
        )

    def _recover_transaction(
        self,
        transaction_directory: Path,
        restored: list[str],
        completed: list[str],
    ) -> None:
        journal_path = transaction_directory / "journal.json"
        if not journal_path.is_file():
            self.load()
            shutil.rmtree(transaction_directory)
            _fsync_directory(self._transactions_root())
            completed.append(transaction_directory.name)
            return
        journal = self._read_journal(journal_path, transaction_directory.name)
        current_revision = self._manifest_revision()
        if journal.transaction_kind == "baseline_advance":
            current_baseline = self._manifest_baseline()
            if current_baseline == journal.base_cognition_baseline:
                self._rollback(transaction_directory, journal)
                restored.append(transaction_directory.name)
            elif current_baseline == journal.target_cognition_baseline:
                completed.append(transaction_directory.name)
            else:
                self._raise_corrupt("Interrupted baseline transaction has an unknown baseline")
        elif current_revision == journal.base_graph_revision:
            self._rollback(transaction_directory, journal)
            restored.append(transaction_directory.name)
        elif current_revision == journal.target_graph_revision:
            completed.append(transaction_directory.name)
        else:
            self._raise_corrupt(
                "Interrupted transaction matches neither the old "
                "nor the new graph revision"
            )
        shutil.rmtree(transaction_directory)
        _fsync_directory(self._transactions_root())

    def _rollback(self, transaction_directory: Path, journal: _Journal) -> None:
        backup_root = transaction_directory / "backup"
        for target in journal.targets:
            formal_path = _checked_relative(self._root, target.path)
            if target.existed_before:
                backup_path = _checked_relative(backup_root, target.path)
                if not backup_path.is_file():
                    self._raise_corrupt("Transaction backup is missing")
                _write_bytes_atomic(formal_path, backup_path.read_bytes())
            else:
                formal_path.unlink(missing_ok=True)
        for removal in journal.removals:
            backup_path = _checked_relative(backup_root, removal)
            if not backup_path.is_file():
                self._raise_corrupt("Transaction backup is missing")
            _write_bytes_atomic(
                _checked_relative(self._root, removal), backup_path.read_bytes()
            )
        self._fsync_formal_parents(
            [target.path for target in journal.targets] + list(journal.removals)
        )

    def _read_journal(self, path: Path, transaction_id: str) -> _Journal:
        try:
            data = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as error:
            self._raise_corrupt("Transaction journal is unreadable", cause=error)
        if not isinstance(data, dict) or set(data) != _JOURNAL_FIELDS:
            self._raise_corrupt("Transaction journal fields are invalid")
        if data["schema_version"] != SCHEMA_VERSION:
            self._raise_corrupt("Transaction journal schema version is unsupported")
        if data["transaction_id"] != transaction_id:
            self._raise_corrupt("Transaction journal does not match its directory")
        event_id = data["event_id"]
        if not isinstance(event_id, str):
            self._raise_corrupt("Transaction journal event ID must be text")
        base_revision = data["base_graph_revision"]
        target_revision = data["target_graph_revision"]
        if (
            type(base_revision) is not int
            or type(target_revision) is not int
            or base_revision < 0
            or target_revision < base_revision
        ):
            self._raise_corrupt("Transaction journal revisions are invalid")
        transaction_kind = data["transaction_kind"]
        base_baseline = data["base_cognition_baseline"]
        target_baseline = data["target_cognition_baseline"]
        if transaction_kind not in ("graph_apply", "baseline_advance"):
            self._raise_corrupt("Transaction journal kind is invalid")
        if transaction_kind == "graph_apply" and target_revision != base_revision + 1:
            self._raise_corrupt("Graph transaction revisions are invalid")
        if transaction_kind == "baseline_advance" and target_revision != base_revision:
            self._raise_corrupt("Baseline transaction revisions are invalid")
        if not all(value is None or _is_digest(value) for value in (base_baseline, target_baseline)):
            self._raise_corrupt("Transaction journal baseline digest is invalid")
        targets = data["targets"]
        removals = data["removals"]
        if not isinstance(targets, list) or not all(
            isinstance(item, dict) and set(item) == _JOURNAL_TARGET_FIELDS
            for item in targets
        ):
            self._raise_corrupt("Transaction journal targets are invalid")
        if not isinstance(removals, list) or not all(
            isinstance(item, dict) and set(item) == _JOURNAL_REMOVAL_FIELDS
            for item in removals
        ):
            self._raise_corrupt("Transaction journal removals are invalid")
        journal_targets = []
        for item in targets:
            _checked_relative(self._root, item["path"])
            if type(item["existed_before"]) is not bool:
                self._raise_corrupt("Transaction journal backup flags are invalid")
            journal_targets.append(
                _JournalTarget(item["path"], item["existed_before"])
            )
        journal_removals = []
        for item in removals:
            _checked_relative(self._root, item["path"])
            journal_removals.append(item["path"])
        return _Journal(
            transaction_id=transaction_id,
            event_id=event_id,
            base_graph_revision=base_revision,
            target_graph_revision=target_revision,
            transaction_kind=transaction_kind,
            base_cognition_baseline=base_baseline,
            target_cognition_baseline=target_baseline,
            targets=tuple(journal_targets),
            removals=tuple(journal_removals),
        )

    def _manifest_revision(self) -> int:
        data = self._read_object(self._root / "manifest.json")
        revision = data.get("graph_revision")
        if type(revision) is not int:
            self._raise_corrupt("manifest.json graph_revision must be an integer")
        return revision

    def _manifest_baseline(self) -> str | None:
        baseline = self._read_object(self._root / "manifest.json").get("cognition_baseline")
        if baseline is not None and (not isinstance(baseline, str) or not _is_digest(baseline)):
            self._raise_corrupt("manifest.json cognition_baseline is invalid")
        return baseline

    def _transactions_root(self) -> Path:
        return self._root / ".cache" / "transactions"

    def _view_removals(self, rendered: set[str]) -> list[str]:
        managed = {"views/TREE.md"}
        for directory in ("responsibilities", "behaviors", "capabilities"):
            view_directory = self._root / "views" / directory
            if view_directory.is_dir():
                managed.update(
                    f"views/{directory}/{path.name}"
                    for path in view_directory.glob("*.md")
                )
        return sorted(managed - rendered)

    def verify_legacy_views(self, expected_views: Mapping[str, bytes]) -> None:
        """Ensure an M0 state was not hand-edited before M1a takes ownership."""
        if (self._root / "view_manifest.json").exists():
            return
        self._verify_view_bytes(expected_views, "Legacy M0 views differ from graph")

    def _load_view_manifest(self) -> ViewManifest | None:
        path = self._root / "view_manifest.json"
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            self._raise_corrupt("View manifest must be a regular file")
        data = self._read_object(path)
        self._check_schema(data, "view_manifest.json")
        self._check_fields(data, _VIEW_MANIFEST_FIELDS, "view_manifest.json")
        try:
            return _view_manifest_from_json(data)
        except (KeyError, TypeError, ValueError) as error:
            self._raise_corrupt("View manifest has an invalid data shape", cause=error)

    def _validate_view_manifest(self, state: FormalState) -> None:
        manifest = state.view_manifest
        if manifest is None:
            return
        if manifest.graph_revision != state.graph.graph_revision:
            self._raise_corrupt("View manifest revision does not match graph revision")
        expected = {entry["relative_path"] for entry in manifest.files}
        if len(expected) != len(manifest.files):
            self._raise_corrupt("View manifest has duplicate paths")
        paths = tuple(str(entry["relative_path"]) for entry in manifest.files)
        if paths != tuple(sorted(paths)):
            self._raise_corrupt("View manifest paths are not sorted")
        self._verify_view_bytes(
            {
                str(entry["relative_path"]): b""
                for entry in manifest.files
            },
            "Managed view bytes do not match view manifest",
            expected_digests={
                str(entry["relative_path"]): str(entry["content_digest"])
                for entry in manifest.files
            },
        )

    def _validate_rendered_view_manifest(
        self, manifest: ViewManifest, views: Mapping[str, bytes]
    ) -> None:
        expected = {
            str(entry["relative_path"]): str(entry["content_digest"])
            for entry in manifest.files
        }
        actual = {relative: _view_digest(payload) for relative, payload in views.items()}
        if manifest.schema_version != SCHEMA_VERSION or expected != actual:
            self._raise_corrupt("Rendered views do not match the supplied view manifest")

    def _verify_view_bytes(
        self,
        expected_views: Mapping[str, bytes],
        message: str,
        *,
        expected_digests: Mapping[str, str] | None = None,
    ) -> None:
        actual_paths = set(self._managed_view_paths())
        expected_paths = set(expected_views)
        if actual_paths != expected_paths:
            self._raise_corrupt(message)
        for relative in sorted(expected_paths):
            path = self._root / relative
            if path.is_symlink() or not path.is_file():
                self._raise_corrupt(message)
            try:
                payload = path.read_bytes()
            except OSError as error:
                self._raise_corrupt(message, cause=error)
            expected_digest = (
                expected_digests[relative]
                if expected_digests is not None
                else _view_digest(expected_views[relative])
            )
            if _view_digest(payload) != expected_digest:
                self._raise_corrupt(message)

    def _managed_view_paths(self) -> tuple[str, ...]:
        paths = ["views/TREE.md"]
        for directory in ("responsibilities", "behaviors", "capabilities"):
            view_directory = self._root / "views" / directory
            if not view_directory.is_dir() or view_directory.is_symlink():
                self._raise_corrupt("Managed view directory is invalid")
            paths.extend(
                f"views/{directory}/{path.name}"
                for path in view_directory.iterdir()
                if path.suffix == ".md"
            )
            if any(path.is_symlink() or not path.is_file() for path in view_directory.iterdir()):
                self._raise_corrupt("Managed view directory contains an invalid entry")
        return tuple(sorted(paths))

    def _backup_existing(self, backup_root: Path, relative: str) -> bool:
        source = _checked_relative(self._root, relative)
        if not source.is_file():
            return False
        _write_staged(_checked_relative(backup_root, relative), source.read_bytes())
        return True

    def _fsync_formal_parents(self, relatives: Iterable[str]) -> None:
        directories = {self._root}
        for relative in relatives:
            directories.add(_checked_relative(self._root, relative).parent)
        for directory in sorted(directories):
            _fsync_directory(directory)

    def _crash(self, stage: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(stage)

    def _formal_presence(self) -> bool:
        if not self._root.exists():
            return False
        return any((self._root / relative).exists() for relative in (*_FORMAL_FILES, *_FORMAL_DIRECTORIES))

    def _all_required_paths_exist(self) -> bool:
        return all((self._root / relative).is_file() for relative in _FORMAL_FILES) and (
            self._root / "views/TREE.md"
        ).is_file()

    def _create_directories(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        for relative in _FORMAL_DIRECTORIES:
            (self._root / relative).mkdir(parents=True, exist_ok=True)

    def _read_object(self, path: Path) -> dict[str, object]:
        try:
            payload = path.read_bytes()
            if payload.startswith(_JSON_BOMS):
                raise UnicodeDecodeError(
                    "utf-8", payload, 0, min(len(payload), 4), "BOM is not permitted"
                )
            text = payload.decode("utf-8")
            value = json.loads(text, object_pairs_hook=_unique_json_object)
            if canonical_json_bytes(value) != payload:
                self._raise_corrupt(f"{path.name} is not canonical JSON")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            _DuplicateJsonKeyError,
            TypeError,
            ValueError,
        ) as error:
            self._raise_corrupt(f"Cannot read valid JSON from {path.name}", cause=error)
        if not isinstance(value, dict):
            self._raise_corrupt(f"{path.name} must contain a JSON object")
        return value

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self._raise_corrupt(f"Cannot read UTF-8 from {path.name}", cause=error)

    def _check_schema(self, data: Mapping[str, object], filename: str) -> None:
        schema_version = data.get("schema_version")
        if type(schema_version) is not int:
            self._raise_corrupt(f"{filename} schema_version must be an integer")
        if schema_version != SCHEMA_VERSION:
            raise CodeCortexError(
                ErrorCode.UNSUPPORTED_SCHEMA,
                f"{filename} schema version {schema_version} is unsupported",
                details={"path": filename, "schema_version": schema_version},
                suggested_action="Use a compatible CodeCortex Core version",
            )

    def _check_fields(
        self,
        data: Mapping[str, object],
        expected_fields: set[str],
        filename: str,
    ) -> None:
        if set(data) != expected_fields:
            self._raise_corrupt(
                f"{filename} has unknown or missing top-level fields"
            )

    def _validate_state(self, state: FormalState) -> ValidationResult:
        try:
            return validate_formal_state(state)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            self._raise_corrupt(
                "Formal state contains malformed nested values", cause=error
            )

    def _validate_config(self) -> None:
        path = self._root / "config.toml"
        try:
            config = tomllib.loads(self._read_text(path))
        except tomllib.TOMLDecodeError as error:
            self._raise_corrupt("config.toml is malformed", cause=error)
        allowed_top = {"schema_version", "source", "query", "cache"}
        if set(config) != allowed_top:
            self._raise_corrupt("config.toml has unsupported or missing fields")
        config_schema = config.get("schema_version")
        if type(config_schema) is not int:
            self._raise_corrupt("config.toml schema_version must be an integer")
        if config_schema != SCHEMA_VERSION:
            raise CodeCortexError(
                ErrorCode.UNSUPPORTED_SCHEMA,
                f"config.toml schema version {config_schema} is unsupported",
                details={"path": "config.toml", "schema_version": config_schema},
                suggested_action="Use a compatible CodeCortex Core version",
            )
        allowed_sections = {
            "source": {"include", "exclude", "respect_gitignore", "python_min", "python_max"},
            "query": {"default_depth", "max_nodes", "max_entities", "max_evidence"},
            "cache": {"lock_timeout_seconds"},
        }
        for section, allowed in allowed_sections.items():
            value = config.get(section)
            if not isinstance(value, dict) or set(value) != allowed:
                self._raise_corrupt(f"config.toml section {section} is invalid")
        source = config["source"]
        query = config["query"]
        cache = config["cache"]
        assert isinstance(source, dict)
        assert isinstance(query, dict)
        assert isinstance(cache, dict)
        for key in ("include", "exclude"):
            patterns = source[key]
            if not isinstance(patterns, list) or not all(
                isinstance(pattern, str) and _is_repository_pattern(pattern)
                for pattern in patterns
            ):
                self._raise_corrupt(f"config.toml source.{key} must be relative patterns")
        if type(source["respect_gitignore"]) is not bool:
            self._raise_corrupt("config.toml source.respect_gitignore must be boolean")
        for key in ("python_min", "python_max"):
            if not isinstance(source[key], str) or not source[key]:
                self._raise_corrupt(f"config.toml source.{key} must be a version string")
        for key in ("default_depth", "max_nodes", "max_entities", "max_evidence"):
            value = query[key]
            if type(value) is not int or value < 1:
                self._raise_corrupt(f"config.toml query.{key} must be a positive integer")
        timeout = cache["lock_timeout_seconds"]
        if type(timeout) not in (int, float) or timeout < 0:
            self._raise_corrupt(
                "config.toml cache.lock_timeout_seconds must be non-negative"
            )

    def _load_history_events(self) -> tuple[HistoryEventRef, ...]:
        event_directory = self._root / "history/events"
        events: list[HistoryEventRef] = []
        for path in sorted(event_directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix != ".json":
                self._raise_corrupt("History events directory contains an unsupported entry")
            data = self._read_object(path)
            self._check_schema(data, path.relative_to(self._root).as_posix())
            try:
                event_id = _string(data, "event_id")
                event_type = _string(data, "event_type")
            except (KeyError, TypeError) as error:
                self._raise_corrupt("History event identity or type is invalid", cause=error)
            if path.stem != event_id:
                self._raise_corrupt("History event filename does not match its event ID")
            self._validate_history_event(data)
            events.append(HistoryEventRef(event_id, event_type))
        return tuple(events)

    def _validate_history_event(self, event: Mapping[str, object]) -> None:
        if event.get("event_type") == "cognition_baseline_advanced":
            self._validate_baseline_advanced_event(event)
            return
        self._validate_applied_event(event)

    def _validate_baseline_advanced_event(self, event: Mapping[str, object]) -> None:
        required = {
            "schema_version", "event_id", "event_type", "graph_revision",
            "change_set_id", "before_source_digest", "after_source_digest", "reason",
            "decision_record", "approval", "change_set_summary", "applied_at",
        }
        if set(event) != required:
            self._raise_corrupt("Baseline advance event fields are invalid")
        if not _is_id(event.get("event_id"), IdPrefix.EVENT) or not _is_id(
            event.get("change_set_id"), IdPrefix.CHANGE_SET
        ):
            self._raise_corrupt("Baseline advance event IDs are invalid")
        graph_revision = event.get("graph_revision")
        if type(graph_revision) is not int or graph_revision < 0:
            self._raise_corrupt("Baseline advance event graph revision is invalid")
        if not _is_digest(event.get("before_source_digest")) or not _is_digest(event.get("after_source_digest")):
            self._raise_corrupt("Baseline advance event digests are invalid")
        reason = event.get("reason")
        if reason not in ("no_semantic_change", "user_accepted"):
            self._raise_corrupt("Baseline advance reason is invalid")
        decision = event.get("decision_record")
        if not isinstance(decision, Mapping) or decision.get("decided_by") not in ("analyzer", "main_codex") or not isinstance(decision.get("evidence_summary"), str) or not decision["evidence_summary"].strip() or not isinstance(decision.get("decided_at"), str) or not decision["decided_at"].endswith("Z"):
            self._raise_corrupt("Baseline advance decision record is invalid")
        approval = event.get("approval")
        if reason == "no_semantic_change" and approval is not None:
            self._raise_corrupt("Automatic baseline advance cannot carry approval")
        if reason == "user_accepted" and (
            not isinstance(approval, Mapping)
            or approval.get("change_set_id") != event.get("change_set_id")
            or approval.get("source_digest") != event.get("after_source_digest")
            or approval.get("approved_by") != "user"
            or not isinstance(approval.get("approved_at"), str)
            or not approval["approved_at"].endswith("Z")
            or not isinstance(approval.get("approval_summary"), str)
            or not approval["approval_summary"].strip()
        ):
            self._raise_corrupt("Baseline advance approval is invalid")
        summary = event.get("change_set_summary")
        if not isinstance(summary, Mapping) or _CHANGE_SET_SUMMARY_REQUIRED_FIELDS - set(summary) or summary.get("before_source_digest") != event.get("before_source_digest") or summary.get("after_source_digest") != event.get("after_source_digest"):
            self._raise_corrupt("Baseline advance ChangeSet summary is invalid")
        applied_at = event.get("applied_at")
        if not isinstance(applied_at, str) or not applied_at.endswith("Z"):
            self._raise_corrupt("Baseline advance timestamp is invalid")

    def _validate_applied_event(self, event: Mapping[str, object]) -> None:
        """Validate the M0 audit payload, not merely its filename and type.

        A graph record's provenance is meaningful only when the referenced
        event still contains the reviewed Proposal snapshot and approval that
        caused it. Later milestones may add fields, but cannot omit any M0
        audit field or break the cross-field identity and digest bindings.
        """
        missing = _APPLIED_EVENT_REQUIRED_FIELDS - set(event)
        if missing:
            self._raise_corrupt("History event is missing required audit fields")
        if event.get("event_type") != "cognitive_proposal_applied":
            self._raise_corrupt("History event type is unsupported")
        event_id = event.get("event_id")
        proposal_id = event.get("proposal_id")
        if not _is_id(event_id, IdPrefix.EVENT) or not _is_id(
            proposal_id, IdPrefix.PROPOSAL
        ):
            self._raise_corrupt("History event IDs use invalid namespaces")
        base_revision = event.get("base_graph_revision")
        graph_revision = event.get("graph_revision")
        if (
            type(base_revision) is not int
            or type(graph_revision) is not int
            or base_revision < 0
            or graph_revision != base_revision + 1
        ):
            self._raise_corrupt("History event revisions are invalid")
        reason = event.get("reason")
        patch_digest = event.get("patch_digest")
        if not isinstance(reason, str) or not reason.strip() or not _is_digest(
            patch_digest
        ):
            self._raise_corrupt("History event reason or patch digest is invalid")
        affected_nodes = event.get("affected_nodes")
        if (
            not isinstance(affected_nodes, list)
            or not all(
                isinstance(node_id, str)
                and _SEMANTIC_NODE_ID_PATTERN.fullmatch(node_id)
                for node_id in affected_nodes
            )
            or len(set(affected_nodes)) != len(affected_nodes)
        ):
            self._raise_corrupt("History event affected-node scope is invalid")
        snapshot = event.get("proposal_snapshot")
        if not isinstance(snapshot, Mapping) or (
            snapshot.get("proposal_id") != proposal_id
            or snapshot.get("base_graph_revision") != base_revision
            or snapshot.get("patch_digest") != patch_digest
            or snapshot.get("status") != "applied"
            or snapshot.get("reason") != reason
            or snapshot.get("affected_nodes") != affected_nodes
        ):
            self._raise_corrupt("History event proposal snapshot is inconsistent")
        self._validate_proposal_snapshot(snapshot, patch_digest)
        approval = event.get("approval")
        if not isinstance(approval, Mapping) or (
            approval.get("proposal_id") != proposal_id
            or approval.get("patch_digest") != patch_digest
            or approval.get("approved_by") != "user"
            or not isinstance(approval.get("approved_at"), str)
            or not approval["approved_at"].endswith("Z")
            or not isinstance(approval.get("approval_summary"), str)
            or not approval["approval_summary"].strip()
        ):
            self._raise_corrupt("History event approval is inconsistent")
        change_set_summary = event.get("change_set_summary")
        if not isinstance(change_set_summary, Mapping) or (
            _CHANGE_SET_SUMMARY_REQUIRED_FIELDS - set(change_set_summary)
        ):
            self._raise_corrupt("History event change-set summary is invalid")
        if change_set_summary.get("affected_nodes") != affected_nodes:
            self._raise_corrupt("History event change-set scope is inconsistent")
        for key in ("before_source_digest", "after_source_digest"):
            digest = change_set_summary.get(key)
            if digest is not None and not _is_digest(digest):
                self._raise_corrupt("History event change-set digest is invalid")
        if not all(
            isinstance(change_set_summary.get(key), list)
            for key in ("changed_files", "changed_entities", "unmapped_changes")
        ) or not isinstance(change_set_summary.get("scope_confidence"), str):
            self._raise_corrupt("History event change-set summary is invalid")
        applied_at = event.get("applied_at")
        if not isinstance(applied_at, str) or not applied_at.endswith("Z"):
            self._raise_corrupt("History event application time is invalid")

    def _validate_proposal_snapshot(
        self, snapshot: Mapping[str, object], patch_digest: object
    ) -> None:
        """Verify that the immutable snapshot still describes its approved patch."""
        if _PROPOSAL_SNAPSHOT_REQUIRED_FIELDS - set(snapshot):
            self._raise_corrupt("History event proposal snapshot is incomplete")
        created_at = snapshot.get("created_at")
        if (
            snapshot.get("schema_version") != SCHEMA_VERSION
            or not isinstance(created_at, str)
            or not created_at.endswith("Z")
        ):
            self._raise_corrupt("History event proposal snapshot is invalid")
        for key in (
            "source_preconditions",
            "operations",
            "affected_nodes",
            "evidence",
            "uncertainties",
            "revision_log",
        ):
            if not isinstance(snapshot.get(key), list):
                self._raise_corrupt("History event proposal snapshot is invalid")
        operations = snapshot["operations"]
        assert isinstance(operations, list)
        try:
            parsed_operations = tuple(
                PatchOperation(
                    _string(operation, "kind"),
                    _string(operation, "target_id"),
                    operation.get("value"),
                )
                for operation in operations
                if isinstance(operation, Mapping)
                and set(operation) == _OPERATION_FIELDS
            )
        except (CodeCortexError, KeyError, TypeError) as error:
            self._raise_corrupt("History event proposal operations are invalid", cause=error)
        if len(parsed_operations) != len(operations) or not parsed_operations:
            self._raise_corrupt("History event proposal operations are invalid")
        if canonical_patch_digest(parsed_operations) != patch_digest:
            self._raise_corrupt("History event proposal digest is inconsistent")

    def _validate_empty_views(self, state: FormalState) -> None:
        if state.graph.graph_revision != 0:
            return
        tree_path = self._root / "views/TREE.md"
        try:
            tree_bytes = tree_path.read_bytes()
        except OSError as error:
            self._raise_corrupt("Cannot read the root cognitive tree view", cause=error)
        if tree_bytes != EMPTY_TREE_VIEW:
            self._raise_corrupt("Revision-zero cognitive tree view is not canonical")
        for relative in ("views/responsibilities", "views/behaviors", "views/capabilities"):
            if any((self._root / relative).iterdir()):
                self._raise_corrupt("Revision-zero detail view directories must be empty")

    def _raise_validation(self, issues: tuple[object, ...]) -> NoReturn:
        unsupported = any(
            getattr(issue, "code", None) is ValidationIssueCode.UNSUPPORTED_SCHEMA
            for issue in issues
        )
        code = ErrorCode.UNSUPPORTED_SCHEMA if unsupported else ErrorCode.FORMAL_STATE_CORRUPT
        raise CodeCortexError(
            code,
            "Formal state validation failed",
            details={
                "issues": [
                    {
                        "code": str(getattr(issue, "code", "UNKNOWN")),
                        "location": str(getattr(issue, "location", "")),
                        "message": str(getattr(issue, "message", "")),
                    }
                    for issue in issues
                ]
            },
            suggested_action="Inspect formal files; do not overwrite them",
        )

    def _raise_corrupt(
        self, message: str, *, cause: BaseException | None = None
    ) -> NoReturn:
        error = CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            message,
            suggested_action="Inspect formal files; do not overwrite them",
        )
        if cause is None:
            raise error
        raise error from cause


def _manifest_to_json(manifest: Manifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "minimum_core_version": manifest.minimum_core_version,
        "graph_revision": manifest.graph_revision,
        "cognition_initialized": manifest.cognition_initialized,
        "digest_profile_version": manifest.digest_profile_version,
        "managed_source_set_version": manifest.managed_source_set_version,
        "cognition_baseline": manifest.cognition_baseline,
    }


def _manifest_from_json(data: Mapping[str, object]) -> Manifest:
    return Manifest(
        schema_version=_integer(data, "schema_version"),
        graph_revision=_integer(data, "graph_revision"),
        cognition_initialized=_boolean(data, "cognition_initialized"),
        cognition_baseline=_optional_string(data, "cognition_baseline"),
        minimum_core_version=_string(data, "minimum_core_version"),
        digest_profile_version=_integer(data, "digest_profile_version"),
        managed_source_set_version=_integer(data, "managed_source_set_version"),
    )


def _graph_to_json(graph: CognitiveGraph) -> dict[str, object]:
    return {
        "schema_version": graph.schema_version,
        "graph_revision": graph.graph_revision,
        "nodes": list(graph.nodes),
        "semantic_edges": list(graph.semantic_edges),
        "logical_flows": list(graph.logical_flows),
        "implementation_mappings": list(graph.implementation_mappings),
    }


def _graph_from_json(data: Mapping[str, object]) -> CognitiveGraph:
    return CognitiveGraph(
        schema_version=_integer(data, "schema_version"),
        graph_revision=_integer(data, "graph_revision"),
        nodes=_object_tuple(data, "nodes"),
        semantic_edges=_object_tuple(data, "semantic_edges"),
        logical_flows=_object_tuple(data, "logical_flows"),
        implementation_mappings=_object_tuple(data, "implementation_mappings"),
    )


def _entity_refs_to_json(refs: EntityRefs) -> dict[str, object]:
    return {
        "schema_version": refs.schema_version,
        "graph_revision": refs.graph_revision,
        "entities": list(refs.entities),
    }


def _entity_refs_from_json(data: Mapping[str, object]) -> EntityRefs:
    return EntityRefs(
        schema_version=_integer(data, "schema_version"),
        graph_revision=_integer(data, "graph_revision"),
        entities=_object_tuple(data, "entities"),
    )


def _source_baseline_to_json(baseline: SourceBaseline) -> dict[str, object]:
    return {
        "schema_version": baseline.schema_version,
        "digest_profile_version": baseline.digest_profile_version,
        "managed_source_set_version": baseline.managed_source_set_version,
        "repository_source_digest": baseline.repository_source_digest,
        "files": list(baseline.files),
    }


def _source_baseline_from_json(data: Mapping[str, object]) -> SourceBaseline:
    return SourceBaseline(
        schema_version=_integer(data, "schema_version"),
        digest_profile_version=_integer(data, "digest_profile_version"),
        managed_source_set_version=_integer(data, "managed_source_set_version"),
        repository_source_digest=_optional_string(data, "repository_source_digest"),
        files=_object_tuple(data, "files"),
    )


def view_manifest_for(
    graph_revision: int, views: Mapping[str, bytes]
) -> ViewManifest:
    """Create the canonical manifest that exactly covers rendered views."""
    if type(graph_revision) is not int or graph_revision < 0:
        raise ValueError("View manifest graph revision must be a non-negative integer")
    entries: list[JsonObject] = []
    for relative, payload in sorted(views.items()):
        entries.append(
            {
                "relative_path": relative,
                "content_digest": _view_digest(payload),
            }
        )
    files = tuple(entries)
    if not files or any(not relative.startswith("views/") for relative in views):
        raise ValueError("View manifest requires only managed view paths")
    return ViewManifest(SCHEMA_VERSION, graph_revision, files)


def _view_manifest_to_json(manifest: ViewManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "graph_revision": manifest.graph_revision,
        "files": list(manifest.files),
    }


def _view_manifest_from_json(data: Mapping[str, object]) -> ViewManifest:
    files = _object_tuple(data, "files")
    for entry in files:
        if set(entry) != _VIEW_MANIFEST_FILE_FIELDS:
            raise ValueError("View manifest file entry has unexpected fields")
        relative = entry.get("relative_path")
        digest = entry.get("content_digest")
        if (
            not isinstance(relative, str)
            or not relative.startswith("views/")
            or not _is_repository_pattern(relative)
            or not _is_digest(digest)
        ):
            raise ValueError("View manifest file entry is invalid")
    return ViewManifest(
        schema_version=_integer(data, "schema_version"),
        graph_revision=_integer(data, "graph_revision"),
        files=files,
    )


def _view_digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _boolean(data: Mapping[str, object], key: str) -> bool:
    value = data[key]
    if type(value) is not bool:
        raise TypeError(f"{key} must be a boolean")
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


def _object_tuple(data: Mapping[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{key} must be a list of objects")
    return tuple(value)


def _is_repository_pattern(value: str) -> bool:
    parts = value.split("/")
    return bool(value) and not value.startswith("/") and "\\" not in value and ".." not in parts


def _is_id(value: object, prefix: IdPrefix) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_id(value, prefix)
    except CodeCortexError:
        return False
    return True


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _checked_relative(root: Path, relative: object) -> Path:
    if not isinstance(relative, str):
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT, "Transaction path must be text"
        )
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            "Transaction path escapes the formal state root",
            details={"path": relative},
        )
    resolved = (root / candidate).resolve()
    if os.path.commonpath((str(root), str(resolved))) != str(root):
        raise CodeCortexError(
            ErrorCode.FORMAL_STATE_CORRUPT,
            "Transaction path escapes the formal state root",
            details={"path": relative},
        )
    return root / candidate


def _write_staged(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for current, directories, _files in os.walk(root, topdown=False):
        for name in directories:
            _fsync_directory(Path(current) / name)
    _fsync_directory(root)
