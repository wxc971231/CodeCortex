"""Integration coverage for the explicit M1b discussion preparation entry."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from codecortex.application.discussion import InvocationState, QuestionScope
from codecortex.application.fact_sync import FactSyncService
from codecortex.application.preflight import PreflightService
from codecortex.application.services import ApplicationServices
from codecortex.domain.cognition import (
    CognitiveGraph,
    EntityRefs,
    FormalState,
    Manifest,
    SourceBaseline,
)
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.persistence.graph_replica import GraphHit
from codecortex.infrastructure.repository import Repository


def _write(root: Path, relative_path: str, text: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _services(tmp_path: Path) -> tuple[ApplicationServices, Path]:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    repository = Repository(root)
    _write(root, "app.py", "def answer() -> int:\n    return 1\n")
    lock = RepositoryLock(root)
    formal_store = FormalStore(repository)
    seed_sync = FactSyncService(repository, repository_lock=lock)
    seed = seed_sync.sync()
    baseline = SourceBaseline(
        1,
        1,
        1,
        seed.repository_source_digest,
        tuple(
            {"relative_path": path, "content_digest": digest}
            for path, digest in seed_sync.database.source_file_digests().items()
        ),
    )
    formal_store.initialize(
        FormalState(
            manifest=Manifest(1, 0, True, seed.repository_source_digest),
            graph=CognitiveGraph.empty(),
            entity_refs=EntityRefs.empty(),
            source_baseline=baseline,
        )
    )
    sync = FactSyncService(repository, formal_store=formal_store, repository_lock=lock)
    sync.database.replace_baseline_entity_snapshots(seed.repository_source_digest)
    preflight = PreflightService(
        formal_store=formal_store,
        fact_sync=sync,
        facts=sync.database,
        freshness_store=FreshnessStore(root / ".codecortex" / ".cache"),
        repository_lock=lock,
    )
    return (
        ApplicationServices(
            repository=repository,
            formal_store=formal_store,
            repository_lock=lock,
            fact_sync=sync,
            preflight_service=preflight,
        ),
        root,
    )


def _hit(node_id: str) -> GraphHit:
    return GraphHit(node_id, "behavior", node_id, 10, ("title",))


def test_discussion_entry_preflights_without_formal_graph_mutation(tmp_path: Path) -> None:
    services, root = _services(tmp_path)
    formal_paths = ("manifest.json", "graph.json", "entity_refs.json", "source_baseline.json")
    before = {
        path: (root / ".codecortex" / path).read_bytes() for path in formal_paths
    }

    with patch.object(services, "run_preflight", wraps=services.run_preflight) as run:
        plan = services.plan_discussion(
            QuestionScope("How does deployment work?", ("behavior.deploy",)),
            (_hit("behavior.deploy"),),
            InvocationState(),
        )

    run.assert_called_once_with()
    assert plan.route == "graph_current"
    assert plan.context_request.depth == 2
    assert {
        path: (root / ".codecortex" / path).read_bytes() for path in formal_paths
    } == before


def test_discussion_entry_uses_source_first_for_pending_unknown_scope(tmp_path: Path) -> None:
    services, root = _services(tmp_path)
    _write(root, "app.py", "def answer() -> int:\n    return 2\n")

    plan = services.plan_discussion(
        QuestionScope("How does training work?", ("behavior.train",)),
        (_hit("behavior.train"),),
        InvocationState(),
    )

    assert plan.route == "source_first"
    assert plan.requires_current_facts is True
    assert plan.requires_current_source is True
    assert plan.graph_is_baseline_navigation is True
