"""Checkpoint persistence used by training."""

from __future__ import annotations

from pathlib import Path


def write_checkpoint(path: Path, payload: str) -> None:
    """Atomically replace a checkpoint only after its temporary file is complete."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(path: Path) -> str:
    """Read the latest atomically written checkpoint."""
    return path.read_text(encoding="utf-8")
