"""Evaluation metrics with a lazily loaded optional detail."""

from __future__ import annotations


def evaluate_batch(values: list[int]) -> dict[str, float]:
    """Return the stable top-level score used by the training loop."""
    if not values:
        return {"mean": 0.0}
    return {"mean": sum(values) / len(values)}


def load_metric_explanation(metric: str) -> str:
    """Dynamic L3/L4 detail is loaded only for an explanation request."""
    explanations = {"mean": "Arithmetic mean of the evaluated batch."}
    return explanations[metric]
