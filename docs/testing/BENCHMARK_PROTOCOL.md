# CodeCortex M1b Benchmark Protocol

This document freezes the evaluation contract before any Native-versus-
CodeCortex Child Codex result is observed. It deliberately does not promise
that CodeCortex wins every question. Its graph-outside quality constraint is
non-regression on the fixed corpus, not an unprovable per-question guarantee.

## Frozen inputs

`tests/benchmark/questions.yaml` is JSON syntax in the YAML subset so its
parser needs no undeclared YAML dependency. It has a strict schema, sorted
case IDs, no per-case thresholds, and every case pins:

- one `fixture_state` and its versioned repository-tree SHA-256;
- the exact prompt, expected route, required fact markers and forbidden stale
  claims;
- permitted source evidence ranges;
- whether an Analyzer or a materialization offer is permitted; and
- CodeCortex side-effect constraints, including the ordinary coding case's
  exact zero MCP calls and zero formal/cache mutation requirement.

Corpus schema version 2 adds a required `initial_prompt` exclusively to the
same-topic follow-up case. This explicitly replaces the prior one-shot fiction
with a real persistent first turn and same-session resumed second turn for
both arms. The fixed follow-up prompt, scoring criteria, fixture source digests
and repetition policy are unchanged. The corpus byte digest changes from
`sha256:f5b24e479789629fe5d1dfe0bcc9c73349caf2b2cd43ad9dadd2a6a2c16812fd`
to `sha256:3dd5a4322ae92812fee99e424d80a15ba6b0cec4459d025f43f20a6093e3b693`;
old reviews cannot accept new runs. Both sanitized turn streams live in the
existing arm trace, whose entire bytes are bound to `run_digest` and human review.

`tests/fixtures/m1b_repo/base/` is the small mutable Python repository. Its
`states.json` applies a deterministic, path-safe set of replacements/deletes
to form `fresh`, `training-changed`, `unknown-dynamic`,
`conflict-current-source`, and `cache-deleted`. The cache-deleted state has
the same source tree as fresh; the future harness must additionally remove
the local `.codecortex/.cache/` after creating the formal seed, because cache
content is intentionally not part of a Git source tree.

`corpus_tree_digest()` hashes normalized relative paths and SHA-256 file
contents in byte-sorted order. A runner must reject a mismatch, materialize
the verified tree to a new temporary directory, create a temporary Git commit
there, and record the actual commit ID in its artifact. The corpus never
hardcodes the commit that contains the corpus itself.

The corpus covers exactly these M1b classes: fresh graph coverage, dynamic
L3/L4 detail, unmaterialized A and B, affected source-first, pending but
unaffected, unknown scope, graph outside, cross-Responsibility,
cognition/source conflict, deleted cache, same-topic follow-up, and ordinary
Native Codex coding.

## Run isolation

Task 11 must run Native and CodeCortex in separate copies of the same
temporary Git commit, separate temporary Codex homes, new Child Codex
processes, and identical model, reasoning, sandbox, prompt, time budget, and
repetition count. Ordinary cases use `codex exec --ephemeral --json`. Neither
side may inherit the development conversation. Native must ignore CodeCortex
configuration. The CodeCortex side may use only the isolated test MCP/Skill
configuration.

The complete per-pair workspace is created outside the artifact directory and
removed after the pair (or after preparation failure). Persisted artifacts are
restricted to the sanitized traces and report; temporary `.git/config`, copied
authentication, and machine-absolute workspace paths must not survive cleanup.
Workspace cleanup is a checked benchmark step: any removal error makes the
execution/report fail instead of being silently ignored.

Every raw JSONL trace is sanitized before persistence: redact authentication
tokens and machine-absolute paths, then retain MCP tool names, source/command
access, final answer, token counts and elapsed time. Approval/resume testing
is a separate non-ephemeral mode; it must not be folded into ordinary
single-turn results.

Tool events retain trace order and duplicates. Because JSONL traces expose MCP
tool events rather than Analyzer process-start events, the metric is explicitly
the number of `codecortex-analyzer`/`codecortex_analyzer` tool calls, including
duplicates. Materialization prompt counts
come only from actual assistant messages, never MCP payload text, while route
and anchor validation remains bound to matching MCP call/result records. The
preselected A case must show analysis-backed Proposal creation without apply;
the B case rejects every Proposal or baseline-mutation tool action.

## Deterministic scorecard

`score_answer()` emits a `ScoreCard` for observable frozen criteria:

- required-fact recall and allowed source-grounding recall;
- forbidden/stale claim count;
- required uncertainty disclosure;
- route, Analyzer, materialization prompt, MCP-call and mutation compliance;
- input/output tokens, latency, Analyzer tool-call count and prompt count.

The scorer uses frozen phrase markers and source ranges, so it is repeatable
but intentionally **not** a semantic judge. A scorecard always requires
blinded human review for semantic correctness, relevance, clarity, grounding
quality, and whether an answer merely happened to contain a marker.

Any stale claim, missing required marker/evidence, missing required
uncertainty disclosure, or policy violation fails the deterministic case.
The ordinary coding case fails if it invoked even one CodeCortex MCP tool or
mutated formal state/cache.

## Pre-frozen graph-outside non-regression gate

The following policy is code-owned in `NonRegressionPolicy`, not supplied by
the corpus or result artifact:

| Parameter | Frozen value |
|---|---:|
| Paired repetitions per graph-outside case | 3 |
| Maximum mean required-fact-recall decline | 0.10 |
| Maximum mean source-grounding-recall decline | 0.10 |
| Maximum mean stale-claim increase | 0.0 |

For each graph-outside case, Task 11 compares the three CodeCortex scorecards
against the three Native scorecards in run order. The deterministic gate fails
when any of those fixed limits is exceeded. It reports deltas rather than
claiming statistical proof from three samples. The final acceptance record
must additionally include blinded human review and explicitly state that
there was no observed systematic regression; it must never retrofit a
threshold after seeing a result.

## Reporting and exclusions

Initialization cost is reported separately and, for multi-question runs, with
an amortized value. Cache recovery cost is reported rather than hidden. No
paid-model run is part of Task 10: this task freezes data, validation, scoring
and thresholds only. Task 11 owns process execution, trace parsing/redaction,
artifact generation and human-review recording.
