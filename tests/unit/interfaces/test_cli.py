"""Unit tests for the CLI adapter: parsing, exit codes, and stream purity."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from codecortex import __version__
from codecortex.domain.cognition import (
    ValidationIssue,
    ValidationIssueCode,
    ValidationResult,
)
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.interfaces.cli.main import main


@dataclass
class CliHarness:
    """Inject fakes through the seams that keep unit tests off the real machine."""

    services: MagicMock = field(default_factory=MagicMock)
    install_codex: MagicMock = field(default_factory=MagicMock)
    doctor: MagicMock = field(default_factory=MagicMock)
    mcp: MagicMock = field(default_factory=MagicMock)

    def run(self, argv: Sequence[str]) -> int:
        return main(
            argv,
            services_factory=lambda: self.services,
            install_codex=self.install_codex,
            doctor=self.doctor,
            mcp=self.mcp,
        )


@pytest.fixture
def cli() -> CliHarness:
    return CliHarness()


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    """--version must keep working as the only pre-existing command."""
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == __version__
    assert captured.err == ""


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ErrorCode.NOT_INITIALIZED, 3),
        (ErrorCode.FORMAL_STATE_CORRUPT, 4),
        (ErrorCode.UNSUPPORTED_SCHEMA, 4),
        (ErrorCode.LOCK_TIMEOUT, 5),
    ],
)
def test_stable_error_exit_codes(
    cli: CliHarness, error: ErrorCode, expected: int
) -> None:
    """Adapters translate domain errors into the stable M0 exit codes."""
    cli.services.validate_graph.side_effect = CodeCortexError(error, "failed")
    assert cli.run(["validate", "--json"]) == expected


def test_unmapped_domain_error_exits_10(cli: CliHarness) -> None:
    """A structured error without a stable exit code is unexpected for the CLI."""
    cli.services.validate_graph.side_effect = CodeCortexError(
        ErrorCode.PATH_OUTSIDE_REPOSITORY, "escaped"
    )
    assert cli.run(["validate", "--json"]) == 10


def test_json_mode_keeps_error_payload_off_stdout(
    cli: CliHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json stdout must stay parseable, so error JSON goes to stderr."""
    cli.services.validate_graph.side_effect = CodeCortexError(
        ErrorCode.NOT_INITIALIZED, "not initialized"
    )
    assert cli.run(["validate", "--json"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == "NOT_INITIALIZED"
    assert payload["message"] == "not initialized"


def test_human_mode_reports_error_on_stderr(
    cli: CliHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human diagnostics never pollute stdout either."""
    cli.services.validate_graph.side_effect = CodeCortexError(
        ErrorCode.LOCK_TIMEOUT,
        "Repository lock timed out",
        suggested_action="Retry after the other process finishes",
    )
    assert cli.run(["validate"]) == 5
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "LOCK_TIMEOUT" in captured.err
    assert "Repository lock timed out" in captured.err
    assert "Retry after the other process finishes" in captured.err


def test_unexpected_exception_exits_10_with_traceback_on_stderr(
    cli: CliHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unexpected failures surface a traceback on stderr without touching stdout."""
    cli.services.validate_graph.side_effect = RuntimeError("boom")
    assert cli.run(["validate", "--json"]) == 10
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" in captured.err
    assert "boom" in captured.err


def test_validate_json_prints_validation_result(
    cli: CliHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """validate --json serializes the ValidationResult to stdout."""
    cli.services.validate_graph.return_value = ValidationResult(valid=True, issues=())
    assert cli.run(["validate", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"valid": True, "issues": []}
    assert captured.err == ""


def test_validate_recovers_interrupted_formal_state_before_reading(
    cli: CliHarness,
) -> None:
    """The CLI must not report a transient transaction mixture as corruption."""
    cli.services.validate_graph.return_value = ValidationResult(valid=True, issues=())

    assert cli.run(["validate", "--json"]) == 0

    cli.services.recover_formal_state.assert_called_once_with()
    cli.services.validate_graph.assert_called_once_with()


def test_validate_json_reports_issues_and_exits_4(
    cli: CliHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed formal-state validation maps to the state-validation exit code."""
    cli.services.validate_graph.return_value = ValidationResult(
        valid=False,
        issues=(
            ValidationIssue(
                code=ValidationIssueCode.INVALID_REVISION,
                location="manifest.json",
                message="revision must be zero",
            ),
        ),
    )
    assert cli.run(["validate", "--json"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["issues"] == [
        {
            "code": "INVALID_REVISION",
            "location": "manifest.json",
            "message": "revision must be zero",
        }
    ]


def test_validate_without_json_prints_human_result(
    cli: CliHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default validate output is a human-readable one-line result."""
    cli.services.validate_graph.return_value = ValidationResult(valid=True, issues=())
    assert cli.run(["validate"]) == 0
    captured = capsys.readouterr()
    assert "valid" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["frobnicate"],
        ["mcp"],
        ["mcp", "--profile", "bogus"],
        ["validate", "--bogus"],
        ["install-codex", "--json"],
    ],
)
def test_argument_errors_exit_2(
    cli: CliHarness, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse rejects unknown commands, bad profiles, and unknown flags with 2."""
    with pytest.raises(SystemExit) as excinfo:
        cli.run(argv)
    assert excinfo.value.code == 2
    assert capsys.readouterr().out == ""


def test_missing_command_prints_usage_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare `codecortex` is a usage error, not a silent success."""
    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage" in captured.err.lower()


def test_install_codex_receives_flags(cli: CliHarness) -> None:
    """The injected installer receives exactly the parsed flags."""
    cli.install_codex.return_value = 0
    assert cli.run(["install-codex", "--dry-run", "--force"]) == 0
    cli.install_codex.assert_called_once_with(dry_run=True, force=True)


def test_install_codex_defaults_flags_off(cli: CliHarness) -> None:
    cli.install_codex.return_value = 0
    assert cli.run(["install-codex"]) == 0
    cli.install_codex.assert_called_once_with(dry_run=False, force=False)


def test_doctor_receives_json_flag(cli: CliHarness) -> None:
    cli.doctor.return_value = 0
    assert cli.run(["doctor", "--json"]) == 0
    cli.doctor.assert_called_once_with(as_json=True)


def test_mcp_receives_profile(cli: CliHarness) -> None:
    cli.mcp.return_value = 0
    assert cli.run(["mcp", "--profile", "analyzer"]) == 0
    cli.mcp.assert_called_once_with(profile="analyzer")


def test_injected_command_errors_translate(cli: CliHarness) -> None:
    """Domain errors raised by injected commands use the same exit mapping."""
    cli.doctor.side_effect = CodeCortexError(ErrorCode.FORMAL_STATE_CORRUPT, "corrupt")
    assert cli.run(["doctor", "--json"]) == 4


def test_services_are_only_built_for_validate(cli: CliHarness) -> None:
    """User-level commands must not require a repository or its composition."""
    cli.install_codex.return_value = 0
    assert (
        main(
            ["install-codex", "--dry-run"],
            services_factory=MagicMock(
                side_effect=AssertionError("services must stay unbuilt")
            ),
            install_codex=cli.install_codex,
        )
        == 0
    )
