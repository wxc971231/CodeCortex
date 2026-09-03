# CodeCortex contributor instructions

## Continuous Task Execution

For multi-task implementation work, the root Codex agent acts only as the
controller.

- Execute every implementation task through one `worker` subagent.
- The controller may select tasks, define acceptance criteria, dispatch work,
  inspect source and diffs, assess reported test evidence, and decide
  `PASS`, `FIX`, `NEXT`, or `BLOCKED`.
- The controller must not implement, directly fix, run write-producing tests,
  stage, or commit repository changes, including trivial changes.
- Give each worker a bounded task scope, clear file/module ownership,
  acceptance criteria, and required verification commands.
- A worker implements, runs the required checks, and reports changed files,
  commands/results, commit SHA, and remaining risks before it is considered
  for audit.
- The controller reviews the complete change and the reported evidence after
  every worker result. It must send any findings back to the same worker for
  correction; it must not fix them itself.
- Do not dispatch the next implementation task until the current one passes
  review. Keep at most one implementation worker active in a Codex session.
- Workers must not create or delegate to further subagents.
- Continue through the task plan until all tasks are complete or a genuine
  blocker requires a user decision.

## Optional independent review

Use the explicit `review-agent` only when an independent read-only review is
worth its extra latency and token cost. Its findings inform the controller;
it does not modify, commit, or delegate.
