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

The user approving analysis or an Analyzer dispatch is not Proposal approval.
Apply only if the user explicitly approves the exact, currently displayed
`proposal_id` and `patch_digest`; a changed/revised/stale patch needs fresh
approval.

M1a supports deterministic Python facts, bounded Analyzer reports,
analysis-backed aggregate Proposals, source baselines, rendering/inspection,
and the safe Proposal lifecycle. General freshness-routed project Q&A remains
an M1b capability.
