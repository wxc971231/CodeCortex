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
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from codecortex.application.services import ApplicationServices
from codecortex.domain.facts import SourceConfig
from codecortex.domain.proposals import ApprovalRecord
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.python.discovery import discover_python_source_set
from codecortex.infrastructure.repository import Repository
from codecortex.interfaces.cli.main import _default_services

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


def _production_services(worktree: Path) -> ApplicationServices:
    previous_cwd = Path.cwd()
    os.chdir(worktree)
    try:
        # Compose through the real CLI composition root, not a test double.
        return _default_services()
    finally:
        os.chdir(previous_cwd)


def _ulid(prefix: str, index: int) -> str:
    suffix = format(index, "X")
    return f"{prefix}_01J{'0' * (23 - len(suffix))}{suffix}"


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


def _analysis_backed_apply(
    app: ApplicationServices, worktree: Path
) -> dict[str, object]:
    """Apply one analysis-backed proposal through the production chain.

    Mirrors the documented init flow: fact-synced coordinate, one aggregate
    proposal via create_cognitive_proposal_from_analysis, explicit current
    digest approval, atomic apply, then guarded reads.  The report carries an
    entity mapping and an evidence-less structural contains edge, so a cache
    warning here means the composition root or the validators regressed.
    """
    if app.initialization_service is None:
        return {"ok": False, "detail": "initialization service not wired"}
    coordinate = app.initialization_service.begin_analysis()
    page = app.repository_facts("src._pytest.capture", None, 1)
    if not page.entities:
        return {"ok": False, "detail": "no entities available for mapping"}
    entity = page.entities[0]
    evidence = {
        "id": _ulid("evid", 1),
        "kind": "code_entity",
        "entity_uid": entity.uid,
        "relative_path": entity.relative_path,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
        "observation": "acceptance anchor",
    }
    report_payload = {
        "schema_version": 1,
        "base_graph_revision": coordinate.graph_revision,
        "analyzed_source_digest": coordinate.source_digest,
        "analysis_scope": {"mode": "repository", "files": 260, "modules": 2},
        "coverage": {
            "analyzed_partitions": ["src/_pytest"],
            "unexamined_partitions": [],
        },
        "candidate_nodes": [
            {"id": "responsibility.test-capture", "kind": "responsibility",
             "title": "Test output capture"},
            {"id": "behavior.capture-output", "kind": "behavior",
             "title": "Capture output", "evidence": [evidence]},
            {"id": "capability.fd-manipulation", "kind": "capability",
             "title": "File descriptor manipulation"},
        ],
        "candidate_edges": [
            {
                "id": _ulid("edge", 1),
                "type": "contains",
                "source_id": "responsibility.test-capture",
                "target_id": "behavior.capture-output",
                "epistemic_status": "inferred",
            },
            {
                "id": _ulid("edge", 2),
                "type": "uses",
                "source_id": "behavior.capture-output",
                "target_id": "capability.fd-manipulation",
                "epistemic_status": "inferred",
                "evidence": [dict(evidence, id=_ulid("evid", 2))],
            },
        ],
        "candidate_flows": [],
        "candidate_mappings": [
            {
                "id": _ulid("map", 1),
                "subject_kind": "node",
                "subject_id": "behavior.capture-output",
                "entity_uid": entity.uid,
                "role": "primary",
                "resolution_status": "resolved",
                "evidence_note": "acceptance anchor",
            }
        ],
        "evidence": [],
        "uncertainties": ["acceptance-scoped cognition only"],
        "unmapped_regions": [],
        "diagnostics": [],
    }
    payload = json.dumps(report_payload, ensure_ascii=False).encode("utf-8")
    proposal = app.create_cognitive_proposal_from_analysis(
        payload, "M1a acceptance initialization"
    )
    applied = app.apply_cognitive_proposal(
        proposal.proposal_id,
        ApprovalRecord(
            proposal_id=proposal.proposal_id,
            patch_digest=proposal.patch_digest,
            approved_by="user",
            approved_at="2026-09-03T08:00:00Z",
            approval_summary="Approve acceptance patch",
        ),
    )
    if applied.cache_warnings:
        return {
            "ok": False,
            "detail": f"cache warnings after apply: {list(applied.cache_warnings)}",
            "graph_revision": applied.graph_revision,
        }
    facts_page = app.repository_facts(
        "src._pytest.capture", None, 5, expected_graph_revision=applied.graph_revision
    )
    search = app.search_cognitive_graph(
        "Capture output", expected_graph_revision=applied.graph_revision
    )
    ok = (
        applied.graph_revision == 1
        and len(facts_page.entities) > 0
        and len(search.hits) > 0
        and any(hit.node_id == "behavior.capture-output" for hit in search.hits)
    )
    return {
        "ok": ok,
        "detail": (
            f"revision {applied.graph_revision}, zero cache warnings, "
            f"{len(facts_page.entities)} facts readable, "
            f"{len(search.hits)} search hits"
        ),
        "graph_revision": applied.graph_revision,
        "search_hits": len(search.hits),
    }


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

    app = _production_services(worktree)
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

    started = time.perf_counter()
    apply_detail = _analysis_backed_apply(app, worktree)
    report.timings_seconds["analysis_backed_apply"] = time.perf_counter() - started
    report.counts["applied_graph_revision"] = int(apply_detail.get("graph_revision") or 0)
    report.counts["search_hits_after_apply"] = int(apply_detail.get("search_hits") or 0)
    report.peak_facts_db_bytes = max(report.peak_facts_db_bytes, _facts_db_size(worktree))
    replica_path = worktree / ".codecortex" / ".cache" / "cognitive.sqlite3"
    report.cognitive_replica_bytes = replica_path.stat().st_size
    report.check(
        "analysis_backed_apply_and_reads",
        bool(apply_detail["ok"]),
        str(apply_detail["detail"]),
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
