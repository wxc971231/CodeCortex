"""Input normalization before training."""

from __future__ import annotations


def parse_values(payload: str) -> list[int]:
    return [int(item) for item in payload.split(",") if item]
