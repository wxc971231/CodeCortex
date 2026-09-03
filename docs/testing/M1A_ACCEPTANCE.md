# M1a Acceptance

## Deterministic suite

```bash
python -m pytest tests/unit tests/integration -q   # 479 passed
python -m pytest tests/e2e/test_codex_m1a.py -q    # 1 passed, 1 skipped
python -m pytest -q                                 # full suite, see below
ruff check src tests scripts
mypy src
git diff --check
```

`tests/e2e/test_codex_m1a.py::test_m1a_fixture_oracle_is_human_authored_and_grounded`
is the no-model fixture gate: it pins the human-authored
`tests/fixtures/m1a_repo/oracle.json` (26 Python files, 2 responsibilities,
2 behaviors, 1 shared capability, flows, and source anchors for every
declared element) to the actual fixture sources, so fixture or oracle drift
fails before any Codex run.

## Real Child Codex E2E (opt-in only)

```bash
CODECORTEX_RUN_CODEX_E2E=1 CODECORTEX_E2E_COPY_AUTH=1 \
  python -m pytest tests/e2e/test_codex_m1a.py -m codex_e2e -q
```

The test materializes the 26-file semantic fixture into a temporary Git
repository plus a temporary Codex home, installs the Codex integration, and
requires the Child Codex to follow the M1a Skill (`sync_repository_facts` →
`analysis_scope` → read-only Analyzer → one
`create_cognitive_proposal_from_analysis` → explicit current-digest approval →
apply), asserting real formal state (`cognition_initialized`, revision ≥ 1,
grounded responsibility nodes). Without every opt-in condition the test is an
explicit pytest skip; a passing result is never fabricated.

| Date | Environment | Result | Notes |
|---|---|---|---|
| 2026-09-03 | This machine, no model access granted | skipped | `CODECORTEX_RUN_CODEX_E2E`/`CODECORTEX_E2E_COPY_AUTH` unset; skip verified as part of the gate. A live Codex run remains an opt-in manual step. |

## Real 100–500-file repository fact-indexing gate

```bash
python scripts/run_m1a_acceptance.py --artifact-dir /tmp/codecortex-m1a-final
```

The script never substitutes the small fixture. It clones one frozen real
repository at a pinned commit, initializes CodeCortex formal state, and
measures deterministic fact indexing and bounded-query behaviour through the
production service wiring. Machine artifact:
`/tmp/codecortex-m1a-final/m1a_acceptance.json` (regenerate with the command
above; the artifact directory also holds the cloned repository).

| Field | Value |
|---|---|
| Origin | `https://github.com/pytest-dev/pytest.git` |
| Frozen commit | `bfae4224fd554d3d7f2c277a4cc092b6ec6af3ae` (tag `8.4.2`) |
| Managed Python files | 260 (required 100–500) |
| Indexed entities / relations | 6,692 / 39,447 |
| Diagnostics | 0 |
| Discovery | 0.009 s |
| Full fact sync | 23.21 s (260 files parsed) |
| No-op incremental sync | 0.114 s (0 files parsed) |
| Single-file change sync | 0.200 s (exactly 1 file parsed) |
| Restore sync | 0.214 s (exactly 1 file; cache fingerprint returns to pre-touch value) |
| Full rebuild from empty cache | 23.25 s, cache fingerprint identical to incremental-built cache |
| Bounded queries | scope `src._pytest.capture`, 6 pages, 135 entities, page limit 25 honored, max page 1.2 ms |
| Peak facts DB size | 33,139,552 bytes (≈31.6 MiB) |
| Cognitive replica size | 4,096 bytes (empty revision-0 graph) |
| Acceptance result | **PASS** |

Equivalence note: entity UIDs are cache-local identities (fresh ULIDs on a
from-scratch build, preserved only across incremental re-parses), so the
equivalence fingerprint is defined over stable content — entity kind/address/
fingerprint, relation type/source address/target coordinates/resolution, and
per-file digests — not over ULIDs or row ids.

## M1a completion criteria status

- 100–500-file repository builds and queries facts without unbounded loads: verified above (all reads cursor-paginated with enforced limits).
- Incremental and full indexes equivalent: verified by content fingerprint above.
- Incoming relations re-resolve correctly on change/restore: verified by the single-file touch/restore checks.
- Analyzer report bounds/freshness and single aggregate Proposal: covered by `tests/integration/test_analysis_report_freshness.py` and the Main-only `create_cognitive_proposal_from_analysis` MCP tool (Analyzer profile provably lacks it; see `tests/unit/interfaces/test_mcp_profiles.py`).
- Approved graph/source baseline/history commit atomically: covered by `ProposalService.apply_cognitive_proposal` integration tests.
- Deterministic views and cache deletion preserving formal cognition: covered by the Task 11 view/inspection suites; the acceptance script additionally deletes the whole facts cache mid-run and rebuilds it to an identical fingerprint.
