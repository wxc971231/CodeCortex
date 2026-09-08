"""Configuration boundary for the benchmark service."""

from __future__ import annotations


def checkpoint_directory(environment: str) -> str:
    return f"var/{environment}/checkpoints"
