#!/usr/bin/env python
"""Run the real M1a fact-indexing and bounded-query acceptance gate.

This script never uses the small ``tests/fixtures/m1a_repo`` fixture as a
substitute.  It materializes one frozen real repository (100--500 managed
Python files) at a pinned commit, then measures deterministic fact indexing
and bounded query behaviour through the production service wiring, and writes
machine-readable artifacts under ``--artifact-dir``.

Usage:
    python scripts/run_m1a_acceptance.py --artifact-dir /tmp/codecortex-m1a-final
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from codecortex.application.fact_sync import FactSyncService
from codecortex.application.proposals import _typed_graph
from codecortex.application.query import QueryService
from codecortex.application.services import ApplicationServices
from codecortex.domain.facts import SourceConfig
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.infrastructure.python.discovery import discover_python_source_set
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views

DEFAULT_ORIGIN = "https://github.com/pytest-dev/pytest.git"
# pytest 8.4.2, peeled commit of the annotated tag. Frozen forever.
DEFAULT_COMMIT = "bfae4224fd554d3d7f2c277a4cc092b6ec6af3ae"
MIN_MANAGED_FILES = 100
MAX_MANAGED_FILES = 500
QUERY_LIMIT = 25
MAX_QUERY_PAGES = 500


@dataclass
class AcceptanceReport:
    """Machine-readable acceptance artifact."""

    origin: str
    commit: str
    checks: list[dict[str, object]] = field(default_factory=list)
    timings_seconds: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    peak_facts_db_bytes: int = 0
    cognitive_replica_bytes: int = 0
    result: str = "fail"

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {name}: {detail}")

    @property
    def ok(self) -> bool:
        return all(bool(check["passed"]) for check in self.checks)


def _run(arguments: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _materialize_repository(worktree: Path, origin: str, commit: str) -> None:
    if worktree.is_dir():
        try:
            head = _run(["git", "rev-parse", "HEAD"], cwd=worktree)
        except (subprocess.CalledProcessError, FileNotFoundError):
            head = ""
        if head == commit:
            print(f"Reusing frozen repository at {worktree} ({commit[:12]})")
            return
        shutil.rmtree(worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {origin} into {worktree} ...")
    _run(["git", "clone", "-q", origin, str(worktree)])
    _run(["git", "checkout", "-q", commit], cwd=worktree)


def _compose(root: Path) -> ApplicationServices:
    repository = Repository(root)
    formal_store = FormalStore(repository)
    lock = RepositoryLock(root)
    cache_directory = root / ".codecortex" / ".cache"
    fact_sync = FactSyncService(
        repository, formal_store=formal_store, repository_lock=lock
    )
    return ApplicationServices(
        repository=repository,
        formal_store=formal_store,
        repository_lock=lock,
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
        fact_sync=fact_sync,
        query_service=QueryService(
            formal_store=formal_store,
            facts=FactsDatabase(cache_directory / "facts.sqlite3"),
            replica=GraphReplica(cache_directory / "cognitive.sqlite3"),
            repository_lock=lock,
        ),
    )


def _facts_db_paths(root: Path) -> list[Path]:
    base = root / ".codecortex" / ".cache" / "facts.sqlite3"
    return [base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")]


def _facts_db_size(root: Path) -> int:
    return sum(path.stat().st_size for path in _facts_db_paths(root) if path.exists())


def _cache_fingerprint(root: Path) -> str:
    """Hash the committed fact cache content deterministically.

    Entity UIDs are cache-local identities (fresh ULIDs on every from-scratch
    build), so equivalence is defined over stable content: entity kind/address/
    fingerprint, relation type/source address/target coordinates/expression/
    resolution, and per-file digests.
    """
    facts = FactsDatabase(root / ".codecortex" / ".cache" / "facts.sqlite3")
    digest = hashlib.sha256()
    with facts.open_read() as connection:
        for query in (
            (
                "SELECT kind, address, fingerprint FROM entities "
                "ORDER BY address, kind"
            ),
            (
                "SELECT r.relation_type, e.address, r.target_module, "
                "r.target_address, r.raw_expression, r.resolution_status "
                "FROM relations r JOIN entities e ON e.uid = r.source_uid "
                "ORDER BY e.address, r.relation_type, r.relation_key"
            ),
            (
                "SELECT relative_path, content_digest FROM source_files "
                "ORDER BY relative_path"
            ),
        ):
            for row in connection.execute(query):
                digest.update("\x1f".join("" if v is None else str(v) for v in row).encode())
                digest.update(b"\x1e")
    return digest.hexdigest()


def _rebuild_replica(app: ApplicationServices) -> None:
    state = app.formal_store.load()
    replica = GraphReplica.create_new(
        Path(app.repository.root) / ".codecortex" / ".cache" / "cognitive.sqlite3"
    )
    # The replica consumes the typed domain graph; reuse the production
    # formal-to-typed converter rather than re-implementing it here.
    replica.rebuild(_typed_graph(state), state.graph.graph_revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--commit", default=DEFAULT_COMMIT)
    args = parser.parse_args(argv)

    artifact_dir = args.artifact_dir.resolve()
    worktree = artifact_dir / "repository"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report = AcceptanceReport(origin=args.origin, commit=args.commit)

    _materialize_repository(worktree, args.origin, args.commit)
    head = _run(["git", "rev-parse", "HEAD"], cwd=worktree)
    report.check(
        "frozen_commit",
        head == args.commit,
        f"HEAD is {head}",
    )

    app = _compose(worktree)
    app.initialize_repository()

    started = time.perf_counter()
    discovered = discover_python_source_set(
        Repository(worktree), SourceConfig()
    )
    report.timings_seconds["discovery"] = time.perf_counter() - started
    managed_files = sorted(source.relative_path for source in discovered.sources)
    report.counts["managed_python_files"] = len(managed_files)
    report.check(
        "managed_file_count",
        MIN_MANAGED_FILES <= len(managed_files) <= MAX_MANAGED_FILES,
        f"{len(managed_files)} managed Python files "
        f"(expected {MIN_MANAGED_FILES}..{MAX_MANAGED_FILES})",
    )

    started = time.perf_counter()
    full = app.synchronize_facts("full")
    report.timings_seconds["full_fact_sync"] = time.perf_counter() - started
    report.counts["full_sync_parsed_files"] = full.parsed_files
    report.counts["full_sync_diagnostics"] = len(full.diagnostics)
    report.peak_facts_db_bytes = max(report.peak_facts_db_bytes, _facts_db_size(worktree))
    report.check(
        "full_fact_sync",
        full.parsed_files == len(managed_files) and full.index_generation >= 1,
        f"parsed {full.parsed_files} files in "
        f"{report.timings_seconds['full_fact_sync']:.2f}s, "
        f"{len(full.diagnostics)} diagnostics",
    )

    fingerprint_before = _cache_fingerprint(worktree)

    started = time.perf_counter()
    noop = app.synchronize_facts("auto")
    report.timings_seconds["noop_incremental_sync"] = time.perf_counter() - started
    report.check(
        "noop_incremental_sync",
        noop.parsed_files == 0 and not noop.rebuilt,
        f"parsed {noop.parsed_files} files in "
        f"{report.timings_seconds['noop_incremental_sync']:.3f}s",
    )

    touched = managed_files[0]
    touched_path = worktree / touched
    original_bytes = touched_path.read_bytes()
    try:
        touched_path.write_bytes(original_bytes + b"\n# m1a acceptance touch\n")
        started = time.perf_counter()
        changed = app.synchronize_facts("auto")
        report.timings_seconds["single_file_sync"] = time.perf_counter() - started
        report.check(
            "single_file_incremental_sync",
            changed.parsed_files == 1 and changed.changed_files == 1,
            f"parsed {changed.parsed_files} files after touching {touched}",
        )
    finally:
        touched_path.write_bytes(original_bytes)
    started = time.perf_counter()
    restored = app.synchronize_facts("auto")
    report.timings_seconds["restore_sync"] = time.perf_counter() - started
    restore_fingerprint = _cache_fingerprint(worktree)
    report.check(
        "restore_incremental_sync",
        restored.parsed_files == 1 and restore_fingerprint == fingerprint_before,
        f"restoring the touched file re-parsed {restored.parsed_files} file(s); "
        f"cache fingerprint "
        f"{'returned to' if restore_fingerprint == fingerprint_before else 'DIFFERS from'} "
        f"the pre-touch value",
    )
    report.peak_facts_db_bytes = max(report.peak_facts_db_bytes, _facts_db_size(worktree))

    for path in _facts_db_paths(worktree):
        path.unlink(missing_ok=True)
    started = time.perf_counter()
    rebuilt = app.synchronize_facts("full")
    report.timings_seconds["rebuilt_full_sync"] = time.perf_counter() - started
    fingerprint_after = _cache_fingerprint(worktree)
    report.check(
        "full_rebuild_equivalence",
        rebuilt.parsed_files == len(managed_files)
        and fingerprint_after == fingerprint_before,
        f"rebuilt cache fingerprint matches ({fingerprint_after[:16]}...)",
    )
    report.peak_facts_db_bytes = max(report.peak_facts_db_bytes, _facts_db_size(worktree))

    totals = FactsDatabase(
        worktree / ".codecortex" / ".cache" / "facts.sqlite3"
    ).analysis_totals()
    report.counts["indexed_entities"] = totals.entity_count
    report.counts["indexed_diagnostics"] = totals.diagnostic_count
    with sqlite3.connect(
        f"{(worktree / '.codecortex' / '.cache' / 'facts.sqlite3').resolve().as_uri()}?mode=ro",
        uri=True,
    ) as connection:
        report.counts["indexed_relations"] = connection.execute(
            "SELECT COUNT(*) FROM relations"
        ).fetchone()[0]

    _rebuild_replica(app)
    replica_path = worktree / ".codecortex" / ".cache" / "cognitive.sqlite3"
    report.cognitive_replica_bytes = replica_path.stat().st_size

    facts_db = FactsDatabase(worktree / ".codecortex" / ".cache" / "facts.sqlite3")
    partitions = facts_db.analysis_partitions(None, None, 100).items
    scope_partition = max(
        partitions, key=lambda partition: partition.entity_count
    )
    query_scope = scope_partition.module_name
    report.counts["bounded_query_scope_entity_count"] = scope_partition.entity_count

    pages = 0
    entities_seen = 0
    cursor: str | None = None
    page_timings: list[float] = []
    page_sizes_ok = True
    while True:
        started = time.perf_counter()
        page = app.repository_facts(query_scope, cursor, QUERY_LIMIT)
        page_timings.append(time.perf_counter() - started)
        pages += 1
        entities_seen += len(page.entities)
        if len(page.entities) > QUERY_LIMIT:
            page_sizes_ok = False
        cursor = page.cursor if page.truncated else None
        if cursor is None:
            break
        if pages >= MAX_QUERY_PAGES:
            page_sizes_ok = False
            break
    report.timings_seconds["bounded_query_total"] = sum(page_timings)
    report.timings_seconds["bounded_query_max_page"] = max(page_timings, default=0.0)
    report.counts["bounded_query_pages"] = pages
    report.counts["bounded_query_entities"] = entities_seen
    report.check(
        "bounded_queries",
        page_sizes_ok and pages >= 1 and entities_seen > 0,
        f"scope {query_scope!r}: {pages} pages, {entities_seen} entities, "
        f"limit {QUERY_LIMIT} honored, "
        f"max page {report.timings_seconds['bounded_query_max_page'] * 1000:.1f}ms",
    )

    report.result = "pass" if report.ok else "fail"
    artifact = artifact_dir / "m1a_acceptance.json"
    artifact.write_text(
        json.dumps(asdict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\nResult: {report.result.upper()}")
    print(f"Artifact: {artifact}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
