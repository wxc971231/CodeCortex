# M1b Acceptance

## Deterministic Task 11 harness gate

```bash
python -m pytest tests/e2e/test_codex_m1b.py -q
python scripts/run_codecortex_benchmark.py \
  --corpus tests/benchmark/questions.yaml \
  --repetitions 3 \
  --artifact-dir /tmp/codecortex-m1b-final
```

The second command is intentionally safe by default: it validates the frozen
corpus and writes `benchmark_report.json` with `status: "skipped"`.  It does
not start Codex, read browser auth, or consume a model quota.  A skipped report
is evidence that the guard worked; it is **not** a benchmark pass.

The deterministic harness verifies that every prepared Native/CodeCortex pair
uses distinct clean temporary repositories and Codex homes, while sharing one
temporary Git commit.  It also verifies that persisted traces redact machine
paths and credentials.

## Real Child Codex benchmark (manual, opt-in)

Run this only after explicitly authorizing model use:

```bash
python scripts/run_codecortex_benchmark.py \
  --corpus tests/benchmark/questions.yaml \
  --repetitions 3 \
  --artifact-dir /tmp/codecortex-m1b-final \
  --execute --copy-auth
```

The run creates fresh Child Codex processes without inheriting this development
conversation.  Native receives `--ignore-user-config`; CodeCortex receives an
isolated, test-only home and MCP configuration.  Both sides use the same
model, reasoning effort, sandbox, prompt, per-turn timeout, fixture source
tree, and temporary Git commit.  Browser auth is copied only to temporary
homes and those homes are deleted after each pair.

Artifacts retain only sanitized JSONL under `traces/` plus
`benchmark_report.json`: source/command access, MCP names, final answer,
usage and elapsed time.  Raw traces and normal `~/.codex` content are never
stored in the artifact directory.  The benchmark uses the frozen corpus and
scorer; graph-outside records the pre-frozen paired non-regression deltas.
Any deterministic pass still requires blinded human review for semantic
correctness, relevance, clarity and grounding.

## Approval/resume mode and VS Code smoke test

The automated ordinary cases are one-shot `codex exec --ephemeral --json`
processes.  The separate M1b approval/resume run remains a manual acceptance
step: use a temporary test home with host MCP approval only for the throwaway
run, first confirm that an unapproved Proposal leaves graph revision unchanged,
then resume the same session with the exact current proposal digest and verify
the resulting `cognitive_proposal_applied` event and user approval record.
Delete that test home afterward.  Product resources must remain
`approval_mode = "prompt"`.

The harness exposes that proof separately; it is still paid-model opt-in and
is not part of ordinary one-shot score results:

```bash
python scripts/run_codecortex_benchmark.py \
  --corpus tests/benchmark/questions.yaml \
  --repetitions 3 \
  --artifact-dir /tmp/codecortex-m1b-approval \
  --execute --copy-auth --approval-case unmaterialized-expand
```

Also perform one VS Code host-prompt smoke test using product installation:

1. In a disposable repository, run `codecortex install-codex` without test
   overrides and open it in VS Code Codex.
2. Ask `$codecortex` to prepare one safe Proposal.  Confirm it displays scope
   and digest but does not apply before explicit approval.
3. Approve the exact displayed digest in the second turn.  Confirm the VS Code
   host prompt appears, then allow it once and validate the emitted history
   event.
4. Record date, Codex version, result and any host-prompt mismatch below.

| Date | Environment | Benchmark | Blind review | VS Code host prompt | Notes |
|---|---|---|---|---|---|
| Not yet run | — | Not yet run (opt-in required) | Not yet run | Not yet run | Deterministic harness only; do not claim M1b completion. |

## Completion gate status

The repository must additionally pass the full M1b deterministic suite,
Ruff, Mypy, build, `codecortex validate --json`, the opt-in Child Codex
benchmark, blinded review, and the VS Code host-prompt smoke test before M1b
can be declared complete.
