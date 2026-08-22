"""Punto de entrada de linea de comandos.

De momento solo expone `cvp --version`. El subcomando `run`, que arranca el pipeline
de un proyecto, llega en la tarea 8.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from cvp import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cvp", description="CV Platform")
    parser.add_argument("--version", action="version", version=f"cvp {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
