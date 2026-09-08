"""A dynamic plugin seam that intentionally makes static scope uncertain."""

from __future__ import annotations

from importlib import import_module


def load_formatter(module_name: str) -> object:
    """Load a formatter supplied by a deployment-specific module name."""
    return import_module(module_name)
