"""Schedule epochs without owning their training behaviour."""

from __future__ import annotations


def epoch_numbers(total: int) -> range:
    return range(1, total + 1)
