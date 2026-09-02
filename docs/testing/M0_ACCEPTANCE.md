# M0 Acceptance

## Automated E2E gate

The Child Codex gate is opt-in and creates a temporary Git repository plus a temporary Codex home. It never reads or modifies the normal `~/.codex` home.

```bash
CODECORTEX_RUN_CODEX_E2E=1 python scripts/run_codex_e2e.py --artifact-dir /tmp/codecortex-m0-artifacts
```

The test-only MCP configuration sets host approval to `approve`; this exists only because non-interactive CLI tests cannot display an approval dialog. It does not bypass CodeCortex's Proposal approval record checks.

## Manual VS Code gate (default `prompt` mode)

1. Open a temporary Git repository in VS Code and install CodeCortex normally.
2. Use `$codecortex` to create a Proposal but do not approve it; verify graph revision is unchanged.
3. Approve the exact displayed patch digest and verify the host shows the `apply_cognitive_proposal` prompt before graph revision and History advance.
4. Disable CodeCortex MCP and verify ordinary Codex source reading still works.

| Date | Tester | Result | Notes |
|---|---|---|---|
| pending | pending | pending | Required before declaring M0 complete. |
