"""Donde esta el ultimo fotograma de un proyecto, y como se lee sin molestar al worker.

## Por que la convencion esta escrita dos veces

`cvp.sinks.preview.ruta_de_preview` es la funcion publica que fija la ruta
`data/preview/<id del proyecto>.jpg`, y se hizo publica "para que la API llame a la misma
funcion en vez de escribir la convencion por segunda vez". Pero importarla arrastra cv2:
`cvp.sinks.preview` lo importa para codificar, y `cvp.sinks.__init__` lo importa a el. La
API no puede pagar eso (`tests/unit/test_api_aislamiento.py`), y #12 prohibe tocar
`cvp.sinks`, asi que la convencion vive aqui **otra vez**, y lo que impide que las dos
copias se separen es `tests/unit/test_api_snapshot.py`: comprueba que las dos funciones
dan la misma ruta.

## Por que se lee entero y se cierra antes de responder

Es la mitigacion de #27. En Windows, el `os.replace` con el que el sink escribe el JPEG
falla con `PermissionError` si otro proceso tiene el fichero de destino abierto; QA midio
48 escrituras fallidas de 199 con un lector en bucle. Un `FileResponse` mantiene el
descriptor abierto mientras transmite, y el MJPEG lo tendria abierto casi siempre. Asi que
aqui el fichero se lee **de una vez** (`read_bytes`: abre, lee, cierra) y lo que se
transmite es la copia en memoria. Son ~60 KB por fotograma: no hay nada que ganar
transmitiendo desde el descriptor.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DIRECTORIO_PREVIEW",
    "Fotograma",
    "fotogramas_nuevos",
    "leer_fotograma",
    "ruta_de_preview",
]

# La misma que `cvp.sinks.preview.DIRECTORIO_PREVIEW`: `data/preview`, relativo a donde
# se lanza el proceso, al lado de la base de datos.
DIRECTORIO_PREVIEW = Path("data") / "preview"


def ruta_de_preview(project_id: str, *, base: Path | str = DIRECTORIO_PREVIEW) -> Path:
    """La misma ruta que `cvp.sinks.preview.ruta_de_preview`, sin importar cv2."""
    if not project_id:
        raise ValueError("un preview necesita el id del proyecto para saber donde leer")
    return Path(base) / f"{project_id}.jpg"


@dataclass(frozen=True, slots=True)
class Fotograma:
    """Un JPEG leido entero, con la version del fichero de la que salio.

    `version` es `(mtime_ns, tamano)`: cambia con cada `os.replace` del sink y es lo que
    permite al MJPEG no mandar el mismo fotograma cuatro veces por segundo.
    """

    datos: bytes
    version: tuple[int, int]


def leer_fotograma(ruta: Path) -> Fotograma | None:
    """El fichero entero, o `None` si no existe.

    Abre, lee y cierra: al devolver, no queda ningun descriptor sobre el fichero. Un
    `FileNotFoundError` a mitad —el sink acaba de reemplazarlo— cuenta como ausente y se
    vuelve a intentar en la siguiente vuelta, no como error.
    """
    try:
        estado = os.stat(ruta)
        datos = ruta.read_bytes()
    except FileNotFoundError:
        return None
    return Fotograma(datos=datos, version=(estado.st_mtime_ns, estado.st_size))


def _version_de(ruta: Path) -> tuple[int, int] | None:
    try:
        estado = os.stat(ruta)
    except FileNotFoundError:
        return None
    return (estado.st_mtime_ns, estado.st_size)


async def fotogramas_nuevos(
    ruta: Path,
    *,
    intervalo_s: float,
    dormir: Callable[[float], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    """Cada fotograma **distinto del anterior** que aparece en `ruta`, sin terminar nunca.

    Mira el fichero cada `intervalo_s` y solo lo lee —y solo emite— cuando su version
    (`mtime` y tamano) ha cambiado. Entre lecturas no hay nada abierto. Si el fichero
    desaparece a mitad (el worker se ha parado), se sigue esperando: volvera cuando el
    worker vuelva. Es el que llama quien decide cuando parar, cerrando el generador; con
    una respuesta HTTP, eso pasa cuando el cliente se desconecta.

    `dormir` es `asyncio.sleep` salvo en los tests, que lo sustituyen por algo que no
    espera de verdad.
    """
    espera = asyncio.sleep if dormir is None else dormir
    ultima_version: tuple[int, int] | None = None
    while True:
        if _version_de(ruta) != ultima_version:
            fotograma = leer_fotograma(ruta)
            if fotograma is not None and fotograma.version != ultima_version:
                ultima_version = fotograma.version
                yield fotograma.datos
        await espera(intervalo_s)
