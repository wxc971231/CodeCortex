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

M0 supports status, technical initialization, graph inspection, validation and
the safe Proposal lifecycle. It does not yet implement AST analysis, freshness
sync, semantic benchmark evaluation or general project Q&A.
