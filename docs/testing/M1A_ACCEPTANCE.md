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

Approval flow: `codex exec` runs with approval policy `never`, so the test
home auto-approves every CodeCortex MCP tool at the host level
(`auto_approve_codecortex_tools` in `tests/e2e/conftest.py`); Core's own
Proposal approval-record checks are unaffected. The flow is a real two-turn
approval: turn 1 builds the proposal and stops without applying; the harness
reads `proposal_id`/`patch_digest` from the pending-proposal record and turn 2
carries the user's explicit approval of that exact digest. If Core rejects an
apply (e.g. ANALYSIS_REPORT_INVALID), the child must discard the candidate,
run a fresh analyzer pass fixing the reported issues, and stop at a
replacement proposal; the harness approves the new digest, up to 3 rounds.
Exec timeout is 900s per turn.

| Date | Environment | Result | Notes |
|---|---|---|---|
| 2026-09-03 | This machine, no model access granted | skipped | `CODECORTEX_RUN_CODEX_E2E`/`CODECORTEX_E2E_COPY_AUTH` unset; skip verified as part of the gate. A live Codex run remains an opt-in manual step. |
| 2026-09-04 | This machine, codex CLI 0.152.1, real model access | **passed** (833s) | 2 turns, no retry rounds needed. Turn 1: overview → initialize → full fact sync → 20 `repository_facts` + 6 `resolve_entity_context` + 2 `analysis_scope` calls → read-only analyzer delegation → `create_cognitive_proposal_from_analysis` succeeded on the 4th attempt (Core report validation rejected 3 malformed candidates; child corrected and resubmitted). Turn 2: harness-approved exact digest → `apply_cognitive_proposal` completed with `graph_revision` 1 and zero cache warnings. Final formal state: 7 nodes (1 responsibility/2 behaviors/4 capabilities), 6 legal edges (2 contains, 1 uses, 3 depends_on), 8 mappings resolving to 9 formal entity refs, 26-file source baseline; symbol-level anchors verified against fixture sources. |

## Real 100–500-file repository fact-indexing gate

```bash
python scripts/run_m1a_acceptance.py --artifact-dir /tmp/codecortex-m1a-final
```

The script never substitutes the small fixture. It clones one frozen real
repository at a pinned commit, composes services through the real CLI
composition root (`_default_services`), initializes CodeCortex formal state,
measures deterministic fact indexing and bounded-query behaviour, and then
runs one analysis-backed apply through the production chain
(`begin_analysis` → `create_cognitive_proposal_from_analysis` → explicit
current-digest approval → transactional apply) with an entity mapping and an
evidence-less structural `contains` edge in the report, asserting zero cache
warnings and readable guarded queries at revision 1. Machine artifact:
`/tmp/codecortex-m1a-final2/m1a_acceptance.json` (regenerate with the command
above; the artifact directory also holds the cloned repository). Re-verified
2026-09-04 after the real-E2E round at
`/tmp/codecortex-m1a-final3/m1a_acceptance.json`: same frozen commit, 9/9 PASS
(full sync 23.16 s, bounded-query max page 1.3 ms, analysis-backed apply with
zero cache warnings).

| Field | Value |
|---|---|
| Origin | `https://github.com/pytest-dev/pytest.git` |
| Frozen commit | `bfae4224fd554d3d7f2c277a4cc092b6ec6af3ae` (tag `8.4.2`) |
| Managed Python files | 260 (required 100–500) |
| Indexed entities / relations | 6,692 / 39,447 |
| Diagnostics | 0 |
| Discovery | 0.009 s |
| Full fact sync | 23.41 s (260 files parsed) |
| No-op incremental sync | 0.110 s (0 files parsed) |
| Single-file change sync | 0.199 s (exactly 1 file parsed) |
| Restore sync | 0.202 s (exactly 1 file; cache fingerprint returns to pre-touch value) |
| Full rebuild from empty cache | 22.92 s, cache fingerprint identical to incremental-built cache |
| Bounded queries (revision 0) | scope `src._pytest.capture`, 6 pages, 135 entities, page limit 25 honored, max page 1.2 ms |
| Analysis-backed apply | 0.65 s, revision 1, zero cache warnings |
| Guarded reads after apply | `repository_facts` 5 entities at revision 1; `search_cognitive_graph("Capture output")` 2 hits including `behavior.capture-output` |
| Peak facts DB size | 42,722,136 bytes (≈40.7 MiB, includes post-apply baseline snapshots) |
| Cognitive replica size | 180,224 bytes (revision-1 graph with mapping) |
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
- Approved graph/source baseline/history commit atomically, with the cognitive replica refreshed through the production composition root: verified by the acceptance apply phase above (zero cache warnings) and by `tests/integration/test_default_services_chain.py`, which drives `_default_services` end to end (apply with entity mapping, and an evidence-less `contains` edge, both followed by readable guarded queries).
- Deterministic views and cache deletion preserving formal cognition: covered by the Task 11 view/inspection suites; the acceptance script additionally deletes the whole facts cache mid-run and rebuilds it to an identical fingerprint.
