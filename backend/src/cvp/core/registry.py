"""El registro por nombre: lo que convierte un `type:` del YAML en una clase.

Un bloque nuevo se anade con un fichero nuevo y un decorador. No hay un `match` gigante
que haya que editar en el pipeline, que es justo lo que haria que anadir un analizador
obligara a tocar el nucleo.

`get` devuelve **la clase**, no una instancia: construirla con su trozo de configuracion
es trabajo de la fabrica (issue 3), no del registro.

No lleva locks: registrar ocurre en tiempo de importacion y en un solo hilo. El dia que
haya que registrar en caliente se replantea entonces, no antes.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from cvp.core.errors import DuplicateRegistrationError, UnknownTypeError
from cvp.core.protocols import Analyzer, Detector, Sink, Tracker, VideoSource

__all__ = [
    "ANALYZERS",
    "DETECTORS",
    "Registry",
    "SINKS",
    "SOURCES",
    "TRACKERS",
    "register_analyzer",
    "register_detector",
    "register_sink",
    "register_source",
    "register_tracker",
]

# Los nombres son identificadores de YAML y acaban en URLs y en columnas de la base de
# datos, asi que se restringen desde el principio: minusculas, digitos y guion bajo.
# `youtube_live` si, `youtube-live` y `YOLO` no.
_NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9_]*$")


def _ruta(cls: type) -> str:
    """`modulo.ClaseCualificada`, para que el error diga donde estan las dos clases."""
    return f"{cls.__module__}.{cls.__qualname__}"


class Registry[T]:
    """Los nombres registrados de un tipo de bloque.

    Cada bloque tiene el suyo y son independientes: registrar `"x"` como fuente no hace
    que `DETECTORS.get("x")` funcione.
    """

    def __init__(self, block: str) -> None:
        self._block = block
        self._entries: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        """Decorador que registra una clase bajo `name` y la devuelve **sin tocarla**.

        Valida el nombre al construir el decorador, es decir antes de que se ejecute el
        cuerpo de la clase decorada, para que un nombre invalido falle al importar.
        """
        if not isinstance(name, str):
            raise TypeError(f"el nombre de un {self._block} debe ser str, no {type(name).__name__}")
        if not _NOMBRE_VALIDO.match(name):
            raise ValueError(
                f"nombre de {self._block} invalido: {name!r}. "
                "Debe encajar en ^[a-z][a-z0-9_]*$ (minusculas, digitos y guion bajo)"
            )

        def decorador(cls: type[T]) -> type[T]:
            if not isinstance(cls, type):
                raise TypeError(
                    f"solo se pueden registrar clases como {self._block}, "
                    f"y {name!r} recibio un {type(cls).__name__}"
                )
            registrada = self._entries.get(name)
            if registrada is not None and registrada is not cls:
                raise DuplicateRegistrationError(
                    f"ya hay un {self._block} registrado con el nombre {name!r}: "
                    f"{_ruta(registrada)}; no se puede registrar tambien {_ruta(cls)}"
                )
            # Reregistrar la misma clase con el mismo nombre es idempotente a proposito:
            # un modulo reimportado (pytest lo hace) no debe tumbar la suite.
            self._entries[name] = cls
            return cls

        return decorador

    def get(self, name: str) -> type[T]:
        """Devuelve la clase registrada, o `UnknownTypeError` con las alternativas."""
        registrada = self._entries.get(name)
        if registrada is None:
            raise UnknownTypeError(self._mensaje_desconocido(name))
        return registrada

    def names(self) -> tuple[str, ...]:
        """Los nombres registrados, ordenados alfabeticamente."""
        return tuple(sorted(self._entries))

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def _mensaje_desconocido(self, name: str) -> str:
        """El error que ve quien escribe `type: yolo11` en el YAML, asi que lleva la
        lista de lo que si existe."""
        if not self._entries:
            return (
                f"no hay ningun {self._block} llamado {name!r}: "
                f"no hay ningun {self._block} registrado todavia"
            )
        return (
            f"no hay ningun {self._block} llamado {name!r}. Disponibles: {', '.join(self.names())}"
        )


SOURCES: Registry[VideoSource] = Registry("source")
DETECTORS: Registry[Detector] = Registry("detector")
TRACKERS: Registry[Tracker] = Registry("tracker")
ANALYZERS: Registry[Analyzer] = Registry("analyzer")
SINKS: Registry[Sink] = Registry("sink")

# Los decoradores son el metodo `register` de cada registro. Alias en vez de cinco
# funciones envoltorio identicas: una envoltura solo podria quedarse desincronizada.
register_source = SOURCES.register
register_detector = DETECTORS.register
register_tracker = TRACKERS.register
register_analyzer = ANALYZERS.register
register_sink = SINKS.register
