---
name: codecortex
description: Use only when the user explicitly invokes $codecortex for persistent project understanding.
---

# CodeCortex

Use this skill only for an explicit `$codecortex` request. Do not make ordinary
Codex coding depend on CodeCortex.

1. Call `repository_overview` first. If the repository is not initialized,
   explain that M0 can initialize only the technical skeleton.
2. For `$codecortex init` or repository-wide analysis, delegate evidence
   gathering to the `codecortex-analyzer` subagent. The analyzer is read-only
   and must return a bounded analysis report; it must not change files or
   CodeCortex formal state.
3. Treat every formal cognition change as a discussion: create a Proposal,
   show its affected scope and current `patch_digest`, and allow the user to
   discuss or revise it.
4. Call `apply_cognitive_proposal` only after the user explicitly approves the
   exact current patch digest. Never infer approval from an earlier message.
5. If CodeCortex is unavailable or the graph does not cover the question, use
   normal Codex source search and explain the limitation. Do not block ordinary
   work while waiting for CodeCortex.

## M1a initialization and reinitialization

For `$codecortex init`, first call `initialize_repository` only when formal
state is absent.  Then use this exact sequence; never replace it with an
unbounded `cognitive_graph` read:

```text
sync_repository_facts(mode="full")
→ analysis_scope(expected source digest/revision)
→ delegate codecortex-analyzer
→ receive one JSON AnalysisReport only
→ create_cognitive_proposal_from_analysis
→ show Big Picture + operation diff + affected scope + uncertainties
→ discuss/revise if requested
→ apply only after explicit approval of the current patch_digest
```

The Analyzer must use its read-only profile, read only bounded fact/context
pages plus selected source, and return one report with the exact
`AnalysisReport` schema. It must not create a Proposal or modify any state.
Pass the current graph revision and source digest to it. If Core rejects the
report as stale or invalid, discard it and run a new Analyzer pass; do not
repair, split, or reuse its patch.

## M1a graph shape rules (Core enforces these fail-closed)

The AnalysisReport must produce a graph that passes Core validation on the
first try. Teach the analyzer these rules verbatim at dispatch:

- Node kinds: `responsibility` (what the project is accountable for),
  `behavior` (a user-observable thing it does), `capability` (a shared
  mechanism behaviors rely on).
- Exactly three edge types are legal, with fixed endpoint kinds:
  `contains` (responsibility -> behavior only), `uses`
  (behavior -> capability only), `depends_on` (capability -> capability
  only). Anything else is rejected as INVALID_RELATION.
- Every behavior must have exactly one responsibility parent via `contains`,
  and the contains hierarchy must be acyclic.
- Evidence: structural `contains` edges are exempt, but every `uses` or
  `depends_on` edge with epistemic status `inferred` or `uncertain` must
  carry at least one report evidence item (entity UID, repository-relative
  path, and line range) or Core rejects the apply as
  RELATION_EVIDENCE_REQUIRED.
- Every implementation mapping must reference a real entity UID returned by
  the fact/context read tools; never invent an anchor.

`create_cognitive_proposal_from_analysis` is the only normal route from an
Analyzer report to a pending Proposal. It validates the complete report and
creates one aggregate Proposal. Show the user its high-level responsibilities,
behaviors, capabilities and flows, every operation/diff category, affected
scope, evidence limits, unmapped regions and uncertainties. A request to
change the candidate means revise the current pending Proposal and show its
new digest again.

For `$codecortex reinitialize`, always delegate the Analyzer. Before doing so,
give it bounded existing graph context, relevant user-confirmed `intent`, and
applicable approval History in addition to the current fact scope. Require a
global diff that explicitly labels additions, deletions, moves, merges,
splits, and conflicts. Never clear the graph and never silently replace a
user-confirmed intent: conflicts remain explicit Proposal operations for the
user to discuss and approve. "No deletion proposed" is an explicit outcome,
not permission to erase unmentioned nodes.

