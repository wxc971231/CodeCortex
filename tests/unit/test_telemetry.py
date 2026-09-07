"""Privacy and failure boundaries for disposable runtime telemetry."""

import asyncio
import inspect
import json
import logging
import os

import pytest

from codecortex import telemetry


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.delenv("CODECORTEX_LOG_DIR", raising=False)
    monkeypatch.setenv("CODECORTEX_LOG_LEVEL", "DEBUG")
    telemetry.configure()
    yield
    monkeypatch.delenv("CODECORTEX_LOG_DIR", raising=False)
    monkeypatch.setenv("CODECORTEX_LOG_LEVEL", "INFO")
    telemetry.configure()


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines()]


def test_spans_correlate_filter_scalars_and_do_not_capture_secrets(capsys):
    @telemetry.traced("outer")
    def outer(secret: str) -> str:
        with telemetry.span("inner") as record:
            record.update(parsed_files=3, prompt=secret, changed_files=[secret])
        return secret

    assert outer("credential-secret") == "credential-secret"
    rows = events(capsys)
    assert [row["event"] for row in rows] == ["start", "start", "success", "success"]
    assert len({row["operation_id"] for row in rows}) == 1
    assert rows[1]["parent_id"] == rows[0]["span_id"]
    assert rows[2]["metrics"] == {"parsed_files": 3}
    assert rows[-1]["duration_ms"] >= 0
    assert "credential-secret" not in json.dumps(rows)
    assert all(
        row["schema_version"] == 1 and row["timestamp"].endswith("Z") for row in rows
    )


def test_error_has_class_not_message_and_context_resets(capsys):
    with pytest.raises(ValueError), telemetry.span("failure"):
        raise ValueError("secret-exception")
    with telemetry.span("next"):
        pass
    rows = events(capsys)
    assert rows[1]["event"] == "error"
    assert rows[1]["metrics"]["error_class"] == "ValueError"
    assert rows[0]["operation_id"] != rows[2]["operation_id"]
    assert "secret-exception" not in json.dumps(rows)


def test_async_signature_and_independent_operation_context(capsys):
    @telemetry.traced("async")
    async def operation(value: int = 3) -> int:
        await asyncio.sleep(0)
        return value

    async def run():
        return await asyncio.gather(operation(), operation(4))

    assert inspect.iscoroutinefunction(operation)
    assert str(inspect.signature(operation)) == "(value: int = 3) -> int"
    assert asyncio.run(run()) == [3, 4]
    rows = events(capsys)
    assert len({row["operation_id"] for row in rows}) == 2
    assert all(row["parent_id"] is None for row in rows)


def test_analyzer_never_creates_log_directory(monkeypatch, tmp_path, capsys):
    target = tmp_path / "must-not-exist"
    monkeypatch.setenv("CODECORTEX_LOG_DIR", str(target))
    telemetry.configure("main")

    @telemetry.traced("read", profile="analyzer")
    async def read():
        with telemetry.span("nested"):
            return 7

    assert asyncio.run(read()) == 7
    assert not target.exists()
    assert all(row["profile"] == "analyzer" for row in events(capsys))


def test_private_bounded_rotation(monkeypatch, tmp_path, capsys):
    target = tmp_path / "logs"
    monkeypatch.setenv("CODECORTEX_LOG_DIR", str(target))
    monkeypatch.setattr(telemetry, "MAX_BYTES", 1400)
    telemetry.configure("main")
    for _ in range(30):
        with telemetry.span("rotate"):
            pass
    files = list(target.iterdir())
    assert 1 < len(files) <= telemetry.BACKUP_COUNT + 1
    assert target.stat().st_mode & 0o777 == 0o700
    for file in files:
        assert file.stat().st_mode & 0o777 == 0o600
        assert file.stat().st_size <= 1400
        assert all(
            json.loads(line)["pid"] == os.getpid()
            for line in file.read_text().splitlines()
        )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("failure", ["symlink", "not-directory", "unwritable"])
def test_sink_failure_is_disposable_and_warns_once(
    failure, monkeypatch, tmp_path, capsys
):
    target = tmp_path / "logs"
    if failure == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    elif failure == "not-directory":
        target.write_text("unchanged")
    else:
        target.mkdir(mode=0o500)
    monkeypatch.setenv("CODECORTEX_LOG_DIR", str(target))
    telemetry.configure("main")
    for _ in range(2):
        with telemetry.span("still-works"):
            pass
    rows = events(capsys)
    assert sum(row["event"] == "warning" for row in rows) == 1
    assert sum(row["event"] == "success" for row in rows) == 2
    if failure == "symlink":
        assert list(outside.iterdir()) == []
    elif failure == "not-directory":
        assert target.read_text() == "unchanged"


def test_third_party_logs_are_not_serialized(monkeypatch, tmp_path):
    target = tmp_path / "logs"
    monkeypatch.setenv("CODECORTEX_LOG_DIR", str(target))
    telemetry.configure("main")
    logging.getLogger("third.party").warning("secret-third-party")
    with telemetry.span("own"):
        pass
    assert "secret-third-party" not in "".join(
        file.read_text() for file in target.iterdir()
    )


def test_broken_stderr_and_selector_do_not_change_result(monkeypatch):
    class BrokenStream:
        def write(self, _payload):
            raise OSError("secret-stream-error")

    def broken_selector(_result):
        raise ValueError("secret-selector-error")

    @telemetry.traced("works", result=broken_selector)
    def operation():
        return 42

    monkeypatch.setattr(telemetry.sys, "stderr", BrokenStream())
    assert operation() == 42
