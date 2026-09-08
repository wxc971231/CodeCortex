# Install CodeCortex

## Users

CodeCortex users do not need Conda. Install a built wheel with pipx, then
install the user-level Codex resources:

```bash
pipx install dist/codecortex-0.1.0-py3-none-any.whl
codecortex install-codex
```

Proposal application uses Codex's native MCP tool confirmation. With the
default `prompt` mode, CodeCortex shows a proposal and calls its apply tool
with that proposal's exact digest; Codex then asks for confirmation in its
normal tool-approval UI. You do not need to type the proposal ID or digest
back into chat.

For an explicit automatic host-approval setup, install with:

```bash
codecortex install-codex --approval-mode approve
```

`approve` intentionally skips the host confirmation for this apply tool. It
does not bypass CodeCortex validation: Core still requires the exact current
proposal ID and patch digest and records the resulting approval event. Return
to the default native confirmation behavior at any time with
`codecortex install-codex --approval-mode prompt`.

The installer adds the explicit `$codecortex` skill, a read-only
`codecortex-analyzer` custom agent, and a `codecortex` Main MCP registration.
It preserves unrelated `~/.codex/config.toml` content. If a user has edited a
CodeCortex-managed resource, review the difference and use
`codecortex install-codex --force` only when replacing that resource is
intended. `--dry-run` reports the paths that would change without writing them.
Run `codecortex doctor` to verify the installation and report the configured
proposal approval mode.

## Contributors

Use the project Conda environment:

```bash
conda env create -f environment.yml
conda activate codecortex-dev
python -m pytest -q
ruff check src tests
mypy src
python -m build
```

The installer accepts an explicit home directory internally for tests. Do not
run installation tests against a real user Codex home.

See [Core runtime logging](RUNTIME_LOGGING.md) for DEBUG timings, private rotating
JSONL files, Conda/MCP configuration, and Analyzer's stderr-only contract.