The reinitialize AnalysisReport must include `change_operations`; candidate
collections alone are not a diff. Each operation has exactly
`kind`, `target_id`, `before_revision`, `change_kind`, and `change_group`.
Use the existing stable-ID primitives (`add/update/remove_node`,
`add/update/remove_edge`, `set_logical_flow`, `remove_logical_flow`, and
`add/update/remove_mapping`). A non-remove operation references its after
value by the same stable ID in exactly one candidate collection. Use null
`before_revision` only when the target must be absent; update/remove requires
the exact positive object revision from the existing graph. Never infer a
remove from an omitted candidate.

`change_kind` is one of add/update/remove/move/merge/split/conflict and
`change_group` is a lowercase slug shared by all primitives for that change.
These are audit labels, not magic operations: spell out every edge, flow, and
mapping rewire. A merge group needs an explicit `remove_node` plus a retained
or rewired primitive; a split group needs an explicit `add_node` plus an
updated or rewired primitive. Mark a concrete proposed conflict resolution
with `conflict`; if no deterministic primitive resolution is ready, report it
under `uncertainties` and do not invent an operation. Initial analysis may
omit `change_operations` only while `cognition_initialized=false`, in which
case Core retains the legacy add-only interpretation.

The user approving analysis or an Analyzer dispatch is not Proposal approval.
Apply only if the user explicitly approves the exact, currently displayed
`proposal_id` and `patch_digest`; a changed/revised/stale patch needs fresh
approval.

M1a supports deterministic Python facts, bounded Analyzer reports,
analysis-backed aggregate Proposals, source baselines, rendering/inspection,
and the safe Proposal lifecycle. General freshness-routed project Q&A remains
an M1b capability.

## M1b explicit project discussions and semantic synchronization

For every explicit `$codecortex ask`, `inspect`, `sync`, `expand`, or
reinitialize operation, run deterministic Fact Preflight before drawing a
conclusion. It refreshes only disposable facts and computes one current
baseline-to-source ChangeSet; it never calls an Agent and never needs user
approval. Do not make ordinary Codex coding run this workflow.

For a question, use `search_cognitive_graph` for bounded candidate recall, use
your own reasoning to confirm relevant Responsibility/Behavior/Capability
anchors, obtain `effective_query_freshness`, then pull only bounded discussion
context and the necessary current facts/source. A graph result is a preference,
not a search restriction. If there is no confirmed semantic anchor, use normal
Native Codex `rg`, directory, source, configuration, test, and documentation
exploration without restriction.

If query freshness is `affected_source_first` or `unknown_source_first`, label
the graph as baseline navigation only. Read current facts and source before
answering, distinguish approved graph conclusions from current-source evidence
and uncertainty, and do not block the answer waiting for a graph update.

For a relevant unmaterialized Behavior, ask **once per explicit CodeCortex
invocation**:

```text
A. Expand it now: analyze the current bounded scope, then show one aggregate
   Proposal. This authorizes analysis only; it is not approval of an unknown
   patch. Apply only after the user explicitly approves the displayed current
   patch_digest.
B. Keep it transient: answer now from current graph coverage, indexed facts,
   and source. Create no Proposal and do not repeat this question for the same
   Behavior during this invocation.
```

Keep that one-time decision only in the live Main invocation; do not persist a
Codex thread or chat history in Core or `.codecortex/`. Never auto-apply,
auto-create a formal graph change, or turn a per-file observation into a
Proposal.

For explicit `$codecortex sync`, use a small, complete, single-Responsibility
scope for Main Codex analysis. Use the read-only `codecortex-analyzer` for a
large, cross-Responsibility, unresolved, or otherwise unbounded scope. Either
path may produce an aggregate Proposal only after analysis; semantic changes
still require exact user approval to apply. A no-semantic-change conclusion may
use the audited baseline-advance path described by Core.

Summarize pending Proposals at most once per explicit CodeCortex invocation.
After the user defers or rejects one, do not repeat the reminder in that
invocation. Mention high-impact deletion or migration only at a natural
checkpoint; never interrupt ordinary Codex work for it.
