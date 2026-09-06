"""The CodeCortex command-line interface.

The CLI is a thin adapter: it parses arguments, composes the application
services for repository-scoped commands, and translates structured domain
errors into the stable M0 exit codes. It holds no business logic. stdout
carries only user results or ``--json`` data; diagnostics, error payloads,
and tracebacks always go to stderr.
"""

import json
import shutil
import sys
import traceback
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, cast

from codecortex import __version__
from codecortex.application.baseline import BaselineAdvanceService
from codecortex.application.fact_sync import FactSyncService
from codecortex.application.initialize import InitializationService
from codecortex.application.ports import RepositoryContextPort
from codecortex.application.preflight import PreflightService
from codecortex.application.proposals import ManagedSourceSnapshot, ProposalService
from codecortex.application.query import QueryService
from codecortex.application.recovery import RecoveryService
from codecortex.application.replica_providers import (
    formal_entity_ref_provider,
    formal_history_event_provider,
)
from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.domain.facts import DigestProfile, SourceConfig
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import ReadOnlyRepositoryLock, RepositoryLock
from codecortex.infrastructure.pending import PendingProposalStore
from codecortex.infrastructure.persistence.facts_db import FactsDatabase
from codecortex.infrastructure.persistence.freshness import FreshnessStore
from codecortex.infrastructure.persistence.graph_replica import GraphReplica
from codecortex.infrastructure.python.digest import (
    digest_source_file,
    repository_digest,
)
from codecortex.infrastructure.python.discovery import discover_python_source_set
from codecortex.infrastructure.repository import Repository, find_repository
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
    from codecortex.integrations.codex.install import install_codex

    executable = shutil.which("codecortex")
    if executable is None:
        return _command_not_available("install-codex")
    result = install_codex(
        Path.home(), Path(executable), dry_run=dry_run, force=force
    )
    action = "would update" if result.dry_run else "updated"
    if not result.changed:
        print("CodeCortex Codex integration is already current")
    else:
        print(f"CodeCortex {action}: {', '.join(result.changed_paths)}")
    return 0


def _doctor_unavailable(*, as_json: bool) -> int:
    from codecortex.infrastructure.repository import find_repository
    from codecortex.integrations.codex.doctor import run_doctor

    executable = shutil.which("codecortex")
    try:
        repository = find_repository(Path.cwd())
    except CodeCortexError as error:
        if error.code is not ErrorCode.NOT_INITIALIZED:
            raise
        repository = None
    report = run_doctor(
        Path.home(),
        Path(executable) if executable is not None else Path("codecortex"),
        repository,
    )
    if as_json:
        json.dump(report.to_dict(), sys.stdout)
        sys.stdout.write("\n")
    else:
        for check in report.checks:
            print(f"{check.status.upper():7} {check.code}: {check.summary}")
            if check.action:
                print(f"         hint: {check.action}")
    return VALIDATION_FAILED_EXIT if report.has_errors else 0


def _mcp_unavailable(*, profile: str) -> int:
    from codecortex.interfaces.mcp.server import run_stdio

    checked_profile = cast(Literal["main", "analyzer"], profile)
    return run_stdio(profile, lambda: _default_services(checked_profile))


def _default_services(
    profile: Literal["main", "analyzer"] = "main",
) -> ApplicationServices:
    """Compose profile-safe services over the repository containing the CWD."""
    repository = find_repository(Path.cwd())
    # Repository.root is a frozen (read-only) dataclass attribute while the port
    # declares a settable one; the composition only ever reads it.
    context = cast(RepositoryContextPort, repository)
    formal_store = FormalStore(repository)
    repository_lock = (
        RepositoryLock(repository.root)
        if profile == "main"
        else ReadOnlyRepositoryLock(repository.root)
    )
    cache_directory = repository.root / ".codecortex" / ".cache"
    facts = FactsDatabase(
        cache_directory / "facts.sqlite3",
        read_only=(profile == "analyzer"),
        repository_root=repository.root,
    )
    fact_sync = FactSyncService(
        repository,
        database=facts,
        repository_lock=repository_lock,
        formal_store=formal_store,
    )
    replica_path = cache_directory / "cognitive.sqlite3"
    entity_refs = formal_entity_ref_provider(formal_store)
    history_events = formal_history_event_provider(formal_store)
    replica = (
        GraphReplica.create_new(
            replica_path,
            entity_refs=entity_refs,
            history_events=history_events,
        )
        if profile == "main"
        else GraphReplica(
            replica_path,
            read_only=True,
            repository_root=repository.root,
            entity_refs=entity_refs,
            history_events=history_events,
        )
    )
    proposal_service = ProposalService(
        formal_store=formal_store,
        repository_lock=repository_lock,
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
        fact_sync=fact_sync,
        source_probe=lambda: _probe_sources(repository),
        facts=facts,
        replica=replica,
    )
    recovery = (
        RecoveryService(
            formal_store=formal_store,
            fact_sync=fact_sync,
            facts=facts,
            freshness_store=FreshnessStore(cache_directory),
            repository_lock=repository_lock,
            cognitive_replica=replica,
        )
        if profile == "main"
        else None
    )
    preflight = PreflightService(
        formal_store=formal_store,
        fact_sync=fact_sync,
        facts=facts,
        freshness_store=FreshnessStore(cache_directory),
        repository_lock=repository_lock,
        recovery_service=recovery,
    )
    return ApplicationServices(
        repository=context,
        formal_store=formal_store,
        repository_lock=repository_lock,
        pending_proposals=PendingProposalStore(repository),
        view_renderer=render_views,
        fact_sync=fact_sync,
        query_service=QueryService(
            formal_store=formal_store,
            facts=facts,
            replica=replica,
            repository_lock=repository_lock,
        ),
        initialization_service=InitializationService(
            formal_store=formal_store,
            fact_sync=fact_sync,
            repository_lock=repository_lock,
            proposal_service=proposal_service,
        ),
        m1a_proposal_service=proposal_service,
        preflight_service=preflight,
        baseline_advance_service=BaselineAdvanceService(
            formal_store=formal_store,
            fact_sync=fact_sync,
            facts=facts,
            freshness_store=FreshnessStore(cache_directory),
            repository_lock=repository_lock,
        ),
        cognitive_replica=replica,
    )


def _probe_sources(repository: Repository) -> ManagedSourceSnapshot:
    """Return a fresh managed-source snapshot for M1a proposal preconditions."""
    discovered = discover_python_source_set(repository, SourceConfig())
    files = [digest_source_file(source) for source in discovered.sources]
    return ManagedSourceSnapshot(
        repository_source_digest=repository_digest(files, DigestProfile()),
        file_digests={
            item.source.relative_path: item.content_digest for item in files
        },
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
    services = services_factory()
    services.recover_formal_state()
    result = services.validate_graph()
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
            if arguments.command is not None:
                print(
                    "codecortex: --version does not accept a command",
                    file=sys.stderr,
                )
                return USAGE_ERROR_EXIT
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
