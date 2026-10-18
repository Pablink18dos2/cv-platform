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

## La carrera es en los dos sentidos

Leer entero reduce la ventana, no la cierra: QA midio en #12 que con un lector conectado
siguen fallando 1-8 % de los `os.replace` del sink a 5 fps. Y del otro lado, **abrir el
fichero mientras el sink lo reemplaza falla tambien**, y en Windows no es un
`FileNotFoundError` sino un `PermissionError` (`[Errno 13]`): QA vio morir el MJPEG en 4
de 8 ejecuciones por eso. Las dos excepciones significan lo mismo —"el sink esta en
ello"— y aqui se tratan igual: el stream espera a la vuelta siguiente y el snapshot
reintenta unas pocas veces (`leer_fotograma_con_reintentos`). La ventana del `os.replace`
es de microsegundos; lo que no puede pasar es que un choque con ella mate la respuesta.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DIRECTORIO_PREVIEW",
    "Fotograma",
    "PreviewOcupado",
    "fotogramas_nuevos",
    "leer_fotograma",
    "leer_fotograma_con_reintentos",
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


class PreviewOcupado(Exception):
    """El sink esta reemplazando el fichero en este instante. Se vuelve a intentar."""


def leer_fotograma(ruta: Path) -> Fotograma | None:
    """El fichero entero, o `None` si no existe. `PreviewOcupado` si el sink lo esta
    reemplazando ahora mismo.

    Abre, lee y cierra: al devolver, no queda ningun descriptor sobre el fichero. Un
    `FileNotFoundError` a mitad —el sink acaba de reemplazarlo— cuenta como ausente. Un
    `PermissionError` es la misma carrera vista desde Windows: el fichero existe, pero
    durante el `os.replace` no se deja abrir. Ni lo uno ni lo otro es un error: se vuelve
    a intentar, en la siguiente vuelta del stream o en el siguiente reintento del snapshot.
    """
    try:
        estado = os.stat(ruta)
        datos = ruta.read_bytes()
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise PreviewOcupado(str(exc)) from exc
    return Fotograma(datos=datos, version=(estado.st_mtime_ns, estado.st_size))


def leer_fotograma_con_reintentos(
    ruta: Path,
    *,
    intentos: int = 5,
    espera_s: float = 0.02,
    dormir: Callable[[float], None] = time.sleep,
) -> Fotograma | None:
    """`leer_fotograma`, insistiendo si el sink esta reemplazando el fichero.

    Es para el snapshot, que tiene que responder ahora: la ventana del `os.replace` es
    de microsegundos, asi que el segundo intento casi siempre basta y el peor caso son
    `intentos x espera_s` (100 ms). Si sigue ocupado, se rinde con `PreviewOcupado` y
    quien llama decide (el snapshot responde 503 y el cliente vuelve a pedir).
    """
    if intentos < 1:
        raise ValueError(f"intentos es un numero >= 1, no {intentos!r}")
    for intento in range(intentos):
        try:
            return leer_fotograma(ruta)
        except PreviewOcupado:
            if intento == intentos - 1:
                raise
            dormir(espera_s)
    raise AssertionError("inalcanzable")  # pragma: no cover


def _version_de(ruta: Path) -> tuple[int, int] | None:
    try:
        estado = os.stat(ruta)
    except (FileNotFoundError, PermissionError):
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
            try:
                fotograma = leer_fotograma(ruta)
            except PreviewOcupado:
                # El sink esta a mitad de `os.replace`: en la vuelta siguiente ya estara.
                fotograma = None
            if fotograma is not None and fotograma.version != ultima_version:
                ultima_version = fotograma.version
                yield fotograma.datos
        await espera(intervalo_s)
