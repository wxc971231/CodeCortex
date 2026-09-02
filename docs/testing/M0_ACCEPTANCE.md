# M0 Acceptance

## Automated E2E gate

The Child Codex gate is opt-in and creates a temporary Git repository plus a temporary Codex home. It never reads or modifies the normal `~/.codex` home.

```bash
CODECORTEX_RUN_CODEX_E2E=1 CODECORTEX_E2E_COPY_AUTH=1 \
  python scripts/run_codex_e2e.py --artifact-dir /tmp/codecortex-m0-artifacts
```

`CODECORTEX_E2E_COPY_AUTH=1` copies the browser-login `auth.json` into the
pytest-created temporary Codex home with mode `600`. It is not copied into the
artifact directory or repository and disappears with the temporary test tree.

The test-only MCP configuration sets host approval to `approve`; this exists only because non-interactive CLI tests cannot display an approval dialog. It does not bypass CodeCortex's Proposal approval record checks.

The final audit also changes only the temporary `mcp_servers.codecortex.command`
to `/missing/codecortex`; the native README test still passed with the MCP
registration present but broken, proving the intended `required = false`
fallback rather than merely omitting CodeCortex configuration.

## Manual VS Code gate (default `prompt` mode)

1. Open a temporary Git repository in VS Code and install CodeCortex normally.
2. Use `$codecortex` to create a Proposal but do not approve it; verify graph revision is unchanged.
3. Approve the exact displayed patch digest and verify the host shows the `apply_cognitive_proposal` prompt before graph revision and History advance.
4. Disable CodeCortex MCP and verify ordinary Codex source reading still works.

| Date | Tester | Result | Notes |
|---|---|---|---|
| 2026-09-03 | pc5090 | passed | `$codecortex init` used Main MCP and read-only Analyzer; revision 0 graph validated. An unapproved Proposal left revision/events at 0. Host rejection of `apply_cognitive_proposal` also left revision/events at 0. A second exact-digest approval with host allow advanced revision to 1 and created `evt_01M1HXAR3HS80XZCBVKS5ACJ4N` with `approved_by: user`. `codex exec --ephemeral --ignore-user-config` still read README.md correctly. |
