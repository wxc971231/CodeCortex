"""Compose ingestion, validation, training and reporting."""

from __future__ import annotations

from pathlib import Path

from forge.ingestion import parse_values
from forge.reporting import render_report
from forge.training import train_epoch
from forge.validation import validate_values


def run(payload: str, checkpoint: Path) -> str:
    values = validate_values(parse_values(payload))
    return render_report(train_epoch(values, checkpoint))
