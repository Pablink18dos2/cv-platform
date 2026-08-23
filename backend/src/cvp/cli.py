"""Punto de entrada de linea de comandos.

`cvp --version` y `cvp validate <ruta>`. El subcomando `run`, que arranca el pipeline de
un proyecto, llega en la tarea 8 y reutilizara `load_project_config`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from cvp import __version__
from cvp.pipeline import ConfigError, load_project_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cvp", description="CV Platform")
    parser.add_argument("--version", action="version", version=f"cvp {__version__}")
    subcomandos = parser.add_subparsers(dest="comando")
    validate = subcomandos.add_parser(
        "validate",
        help="valida el project.yaml de un proyecto sin cargar ningun modelo",
        description=(
            "Valida la configuracion de un proyecto y sale con 0 si esta bien. Responde en "
            "milisegundos: no importa torch, ni OpenCV, ni ultralytics."
        ),
    )
    validate.add_argument("path", help="el project.yaml o el directorio del proyecto")
    return parser


def _validate(path: str) -> int:
    """Imprime el resumen y sale con 0, o el bloque de errores en stderr y sale con 1.

    Sin traceback: un `ConfigError` es un error de quien escribio el YAML, no un fallo del
    programa, y un traceback solo esconderia el mensaje que hay que leer.
    """
    try:
        config = load_project_config(path)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"{config.id} ({config.name}): source {config.source.type}, "
        f"detector {config.detector.type}, {len(config.analyzers)} analizadores, "
        f"{len(config.sinks)} sinks"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.comando == "validate":
        return _validate(args.path)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
