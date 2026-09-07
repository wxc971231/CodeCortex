"""Real Core work emits useful metadata without serializing its payloads."""

import inspect
import json
import subprocess
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codecortex import telemetry
from codecortex.application.query import CacheCoordinate, QueryService
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.persistence.facts_db import Page
from codecortex.infrastructure.persistence.graph_replica import ContextRequest
from codecortex.interfaces.cli.main import main
from codecortex.interfaces.mcp.server import build_server, run_stdio
from tests.integration.test_m1a_apply import M1aHarness, _full_report, approval_for


@pytest.fixture(autouse=True)
def logging_config(monkeypatch):
    monkeypatch.delenv("CODECORTEX_LOG_DIR", raising=False)
    monkeypatch.setenv("CODECORTEX_LOG_LEVEL", "DEBUG")
    telemetry.configure()
    yield
    monkeypatch.setenv("CODECORTEX_LOG_LEVEL", "INFO")
    telemetry.configure()


def records(capsys):
    return [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{")
    ]


def test_real_sync_apply_and_query_emit_internal_spans(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text(
        "def answer_question(text):\n    return text.upper()\n"
    )
    harness = M1aHarness(tmp_path)
    harness.initialize_formal()
    with telemetry.span("test.workflow", root=True):
        report = _full_report(harness)
        proposal = harness.service.create_proposal_from_analysis(
            report, "secret-reason"
        )
        applied = harness.service.apply_cognitive_proposal(
            proposal.proposal_id, approval_for(proposal)
        )
        query = QueryService(
            formal_store=harness.formal_store,
            facts=harness.facts,
            replica=harness.replica,
            repository_lock=harness.lock,
        )
        page = query.repository_facts("pkg", limit=1)
        query.get_discussion_context(
            ContextRequest(node_ids=("behavior.answer-question",), max_nodes=1)
        )
    rows = records(capsys)
    successful = {row["name"]: row for row in rows if row["event"] == "success"}
    assert {
        "fact_sync",
        "fact_sync.snapshot",
        "fact_sync.parse",
        "fact_sync.commit",
        "proposal.create",
        "proposal.apply",
        "formal.commit",
        "proposal.cache_refresh",
        "query.repository_facts",
        "query.discussion_context",
    } <= successful.keys()
    assert successful["proposal.apply"]["metrics"] == {
        "graph_revision": applied.graph_revision,
        "cache_warning_count": 0,
    }
    assert (
        successful["query.repository_facts"]["metrics"]["repository_source_digest"]
        == page.coordinate.repository_source_digest
    )
    assert successful["query.discussion_context"]["metrics"]["truncated"] is True
    assert successful["query.discussion_context"]["metrics"]["truncation_count"] > 0
    assert len({row["operation_id"] for row in rows}) == 1
    serialized = json.dumps(rows)
    assert "secret-reason" not in serialized
    assert "return text.upper" not in serialized
    assert "Approve current patch" not in serialized


@pytest.mark.anyio
async def test_every_mcp_tool_retains_signature_async_and_request_roots(capsys):
    services = MagicMock()
    server = build_server("analyzer", services)
    for profile in ("analyzer", "main"):
        for tool in build_server(profile, services)._tool_manager._tools.values():
            assert tool.is_async
            assert inspect.iscoroutinefunction(tool.fn)
            assert inspect.signature(tool.fn) == inspect.signature(tool.fn.__wrapped__)
    with (
        telemetry.span("cli.command"),
        patch(
            "codecortex.interfaces.mcp.server.tools.repository_overview",
            return_value=MagicMock(),
        ),
    ):
        await server._tool_manager._tools["repository_overview"].fn()
        await server._tool_manager._tools["repository_overview"].fn()
    rows = [row for row in records(capsys) if row["name"] == "mcp.repository_overview"]
    assert len({row["operation_id"] for row in rows}) == 2
    assert all(
        row["parent_id"] is None and row["profile"] == "analyzer" for row in rows
    )


def test_analyzer_cli_startup_with_log_dir_never_writes(monkeypatch, tmp_path, capsys):
    log_directory = tmp_path / "must-not-exist"
    monkeypatch.setenv("CODECORTEX_LOG_DIR", str(log_directory))
    with patch(
        "codecortex.interfaces.mcp.server.build_server", return_value=MagicMock()
    ):
        assert (
            main(
                ["mcp", "--profile", "analyzer"],
                mcp=lambda **kwargs: run_stdio(kwargs["profile"], MagicMock()),
            )
            == 0
        )
    assert not log_directory.exists()
    assert all(row["profile"] == "analyzer" for row in records(capsys))


def test_reconfigure_same_settings_does_not_split_file(monkeypatch, tmp_path):
    directory = tmp_path / "logs"
    monkeypatch.setenv("CODECORTEX_LOG_DIR", str(directory))
    telemetry.configure()
    with telemetry.span("outer"):
        telemetry.configure()
        with telemetry.span("inner"):
            pass
    assert len(list(directory.iterdir())) == 1
    rows = [
        json.loads(line) for line in next(directory.iterdir()).read_text().splitlines()
    ]
    assert [row["event"] for row in rows] == ["start", "start", "success", "success"]


@pytest.mark.anyio
async def test_mcp_error_logs_code_without_domain_message(capsys):
    services = MagicMock()
    services.repository_overview.side_effect = CodeCortexError(
        ErrorCode.NOT_INITIALIZED, "secret-error"
    )
    with pytest.raises(ToolError):
        await (
            build_server("analyzer", services)
            ._tool_manager._tools["repository_overview"]
            .fn()
        )
    rows = records(capsys)
    assert rows[-1]["metrics"]["error_code"] == "NOT_INITIALIZED"
    assert "secret-error" not in json.dumps(rows)


def test_cli_nonzero_exit_is_logged_as_error(capsys):
    def fail(**_kwargs):
        raise CodeCortexError(ErrorCode.NOT_INITIALIZED, "user-facing failure")

    assert main(["doctor", "--json"], doctor=fail) == 3
    rows = records(capsys)
    command = [row for row in rows if row.get("name") == "cli.command"]
    assert command[-1]["event"] == "error"
    assert command[-1]["metrics"] == {
        "error_code": "NOT_INITIALIZED", "error_class": "CodeCortexError", "exit_code": 3,
    }


@pytest.mark.parametrize("child", ["diagnostics", "mappings", "relations"])
def test_child_only_query_truncation_is_visible_in_telemetry(child, capsys):
    facts, replica = MagicMock(), MagicMock()
    query = QueryService(formal_store=MagicMock(), facts=facts,
                         replica=replica, repository_lock=MagicMock())
    coordinate = CacheCoordinate("sha256:" + "a" * 64, 1)
    facts.analysis_partitions.return_value = Page(items=(), next_cursor=None, truncated=False)
    facts.query_diagnostics.return_value = Page(items=(), next_cursor=None, truncated=child == "diagnostics")
    facts.entities_at_path.return_value = Page(items=(MagicMock(uid="entity.stub"),), next_cursor=None, truncated=False)
    facts.query_relations.return_value = Page(items=(), next_cursor=None, truncated=child == "relations")
    replica.mappings_for_entities.return_value = ((), child == "mappings")
    with patch.object(query, "_guarded_read", return_value=nullcontext(coordinate)):
        result = query.analysis_scope() if child == "diagnostics" else query.resolve_entity_context(path="pkg/a.py")
    assert result.truncated is False
    assert records(capsys)[-1]["metrics"]["truncated"] is True
