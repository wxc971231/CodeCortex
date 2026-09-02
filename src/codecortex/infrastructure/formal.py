"""Filesystem persistence for the canonical CodeCortex formal state."""

import json
import os
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from codecortex.application.ports import RecoveryResult
from codecortex.domain.cognition import (
    SCHEMA_VERSION,
    CognitiveGraph,
    EntityRefs,
    FormalState,
    HistoryEventRef,
    Manifest,
    SourceBaseline,
    ValidationIssueCode,
    ValidationResult,
    validate_formal_state,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.ids import IdPrefix, validate_id
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
    "targets",
    "removals",
}
_JOURNAL_TARGET_FIELDS = {"path", "existed_before"}
_JOURNAL_REMOVAL_FIELDS = {"path"}


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

        try:
            state = FormalState(
                manifest=_manifest_from_json(manifest_data),
                graph=_graph_from_json(graph_data),
                entity_refs=_entity_refs_from_json(refs_data),
                source_baseline=_source_baseline_from_json(baseline_data),
                history_events=self._load_history_events(),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._raise_corrupt("Formal state has an invalid data shape", cause=error)

        result = self._validate_state(state)
        if not result.valid:
            self._raise_validation(result.issues)
        self._validate_empty_views(state)
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
        if state.manifest.graph_revision != base_revision + 1:
            self._raise_corrupt(
                "Commit revision does not follow the on-disk graph revision"
            )

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
        for relative, payload in views.items():
            if not relative.startswith("views/"):
                self._raise_corrupt("Rendered view paths must stay under views/")
            payloads[relative] = payload
        for relative in payloads:
            _checked_relative(self._root, relative)
        removals = self._view_removals(set(views))

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
            "views": sorted(views),
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
        if current_revision == journal.base_graph_revision:
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
            or target_revision != base_revision + 1
        ):
            self._raise_corrupt("Transaction journal revisions are invalid")
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
            targets=tuple(journal_targets),
            removals=tuple(journal_removals),
        )

    def _manifest_revision(self) -> int:
        data = self._read_object(self._root / "manifest.json")
        revision = data.get("graph_revision")
        if type(revision) is not int:
            self._raise_corrupt("manifest.json graph_revision must be an integer")
        return revision

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
        return all((self._root / relative).is_file() for relative in _FORMAL_FILES) and all(
            (self._root / relative).is_dir() for relative in _FORMAL_DIRECTORIES
        ) and (self._root / "views/TREE.md").is_file()

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
            events.append(HistoryEventRef(event_id, event_type))
        return tuple(events)

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
