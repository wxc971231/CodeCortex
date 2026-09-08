"""Storage path helpers independent of checkpoint writes."""

from __future__ import annotations

from pathlib import Path


def checkpoint_path(directory: Path, epoch: int) -> Path:
    return directory / f"epoch-{epoch}.json"
