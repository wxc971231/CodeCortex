"""The CodeCortex command-line interface.

The CLI is a thin adapter: it parses arguments, composes the application
services for repository-scoped commands, and translates structured domain
errors into the stable M0 exit codes. It holds no business logic. stdout
carries only user results or ``--json`` data; diagnostics, error payloads,
and tracebacks always go to stderr.
"""

import json
import sys
import traceback
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from codecortex import __version__
from codecortex.application.ports import RepositoryContextPort
from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.repository import find_repository
from codecortex.infrastructure.views import render_views

ERROR_EXIT = {
    ErrorCode.NOT_INITIALIZED: 3,
    ErrorCode.FORMAL_STATE_CORRUPT: 4,
    ErrorCode.UNSUPPORTED_SCHEMA: 4,
    ErrorCode.LOCK_TIMEOUT: 5,
}
"""Stable translation from structured domain errors to M0 exit codes."""

USAGE_ERROR_EXIT = 2
VALIDATION_FAILED_EXIT = 4
UNEXPECTED_ERROR_EXIT = 10


class InstallCodexCommand(Protocol):
    """Install or update the user-level Codex integration."""

    def __call__(self, *, dry_run: bool, force: bool) -> int: ...


class DoctorCommand(Protocol):
    """Run read-only environment checks for the local CodeCortex setup."""

    def __call__(self, *, as_json: bool) -> int: ...


class McpCommand(Protocol):
    """Run the STDIO MCP server; its stdout is reserved for protocol frames."""

    def __call__(self, *, profile: str) -> int: ...


def _command_not_available(name: str) -> int:
    print(f"codecortex {name}: command not available in this build", file=sys.stderr)
    return USAGE_ERROR_EXIT


def _install_codex_unavailable(*, dry_run: bool, force: bool) -> int:
    return _command_not_available("install-codex")


def _doctor_unavailable(*, as_json: bool) -> int:
    return _command_not_available("doctor")


def _mcp_unavailable(*, profile: str) -> int:
    return _command_not_available("mcp")


def _default_services() -> ApplicationServices:
    """Compose application services over the repository containing the CWD."""
    repository = find_repository(Path.cwd())
    # Repository.root is a frozen (read-only) dataclass attribute while the port
    # declares a settable one; the composition only ever reads it.
    context = cast(RepositoryContextPort, repository)
    return ApplicationServices(
        repository=context,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(repository.root),
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
    )


def build_parser() -> ArgumentParser:
    """Build the parser for the exact M0 CLI contract."""
    parser = ArgumentParser(prog="codecortex")
    parser.add_argument("--version", action="store_true", help="print the version")
    commands = parser.add_subparsers(dest="command")

    install_codex = commands.add_parser(
        "install-codex", help="install the user-level Codex integration"
    )
    install_codex.add_argument(
        "--dry-run", action="store_true", help="show the planned changes only"
    )
    install_codex.add_argument(
        "--force", action="store_true", help="overwrite CodeCortex-managed resources"
    )

    doctor = commands.add_parser("doctor", help="check the local CodeCortex setup")
    doctor.add_argument("--json", action="store_true", help="print JSON results")

    validate = commands.add_parser(
        "validate", help="validate the repository formal state"
    )
    validate.add_argument("--json", action="store_true", help="print JSON results")

    mcp = commands.add_parser("mcp", help="run the STDIO MCP server")
    mcp.add_argument("--profile", choices=("main", "analyzer"), required=True)
    return parser


def _validate(
    arguments: Namespace,
    services_factory: Callable[[], ApplicationServices],
) -> int:
    result = services_factory().validate_graph()
    if arguments.json:
        payload = {
            "valid": result.valid,
            "issues": [
                {
                    "code": issue.code.value,
                    "location": issue.location,
                    "message": issue.message,
                }
                for issue in result.issues
            ],
        }
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    elif result.valid:
        print("Repository formal state is valid")
    else:
        print("Repository formal state is invalid:")
        for issue in result.issues:
            print(f"  {issue.location}: {issue.code}: {issue.message}")
    return 0 if result.valid else VALIDATION_FAILED_EXIT


def _report_error(error: CodeCortexError, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(error.to_dict()), file=sys.stderr)
        return
    print(f"codecortex: {error.code}: {error.message}", file=sys.stderr)
    if error.suggested_action:
        print(f"hint: {error.suggested_action}", file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    services_factory: Callable[[], ApplicationServices] | None = None,
    install_codex: InstallCodexCommand | None = None,
    doctor: DoctorCommand | None = None,
    mcp: McpCommand | None = None,
) -> int:
    """Run the CLI contract, translating domain errors into exit codes."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    as_json = bool(getattr(arguments, "json", False))
    try:
        if arguments.version:
            print(__version__)
            return 0
        if arguments.command == "validate":
            return _validate(arguments, services_factory or _default_services)
        if arguments.command == "install-codex":
            run_install = install_codex or _install_codex_unavailable
            return run_install(dry_run=arguments.dry_run, force=arguments.force)
        if arguments.command == "doctor":
            return (doctor or _doctor_unavailable)(as_json=arguments.json)
        if arguments.command == "mcp":
            return (mcp or _mcp_unavailable)(profile=arguments.profile)
        parser.print_help(sys.stderr)
        return USAGE_ERROR_EXIT
    except CodeCortexError as error:
        _report_error(error, as_json=as_json)
        return ERROR_EXIT.get(error.code, UNEXPECTED_ERROR_EXIT)
    except Exception:  # noqa: BLE001 - the contract maps any unexpected failure to 10
        traceback.print_exc()
        return UNEXPECTED_ERROR_EXIT


def entrypoint() -> None:
    raise SystemExit(main())
