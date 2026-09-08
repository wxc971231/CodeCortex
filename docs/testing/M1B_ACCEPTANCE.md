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
is evidence that the guard worked; it is **not** a benchmark pass, and the CLI
returns exit code 2 so automation cannot mistake it for acceptance.

The deterministic harness verifies that every prepared Native/CodeCortex pair
uses distinct clean temporary repositories and Codex homes, while sharing one
temporary Git commit.  It also verifies that persisted traces redact machine
paths and credentials.  Each prepared repository contains the pinned,
pre-approved revision-1 formal graph from `tests/fixtures/m1b_repo/formal_graph.json`;
the harness validates and applies that graph through the product Proposal path.
Scored routes and cognitive anchors come from matching MCP call/result records,
not text emitted by the Child process.

The deterministic suite also covers the M1a-to-M1b boundary: a fresh Main MCP
session can run overview → initialize → full Fact Sync → guarded analysis scope,
while initialized-only M1b reads still return `NOT_INITIALIZED`. Analyzer
composition and reads are checked against missing, corrupt, and prepared cache
states; they must never create, repair, delete, or otherwise mutate cache files.
Bootstrap authorization and the actual read share one repository lock, and the
suite interleaves two Main instances to prove a concurrent completed
initialization cannot retain the exception. Final live-source digest checks are
also race-tested after Fact Sync, Analyzer preparation, and cache recovery.

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
tree, and temporary Git commit. When `--copy-auth` is explicitly authorized,
browser auth is copied only to both temporary homes before either paired arm
starts. The entire per-pair temporary workspace (both Git copies, the seed, and
both homes) lives outside the artifact directory and is deleted after each pair,
including preparation failures. Cleanup is part of execution validity: a
removal failure produces a failed report and can never be accepted as a pass.

Artifacts retain only sanitized JSONL under `traces/` plus
`benchmark_report.json`: source/command access, MCP names, final answer,
usage and elapsed time.  Raw traces and normal `~/.codex` content are never
stored in the artifact directory.  The benchmark uses the frozen corpus and
scorer; graph-outside records the pre-frozen paired non-regression deltas.
Any deterministic pass still requires blinded human review for semantic
correctness, relevance, clarity and grounding.

A successful automated execution writes `execution_status: "completed"` but
remains `status: "pending_human_review"`.  Acceptance requires a separate JSON
review supplied with `--blind-review`; it must attest blinded review, cover the
exact sorted corpus case IDs, match the persisted corpus digest, and bind to
the exact completed paired-result and sanitized-trace artifact digest:

```json
{
  "schema_version": 2,
  "corpus_digest": "sha256:<digest from benchmark_report.json>",
  "run_digest": "sha256:<run_digest from benchmark_report.json>",
  "reviewed_case_ids": ["<every frozen case ID, sorted>"],
  "blinded": true,
  "decision": "accepted",
  "reviewer": "<independent reviewer>",
  "reviewed_at": "2026-09-05T08:00:00Z",
  "notes": "<review notes>"
}
```

Attach the review to the already completed artifact without `--execute`; this
does not start another Child run:

```bash
python scripts/run_codecortex_benchmark.py \
  --corpus tests/benchmark/questions.yaml \
  --repetitions 3 \
  --artifact-dir /tmp/codecortex-m1b-final \
  --blind-review path/to/review.json
```

The attachment step recomputes `run_digest` from the complete case/repetition
matrix, both paired arm result records, and the identities and bytes of every
sanitized trace. An older same-corpus review or any modified report/trace stays
pending and fails closed. A rejected review produces `status: "failed"`; only
a validated accepting review for these exact artifacts produces
`status: "accepted"`.

## Approval/resume mode and VS Code smoke test

The automated ordinary cases are one-shot `codex exec --ephemeral --json`
processes, except `same-topic-followup`: each arm first asks the frozen
checkpoint-persistence question in a persistent session, then resumes that
exact session with the frozen read-path follow-up. Both turns retain the same
repository, isolated home, model, effort, sandbox and per-turn timeout. Missing
session-start evidence, failed/incomplete first turns, or an empty first answer
fail closed without starting the second turn. Both sanitized turn streams are
retained in the arm's digest-bound trace artifact. Usage and latency include
both turns; final-answer quality, route and evidence are scored from the second
turn, while prohibited tools and state mutations cover the whole discussion.
The separate M1b approval/resume run remains a manual acceptance
step: use a temporary test home where the host can resolve the exact-digest
apply call noninteractively, then verify that it writes the resulting
`cognitive_proposal_applied` event and user approval record. Delete that test
home afterward. Product resources remain `approval_mode = "prompt"`; a real
VS Code session may show a native request or have Guardian resolve it, based
on the user's Codex approval setting.

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
2. Ask `$codecortex` to prepare one safe Proposal. Confirm it displays scope
   and digest, then invokes `apply_cognitive_proposal` with those exact values.
3. Confirm Codex resolves the native host approval, either through its request
   or the user's “approve for me”/Guardian review behavior, then validate the
   emitted history event. Do not claim a CodeCortex-specific approval button;
   no follow-up message repeating the ID or digest is required.
4. Record date, Codex version, result and any host-prompt mismatch below.

| Date | Environment | Benchmark | Blind review | VS Code host prompt | Notes |
|---|---|---|---|---|---|
| 2026-09-08 | VS Code product host, `approval_mode = prompt` | Not run (opt-in required) | Not run | Passed smoke: manually confirmed the native MCP card, including the create/apply flow; observed Codex “approve for me”/Guardian automatic review; checked the application Event and `codecortex validate --json` | The dedicated approval/resume benchmark remains not run. The Child Codex benchmark and blind review remain not run; this smoke record does not complete M1b. |

The 2026-09-08 smoke used the product installation and confirmed the native
MCP card through both proposal creation and application. The host approval was
observed through Codex “approve for me”/Guardian automatic review. The emitted
application Event and `codecortex validate --json` output were checked. This
manual smoke does not provide evidence for the separate approval/resume
benchmark, Child Codex benchmark, or blinded review; each remains explicitly
unexecuted.

## Completion gate status

The repository must additionally pass the full M1b deterministic suite,
Ruff, Mypy, build, `codecortex validate --json`, the opt-in Child Codex
benchmark, blinded review, the dedicated approval/resume benchmark, and the
VS Code host-prompt smoke test before M1b can be declared complete. The
host-prompt smoke test is recorded above, but the Child Codex benchmark,
blinded review, and dedicated approval/resume benchmark remain unexecuted.
