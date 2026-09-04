"""Validate batch input before it reaches evaluation."""

from __future__ import annotations


def validate_values(values: list[int]) -> list[int]:
    if any(value < 0 for value in values):
        raise ValueError("values must be non-negative")
    return values
