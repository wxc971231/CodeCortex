"""Read-only diagnostics for a CodeCortex/Codex installation."""

import os
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table

from codecortex import __version__
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.repository import Repository
from codecortex.interfaces.mcp.server import MAIN_ONLY_TOOL_NAMES, READ_TOOL_NAMES

from .install import AGENT_RELATIVE, CONFIG_RELATIVE, RESOURCE_PACKAGE, SKILL_RELATIVE


class DoctorStatus(StrEnum):
    """Severity of one non-mutating diagnostic check."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class DoctorCheck:
    """A stable, secret-free diagnostic result and its suggested next action."""

    code: str
    status: DoctorStatus
    summary: str
    action: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """A complete local diagnostic report suitable for human or JSON output."""

    checks: tuple[DoctorCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status is DoctorStatus.ERROR for check in self.checks)

    def by_code(self, code: str) -> DoctorCheck:
        return next(check for check in self.checks if check.code == code)

    def to_dict(self) -> dict[str, object]:
        return {
            "has_errors": self.has_errors,
            "checks": [asdict(check) for check in self.checks],
        }


def run_doctor(
    home: Path, executable: Path, repository: Repository | None
) -> DoctorReport:
    """Inspect local integration state without creating, editing, or repairing files."""
    root = home.expanduser()
    checks = (
        _python_check(),
        _executable_check(executable),
        _resource_check(root / SKILL_RELATIVE, "SKILL.md", "CODEX_SKILL_RESOURCE"),
        _resource_check(
            root / AGENT_RELATIVE,
            "codecortex-analyzer.toml",
            "CODEX_ANALYZER_RESOURCE",
            parse_toml=True,
        ),
        *_config_checks(root / CONFIG_RELATIVE, executable),
        _writable_parents_check(root),
        _profile_allowlist_check(),
        _repository_check(repository),
    )
    return DoctorReport(checks=tuple(checks))


def _python_check() -> DoctorCheck:
    if sys.version_info[:2] == (3, 14):
        return _ok("PYTHON_VERSION", f"Python 3.14 is compatible with CodeCortex {__version__}.")
    return _error(
        "PYTHON_VERSION",
        "The active Python version is outside CodeCortex's supported 3.14 runtime.",
        "Use the codecortex-dev Conda environment or reinstall CodeCortex with Python 3.14.",
    )


def _executable_check(executable: Path) -> DoctorCheck:
    candidate = executable.expanduser()
    if candidate.exists() and candidate.is_file():
        return _ok("CODECORTEX_EXECUTABLE", "The CodeCortex executable is available.")
    return _error(
        "CODECORTEX_EXECUTABLE",
        "The CodeCortex executable cannot be resolved.",
        "Reinstall CodeCortex so `codecortex` is available on PATH.",
    )


def _resource_check(
    installed: Path, resource_name: str, code: str, *, parse_toml: bool = False
) -> DoctorCheck:
    if not installed.is_file() or installed.is_symlink():
        return _error(code, "A required CodeCortex resource is missing or unsafe.", _reinstall_action())
    try:
        expected = resources.files(RESOURCE_PACKAGE).joinpath(resource_name).read_bytes()
        actual = installed.read_bytes()
        if actual != expected:
            return _error(code, "A CodeCortex-managed resource differs from this package.", _reinstall_action())
        if parse_toml:
            tomlkit.parse(actual.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError):
        return _error(code, "A CodeCortex resource cannot be read or parsed.", _reinstall_action())
    return _ok(code, "The installed CodeCortex resource matches this package.")


def _config_checks(path: Path, executable: Path) -> tuple[DoctorCheck, DoctorCheck]:
    if not path.is_file() or path.is_symlink():
        return (
            _error("CODEX_MCP_CONFIG", "Codex MCP configuration is missing or unsafe.", _reinstall_action()),
            _error("CODEX_MCP_COMMAND", "The CodeCortex MCP registration is unavailable.", _reinstall_action()),
        )
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError):
        return (
            _error("CODEX_MCP_CONFIG", "Codex MCP configuration cannot be parsed.", _reinstall_action()),
            _error("CODEX_MCP_COMMAND", "The CodeCortex MCP registration cannot be verified.", _reinstall_action()),
        )
    parsed = _mcp_server(document)
    config = _ok("CODEX_MCP_CONFIG", "Codex MCP configuration parsed successfully.")
    expected_command = str(executable.expanduser().resolve())
    valid = (
        isinstance(parsed, Table)
        and parsed.get("command") == expected_command
        and parsed.get("args") == ["mcp", "--profile", "main"]
        and parsed.get("required") is False
        and parsed.get("startup_timeout_sec") == 10
        and parsed.get("tool_timeout_sec") == 120
        and _approval_is_prompt(parsed)
    )
    if valid:
        return config, _ok("CODEX_MCP_COMMAND", "The Main CodeCortex MCP registration matches this installation.")
    return config, _error(
        "CODEX_MCP_COMMAND",
        "The Main CodeCortex MCP registration does not match this installation.",
        _reinstall_action(),
    )


def _mcp_server(document: Any) -> Table | None:
    servers = document.get("mcp_servers") if isinstance(document, dict) else None
    if not isinstance(servers, Table):
        return None
    server = servers.get("codecortex")
    return server if isinstance(server, Table) else None


def _approval_is_prompt(server: Table) -> bool:
    tools = server.get("tools")
    if not isinstance(tools, Table):
        return False
    apply = tools.get("apply_cognitive_proposal")
    return isinstance(apply, Table) and apply.get("approval_mode") == "prompt"


def _writable_parents_check(home: Path) -> DoctorCheck:
    parents = (home / SKILL_RELATIVE).parent, (home / AGENT_RELATIVE).parent, (home / CONFIG_RELATIVE).parent
    if all(_nearest_existing(parent).is_dir() and os.access(_nearest_existing(parent), os.W_OK) for parent in parents):
        return _ok("CODEX_WRITABLE_PARENTS", "CodeCortex installation parent directories are writable.")
    return _error(
        "CODEX_WRITABLE_PARENTS",
        "A CodeCortex installation parent directory is not writable.",
        "Fix the ownership or permissions of the Codex home directory.",
    )


def _nearest_existing(path: Path) -> Path:
    cursor = path
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    return cursor


def _profile_allowlist_check() -> DoctorCheck:
    expected_read = {
        "repository_overview", "cognitive_graph", "inspect_node", "history_event", "validate_graph",
        "repository_facts", "analysis_scope", "resolve_entity_context",
        "get_discussion_context", "search_cognitive_graph",
    }
    expected_main = {
        "initialize_repository", "create_cognitive_proposal", "revise_cognitive_proposal",
        "cognitive_proposal", "apply_cognitive_proposal", "sync_repository_facts",
        "create_cognitive_proposal_from_analysis",
    }
    if READ_TOOL_NAMES == expected_read and MAIN_ONLY_TOOL_NAMES == expected_main:
        return _ok("MCP_PROFILE_ALLOWLIST", "Main and Analyzer MCP tool allowlists match the M1a contract.")
    return _error(
        "MCP_PROFILE_ALLOWLIST",
        "Main and Analyzer MCP tool allowlists do not match the M1a contract.",
        "Reinstall a compatible CodeCortex version.",
    )


def _repository_check(repository: Repository | None) -> DoctorCheck:
    if repository is None:
        return DoctorCheck(
            "REPOSITORY_STATE",
            DoctorStatus.WARNING,
            "The current directory is not a Git repository.",
            "Run `codecortex doctor` from a Git repository to inspect formal state.",
        )
    try:
        FormalStore(repository).load()
    except CodeCortexError as error:
        if error.code is ErrorCode.NOT_INITIALIZED:
            return DoctorCheck(
                "REPOSITORY_STATE",
                DoctorStatus.WARNING,
                "CodeCortex formal state is not initialized for this repository.",
                "Use `$codecortex init` in Codex to create the M0 technical skeleton.",
            )
        return _error(
            "REPOSITORY_STATE",
            "CodeCortex formal state is invalid or incompatible.",
            "Run `codecortex validate --json` for the stable validation error.",
        )
    return _ok("REPOSITORY_STATE", "Repository formal state is valid and compatible.")


def _ok(code: str, summary: str) -> DoctorCheck:
    return DoctorCheck(code, DoctorStatus.OK, summary)


def _error(code: str, summary: str, action: str) -> DoctorCheck:
    return DoctorCheck(code, DoctorStatus.ERROR, summary, action)


def _reinstall_action() -> str:
    return "Run `codecortex install-codex` again."
