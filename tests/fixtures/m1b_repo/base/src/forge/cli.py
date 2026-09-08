"""Ordinary code path intentionally outside the initial cognition graph."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge")
    parser.add_argument("--dry-run", action="store_true")
    return parser
