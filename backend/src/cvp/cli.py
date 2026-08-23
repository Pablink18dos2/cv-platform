"""Punto de entrada de linea de comandos: `cvp --version`, `cvp validate` y `cvp run`.

Los dos subcomandos aceptan lo mismo (un `project.yaml` o el directorio de un proyecto) y
fallan igual ante un YAML mal escrito: el bloque de errores en stderr, codigo 1 y sin
traceback. La diferencia es lo que cuestan: `validate` responde en milisegundos porque no
importa nada pesado, y `run` paga OpenCV, ultralytics y yt-dlp **dentro de la funcion**,
que es donde toca. Un `import` de esos arriba del fichero haria lento tambien a `validate`.

Codigos de salida, que es el contrato con quien lo llame desde un script o desde systemd:

- `0` — todo bien, incluido el apagado limpio con Ctrl+C.
- `1` — la configuracion no vale: YAML invalido, o un `type:` sin implementacion registrada.
- `2` — la ejecucion ha fallado: la fuente no abre ni reintentando, o un bloque revienta.
- `130` — abortado por un segundo Ctrl+C (128 + SIGINT, la convencion de siempre).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence

from cvp import __version__
from cvp.pipeline import ConfigError, load_project_config

# Los niveles que acepta `--log-level`, tal y como los escribe `logging`.
NIVELES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
NIVEL_POR_DEFECTO = "INFO"
VARIABLE_DE_NIVEL = "CVP_LOG_LEVEL"

_log = logging.getLogger("cvp")


def nivel_por_defecto(env: Mapping[str, str] | None = None) -> str:
    """El nivel de log por defecto, que sale de `CVP_LOG_LEVEL` si esta puesta.

    Un valor que no existe no tumba el arranque: el worker corre desatendido y una
    variable de entorno mal escrita no es motivo para no medir nada esta noche. Se avisa
    por stderr y se sigue con `INFO`.
    """
    entorno = os.environ if env is None else env
    crudo = entorno.get(VARIABLE_DE_NIVEL)
    if not crudo:
        return NIVEL_POR_DEFECTO
    nivel = crudo.strip().upper()
    if nivel not in NIVELES:
        print(
            f"{VARIABLE_DE_NIVEL}={crudo!r} no es un nivel de log "
            f"({', '.join(NIVELES)}); se usa {NIVEL_POR_DEFECTO}",
            file=sys.stderr,
        )
        return NIVEL_POR_DEFECTO
    return nivel


def build_parser(env: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
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

    run = subcomandos.add_parser(
        "run",
        help="arranca el pipeline de un proyecto y lo mantiene corriendo",
        description=(
            "Corre fuente -> detector -> tracker -> analizadores -> sinks. Sin --max-frames "
            "ni --duration no termina solo cuando la fuente es un directo, que es su razon "
            "de ser; Ctrl+C lo apaga limpio."
        ),
    )
    run.add_argument("path", help="el project.yaml o el directorio del proyecto")
    run.add_argument(
        "--max-frames",
        type=int,
        default=None,
        metavar="N",
        help="para y sale con 0 tras procesar N fotogramas",
    )
    run.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="S",
        dest="duration_s",
        help="para y sale con 0 tras S segundos",
    )
    run.add_argument(
        "--health-every",
        type=float,
        default=30.0,
        metavar="S",
        dest="health_every_s",
        help="cada cuantos segundos se escribe la linea de salud (por defecto 30)",
    )
    run.add_argument(
        "--log-level",
        choices=NIVELES,
        default=nivel_por_defecto(env),
        help=f"nivel de log; por defecto {VARIABLE_DE_NIVEL} del entorno, o INFO",
    )
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


def _run(
    path: str,
    *,
    max_frames: int | None,
    duration_s: float | None,
    health_every_s: float,
    log_level: str,
) -> int:
    """Arranca el pipeline. La configuracion se valida **antes** de importar nada pesado."""
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        config = load_project_config(path)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    # Aqui si toca pagar OpenCV, ultralytics y yt-dlp: son las dependencias de los bloques
    # que se van a construir. `cvp validate` no pasa por esta linea.
    from cvp.core import UnknownTypeError
    from cvp.pipeline.errors import PipelineError
    from cvp.pipeline.runner import run_project

    try:
        stats = run_project(
            config,
            max_frames=max_frames,
            duration_s=duration_s,
            health_every_s=health_every_s,
        )
    except UnknownTypeError as exc:
        # El `type:` valida contra el esquema pero nadie lo ha registrado. Se arregla
        # editando el YAML (o implementando el bloque), asi que lleva el codigo de la
        # configuracion y sale sin traceback, igual que `validate`.
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    except PipelineError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # El segundo Ctrl+C, que aborta el apagado limpio a peticion del usuario.
        print("abortado", file=sys.stderr)
        return 130
    except Exception:
        _log.exception("la ejecucion ha terminado por un error")
        return 2
    print(stats.summary())
    return 0


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = build_parser(env)
    args = parser.parse_args(argv)
    if args.comando == "validate":
        return _validate(args.path)
    if args.comando == "run":
        return _run(
            args.path,
            max_frames=args.max_frames,
            duration_s=args.duration_s,
            health_every_s=args.health_every_s,
            log_level=args.log_level,
        )
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
