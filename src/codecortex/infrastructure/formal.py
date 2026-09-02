"""Filesystem persistence for the canonical CodeCortex formal state."""

import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from codecortex.domain.cognition import (
    SCHEMA_VERSION,
    CognitiveGraph,
    EntityRefs,
    FormalState,
    HistoryEventRef,
    Manifest,
    SourceBaseline,
    ValidationIssueCode,
    validate_formal_state,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.jsonio import write_json_atomic
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


class FormalStore:
    """Load and initialize Git-portable formal files for one repository."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository
        self._root = repository.resolve_relative(".codecortex")

    def initialize(self, state: FormalState) -> FormalState:
        """Create formal revision zero, or return an existing valid state unchanged."""
        existing = self._formal_presence()
        if existing:
            if not self._all_required_paths_exist():
                self._raise_corrupt("Formal state is only partially initialized")
            return self.load()

        result = validate_formal_state(state)
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
        graph_data = self._read_object(self._root / "graph.json")
        self._check_schema(graph_data, "graph.json")
        refs_data = self._read_object(self._root / "entity_refs.json")
        self._check_schema(refs_data, "entity_refs.json")
        baseline_data = self._read_object(self._root / "source_baseline.json")
        self._check_schema(baseline_data, "source_baseline.json")

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

        result = validate_formal_state(state)
        if not result.valid:
            self._raise_validation(result.issues)
        self._validate_empty_views(state)
        return state

    def formal_file_presence(self) -> dict[str, bool]:
        """Report required formal paths without exposing machine-local cache paths."""
        return {
            relative: (self._root / relative).is_file() for relative in _FORMAL_FILES
        }

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

    def _read_object(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
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
        "entity_refs": list(refs.entity_refs),
    }


def _entity_refs_from_json(data: Mapping[str, object]) -> EntityRefs:
    return EntityRefs(
        schema_version=_integer(data, "schema_version"),
        graph_revision=_integer(data, "graph_revision"),
        entity_refs=_object_tuple(data, "entity_refs"),
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
