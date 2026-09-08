"""Training responsibility that owns checkpoint production."""

from __future__ import annotations

from pathlib import Path

from forge.checkpoint import write_checkpoint
from forge.evaluation import evaluate_batch


def train_epoch(values: list[int], checkpoint_path: Path) -> dict[str, float]:
    """Evaluate one batch and atomically persist its metric snapshot."""
    metrics = evaluate_batch(values)
    write_checkpoint(checkpoint_path, repr(metrics))
    return metrics
