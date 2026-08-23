"""De `type:` a objeto: el unico sitio donde se construyen los bloques de un proyecto.

Aqui se cierra el hueco que #3 dejo abierto a proposito. `cvp.pipeline.config` valida el
`type:` contra **sus** esquemas (`CONFIG_SCHEMAS`) para que `cvp validate` responda en
milisegundos sin importar torch; los nombres de las **implementaciones** viven en otra
lista, los registros de `cvp.core.registry`, que solo se llenan al importar `cvp.sources`,
`cvp.detectors`, ... Un nombre que este en la primera lista y no en la segunda valida y
luego no se puede construir: este modulo lo detecta **al arrancar**, con la lista de lo que
si existe, y no en el minuto 40.

Dos reglas que explican la forma del modulo:

- **Todo se resuelve antes de construir nada.** Un `type:` sin implementacion tiene que
  fallar antes de que YOLO cargue medio gigabyte de pesos y antes de que el connector abra
  el directo; si no, el error tarda medio minuto en aparecer y llega detras de mil lineas
  de log de ultralytics.
- **No hay ni un `if tipo == ...`.** El nombre lo resuelve el registro y el objeto lo
  construye su propio `from_config`. Lo unico que sabe este modulo es que **el tracker
  necesita un `frame_rate` que no esta en el YAML**, porque es el `target_fps` de la
  fuente; eso es una regla del bloque `tracker`, no de una implementacion concreta, y por
  eso vive en una tabla y no en un condicional.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, NamedTuple

from cvp.core import ANALYZERS, DETECTORS, SINKS, SOURCES, TRACKERS, UnknownTypeError
from cvp.core.registry import Registry
from cvp.pipeline.config import ProjectConfig
from cvp.pipeline.errors import PipelineError

__all__ = [
    "PAQUETES_DE_IMPLEMENTACION",
    "REGISTROS",
    "Blocks",
    "build_pipeline",
    "load_implementations",
]

_log = logging.getLogger(__name__)

# Los paquetes cuyos decoradores `@register_*` llenan los registros. Importarlos cuesta
# unos segundos (cv2, supervision) y por eso no se importan en `cvp.pipeline.__init__`:
# `cvp validate` no puede pagarlos.
PAQUETES_DE_IMPLEMENTACION = (
    "cvp.sources",
    "cvp.detectors",
    "cvp.trackers",
    "cvp.analyzers",
    "cvp.sinks",
)

# Campo del YAML -> registro que resuelve su `type:`.
REGISTROS: Mapping[str, Registry[Any]] = MappingProxyType(
    {
        "source": SOURCES,
        "detector": DETECTORS,
        "tracker": TRACKERS,
        "analyzers": ANALYZERS,
        "sinks": SINKS,
    }
)


class Blocks(NamedTuple):
    """Los cinco bloques de un proyecto, ya construidos."""

    source: Any
    detector: Any
    tracker: Any
    analyzers: tuple[Any, ...]
    sinks: tuple[Any, ...]


def load_implementations() -> None:
    """Importa los paquetes de implementacion para que corran sus decoradores.

    Que `cvp.analyzers` o `cvp.sinks` no existan todavia **no es un error**: son #9, #10 y
    #11, y hasta entonces sus registros estan vacios. Lo que si es un error es que un
    paquete que existe no se pueda importar porque le falte una dependencia, y eso se
    distingue mirando **que** modulo falta, no tragandose el `ModuleNotFoundError` entero.
    """
    for paquete in PAQUETES_DE_IMPLEMENTACION:
        try:
            importlib.import_module(paquete)
        except ModuleNotFoundError as exc:
            if exc.name != paquete:
                raise
            _log.debug("%s todavia no existe; sus bloques no estaran registrados", paquete)


def build_pipeline(
    config: ProjectConfig,
    *,
    registries: Mapping[str, Registry[Any]] | None = None,
    load: Callable[[], None] | None = load_implementations,
) -> Blocks:
    """Construye los bloques de `config`, resolviendo cada `type:` en su registro.

    `registries` y `load` existen para los tests: con registros propios se prueba el
    cableado entero —que cada bloque se resuelve por nombre, que el tracker recibe el
    `frame_rate` de la fuente— sin importar ultralytics ni OpenCV, que son tres segundos
    de la suite. En produccion se usan los de verdad.

    Lanza `UnknownTypeError` si un `type:` valido segun el esquema no tiene implementacion
    registrada, y `PipelineError` si la clase registrada no ofrece `from_config`.
    """
    if registries is None:
        if load is not None:
            load()
        registries = REGISTROS

    # Primero los cinco nombres, despues los cinco objetos. Resolver es instantaneo;
    # construir abre el directo y carga los pesos.
    clase_fuente = _resolver(registries, "source", config.source.type, "source")
    clase_detector = _resolver(registries, "detector", config.detector.type, "detector")
    clase_tracker = _resolver(registries, "tracker", config.tracker.type, "tracker")
    clases_analizadores = [
        _resolver(registries, "analyzers", cfg.type, f"analyzers[{i}]")
        for i, cfg in enumerate(config.analyzers)
    ]
    clases_sinks = [
        _resolver(registries, "sinks", cfg.type, f"sinks[{i}]")
        for i, cfg in enumerate(config.sinks)
    ]

    source = _construir(clase_fuente, config.source, "source")
    detector = _construir(clase_detector, config.detector, "detector")
    # El unico argumento que no sale del YAML. `frame_rate` es la tasa a la que el tracker
    # ve fotogramas, o sea `source.target_fps`: escribirlo tambien en el bloque `tracker`
    # seria una forma segura de que los dos acabaran discrepando (decision de #3 y #7).
    tracker = _construir(
        clase_tracker, config.tracker, "tracker", frame_rate=config.source.target_fps
    )
    analyzers = tuple(
        _construir(clase, cfg, f"analyzers[{i}]")
        for i, (clase, cfg) in enumerate(zip(clases_analizadores, config.analyzers, strict=True))
    )
    sinks = tuple(
        _construir(clase, cfg, f"sinks[{i}]")
        for i, (clase, cfg) in enumerate(zip(clases_sinks, config.sinks, strict=True))
    )

    if not analyzers:
        _log.warning(
            "el proyecto %s no tiene analizadores: corren fuente, detector y tracker y no "
            "se mide nada. Es el modo con el que se mide el encadenado completo",
            config.id,
        )
    if not sinks:
        _log.warning(
            "el proyecto %s no tiene sinks: lo que salga de los analizadores no se escribe "
            "en ningun sitio",
            config.id,
        )
    return Blocks(source, detector, tracker, analyzers, sinks)


def _resolver(
    registries: Mapping[str, Registry[Any]], bloque: str, nombre: str, campo: str
) -> type[Any]:
    """La clase registrada bajo `nombre`, o `UnknownTypeError` diciendo cual es el campo.

    El registro ya pone en el mensaje la lista de nombres disponibles; lo que anade este
    envoltorio es **donde** esta el `type:` culpable, que es lo que hace falta para
    arreglarlo sin buscar por todo el fichero.
    """
    registro = registries.get(bloque)
    if registro is None:  # pragma: no cover - solo si alguien pasa registros incompletos
        raise PipelineError(f"{campo}.type: no hay ningun registro para el bloque {bloque!r}")
    try:
        return registro.get(nombre)
    except UnknownTypeError as exc:
        raise UnknownTypeError(f"{campo}.type: {exc}") from exc


def _construir(clase: type[Any], cfg: Any, campo: str, **extras: Any) -> Any:
    """Construye un bloque con su propio `from_config`.

    `from_config` no esta en el `Protocol` —los protocolos describen como se **usa** un
    bloque, no como se fabrica— pero es la convencion de todos los adaptadores. Si falta,
    el error dice que clase es y que le falta, en vez de un `AttributeError` pelado.
    """
    fabrica = getattr(clase, "from_config", None)
    if not callable(fabrica):
        raise PipelineError(
            f"{campo}: {clase.__module__}.{clase.__qualname__} no tiene from_config(cfg), "
            "que es como la capa de configuracion construye un bloque"
        )
    return fabrica(cfg, **extras)
