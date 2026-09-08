"""Small independent telemetry seam."""

from __future__ import annotations


def event_name(stage: str) -> str:
    return f"forge.{stage}"
