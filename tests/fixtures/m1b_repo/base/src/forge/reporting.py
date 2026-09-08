"""Reporting responsibility independent of checkpoint persistence."""

from __future__ import annotations


def render_report(metrics: dict[str, float]) -> str:
    """Render a compact report from evaluation metrics."""
    return "\n".join(f"{name}={value:.2f}" for name, value in sorted(metrics.items()))


def report_title() -> str:
    return "Forge evaluation report"
