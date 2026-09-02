"""The CodeCortex command-line interface."""

from argparse import ArgumentParser
from collections.abc import Sequence

from codecortex import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog="codecortex")
    parser.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.version:
        print(__version__)
    return 0


def entrypoint() -> None:
    raise SystemExit(main())
