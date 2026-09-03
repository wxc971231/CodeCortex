from __future__ import annotations


def annotate(values: list[str]) -> dict[str, int]:
    return {value: len(value) for value in values}
