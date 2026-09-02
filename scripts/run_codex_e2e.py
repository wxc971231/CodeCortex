"""Run opt-in Codex E2E tests and retain stdout/stderr as artifacts."""

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    arguments.artifact_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/e2e", "-m", "codex_e2e", "-q"], capture_output=True, text=True, check=False)
    (arguments.artifact_dir / "pytest.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (arguments.artifact_dir / "pytest.stderr.txt").write_text(result.stderr, encoding="utf-8")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
