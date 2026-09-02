"""Process-level coverage for the read-only ``codecortex doctor`` command."""

import json
import os
import subprocess
import sys
from pathlib import Path

import codecortex
from codecortex.integrations.codex.install import install_codex

SOURCE_ROOT = Path(codecortex.__file__).resolve().parents[1]


def test_doctor_json_reports_clean_temporary_home(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "codecortex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    install_codex(tmp_path, executable, dry_run=False, force=False)
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{executable.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(SOURCE_ROOT),
    }

    result = subprocess.run(
        [sys.executable, "-m", "codecortex", "doctor", "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["has_errors"] is False
    assert result.stderr == ""
