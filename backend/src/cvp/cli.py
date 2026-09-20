"""Punto de entrada de linea de comandos: `cvp --version`, `cvp validate`, `cvp run` y `cvp api`.

`validate` y `run` aceptan lo mismo (un `project.yaml` o el directorio de un proyecto) y
fallan igual ante un YAML mal escrito: el bloque de errores en stderr, codigo 1 y sin
traceback. La diferencia es lo que cuestan: `validate` responde en milisegundos porque no
importa nada pesado, y `run` paga OpenCV, ultralytics y yt-dlp **dentro de la funcion**,
que es donde toca. Un `import` de esos arriba del fichero haria lento tambien a `validate`.

`api` es el otro proceso (#12): arranca la API de lectura con uvicorn, sin cargar ningun
modelo, y con `--export-openapi` escribe el contrato `openapi.yaml` en vez de servir.
FastAPI y uvicorn se importan **dentro de la funcion**, por la misma regla que arriba.

Codigos de salida, que es el contrato con quien lo llame desde un script o desde systemd:

- `0` — todo bien, incluido el apagado limpio con el primer Ctrl+C (tambien en `api`).
- `1` — la configuracion no vale: YAML invalido, o un `type:` sin implementacion registrada.
  En `api`, una variable de entorno que no vale (`CVP_API_PORT=abc`).
- `2` — la ejecucion ha fallado: la fuente no abre ni reintentando, o un bloque revienta.
- `130` — abortado (128 + SIGINT, la convencion de siempre). Es el segundo Ctrl+C, y tambien
  el primero si llega **antes de que el pipeline arranque**, mientras se importa torch o se
  cargan los pesos: ahi todavia no hay manejador puesto ni nada que apagar, asi que el
  proceso termina por la senal y decirlo con un 0 seria mentir.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from cvp import __version__
from cvp.pipeline import ConfigError, load_project_config

# Los niveles que acepta `--log-level`, tal y como los escribe `logging`.
NIVELES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
NIVEL_POR_DEFECTO = "INFO"
VARIABLE_DE_NIVEL = "CVP_LOG_LEVEL"

_log = logging.getLogger("cvp")

# Donde vive el contrato: en la raiz del repo, commiteado. `tests/unit/test_api_contrato.py`
# falla si lo que hay ahi no coincide con lo que genera la app.
RUTA_OPENAPI = "openapi.yaml"


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

    api = subcomandos.add_parser(
        "api",
        help="arranca la API de lectura (FastAPI + uvicorn), sin cargar ningun modelo",
        description=(
            "Sirve los proyectos, sus metricas, sus eventos y el ultimo fotograma desde la "
            "base de datos (DATABASE_URL) y data/preview/. Escucha en CVP_API_HOST:CVP_API_PORT "
            "con CORS para CVP_CORS_ORIGINS; --host y --port mandan sobre el entorno. "
            "Ctrl+C apaga y sale con 0."
        ),
    )
    api.add_argument("--host", default=None, help="donde escuchar; por defecto CVP_API_HOST")
    api.add_argument("--port", type=int, default=None, help="puerto; por defecto CVP_API_PORT")
    api.add_argument(
        "--export-openapi",
        nargs="?",
        const=RUTA_OPENAPI,
        default=None,
        metavar="FICHERO",
        help=(
            "en vez de servir, escribe el contrato OpenAPI generado desde la app "
            f"(por defecto en {RUTA_OPENAPI}) y sale con 0"
        ),
    )
    api.add_argument(
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
    from cvp.pipeline.errors import AbortRequested, PipelineError
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
    except ConfigError as exc:
        # Lo que el esquema no puede comprobar y el bloque si al construirse: el contenido
        # de `zones.json` (#11). La fabrica ya lo trae con la ruta del YAML y el campo, y
        # se arregla editando un fichero, asi que suena igual que un YAML roto.
        print(exc, file=sys.stderr)
        return 1
    except PipelineError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 2
    except AbortRequested as exc:
        # El segundo Ctrl+C, que aborta a peticion del usuario. No es un `KeyboardInterrupt`
        # a proposito: ver `PipelineRunner.request_stop`.
        print(f"abortado: {exc}", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        # Un Ctrl+C que se escapa por otro camino (sin manejador puesto, o durante el propio
        # apagado). El usuario ha pedido salir ya, asi que mismo trato.
        print("abortado", file=sys.stderr)
        return 130
    except Exception:
        _log.exception("la ejecucion ha terminado por un error")
        return 2
    print(stats.summary())
    return 0


def exportar_openapi(app: Any, destino: str | os.PathLike[str]) -> None:
    """Escribe `app.openapi()` como YAML en `destino`, en UTF-8 y con `\\n`.

    Es la unica forma de generar `openapi.yaml`: `sort_keys=False` conserva el orden en
    el que FastAPI lo produce (rutas, componentes) para que el diff entre dos versiones
    sea legible, y el test del contrato compara los documentos ya parseados, no el
    texto, asi que el formato no puede desincronizarlos.
    """
    import yaml

    class _Volcador(yaml.SafeDumper):
        """Las cadenas con saltos de linea (las descripciones) van en bloque `|`, que se
        lee y se revisa en un diff; el resto, como siempre."""

    def _cadena(dumper: yaml.SafeDumper, valor: str) -> yaml.ScalarNode:
        estilo = "|" if "\n" in valor else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", valor, style=estilo)

    _Volcador.add_representer(str, _cadena)
    texto = yaml.dump(
        app.openapi(), Dumper=_Volcador, sort_keys=False, allow_unicode=True, width=100
    )
    with open(destino, "w", encoding="utf-8", newline="\n") as fichero:
        fichero.write(texto)


def _api(
    *,
    host: str | None,
    port: int | None,
    export_openapi: str | None,
    log_level: str,
    env: Mapping[str, str] | None,
) -> int:
    """Arranca la API, o escribe el contrato. FastAPI y uvicorn se importan aqui."""
    from cvp.api import Settings, crear_app

    try:
        ajustes = Settings.desde_entorno(env)
        if host is not None:
            ajustes = replace(ajustes, host=host)
        if port is not None:
            ajustes = replace(ajustes, port=port)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if export_openapi is not None:
        # `app.openapi()` no necesita la base, pero `crear_app` la abre al construir: se
        # apunta a una en memoria para no crear `data/` por escribir un fichero de texto.
        app = crear_app(replace(ajustes, database_url="sqlite://"))
        exportar_openapi(app, export_openapi)
        print(f"contrato escrito en {export_openapi}")
        return 0

    import uvicorn

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = crear_app(ajustes)
    # uvicorn captura Ctrl+C, apaga limpio y, al terminar, **vuelve a lanzar la senal** con
    # el manejador original puesto, para que el proceso "termine como manda la senal". Con
    # SIGINT eso es un `KeyboardInterrupt`, que aqui se suprime: el usuario ha pedido salir
    # y salir con 0 es lo normal. En Windows, Ctrl+Break llega como SIGBREAK, cuyo manejador
    # por defecto aborta con codigo 3 (medido en #12); se le pone el mismo que a Ctrl+C.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host=ajustes.host, port=ajustes.port, log_level=log_level.lower())
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
    if args.comando == "api":
        return _api(
            host=args.host,
            port=args.port,
            export_openapi=args.export_openapi,
            log_level=args.log_level,
            env=env,
        )
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
