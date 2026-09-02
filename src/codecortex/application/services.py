"""Application use cases coordinating formal state and repository locking."""

from dataclasses import dataclass

from codecortex.application.ports import (
    FormalStorePort,
    RepositoryContextPort,
    RepositoryLockPort,
)
from codecortex.domain.cognition import (
    FormalState,
    Manifest,
    SourceBaseline,
    ValidationResult,
)


@dataclass(frozen=True)
class RepositoryOverview:
    """Bounded adapter-facing summary of one formal repository."""

    repository_root: str
    graph_revision: int
    cognition_initialized: bool
    cognition_baseline_source_digest: str | None
    formal_files: dict[str, bool]


@dataclass
class ApplicationServices:
    """Coordinate use cases while keeping policy out of CLI and MCP adapters."""

    repository: RepositoryContextPort
    formal_store: FormalStorePort
    repository_lock: RepositoryLockPort
    lock_timeout_seconds: float = 10

    def initialize_repository(self) -> RepositoryOverview:
        """Idempotently establish the revision-zero technical skeleton."""
        manifest = Manifest(
            schema_version=1,
            graph_revision=0,
            cognition_initialized=False,
            cognition_baseline=None,
        )
        baseline = SourceBaseline.empty()
        with self.repository_lock.acquire("exclusive", self.lock_timeout_seconds):
            state = self.formal_store.initialize(
                FormalState.empty(manifest=manifest, source_baseline=baseline)
            )
            return self._overview(state)

    def validate_graph(self) -> ValidationResult:
        """Load and validate the complete formal state under a shared lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            self.formal_store.load()
        return ValidationResult(valid=True, issues=())

    def repository_overview(self) -> RepositoryOverview:
        """Return the current formal-state summary under a shared lock."""
        with self.repository_lock.acquire("shared", self.lock_timeout_seconds):
            state = self.formal_store.load()
            return self._overview(state)

    def _overview(self, state: FormalState) -> RepositoryOverview:
        return RepositoryOverview(
            repository_root=self.repository.root.as_posix(),
            graph_revision=state.manifest.graph_revision,
            cognition_initialized=state.manifest.cognition_initialized,
            cognition_baseline_source_digest=state.manifest.cognition_baseline,
            formal_files=self.formal_store.formal_file_presence(),
        )
