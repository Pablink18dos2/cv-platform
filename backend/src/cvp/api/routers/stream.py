"""`GET /projects/{id}/snapshot.jpg` y `/stream.mjpg`: el ultimo fotograma, y el siguiente.

Los dos sirven `data/preview/<id>.jpg`, el fichero que escribe el sink `preview` de
forma atomica (#9). Ninguno lo mantiene abierto: se lee entero, se cierra y se responde
con la copia (`cvp.api.preview` explica por que, es #27). Ninguno toca la base de datos:
el fichero existe o no, y con eso basta; un `{id}` que no tenga forma de id de proyecto
es 404 antes de mirar el disco.

- **`snapshot.jpg`** es una imagen normal con `Cache-Control: no-store`: un `<img>` que
  se recarga cada pocos segundos no puede recibir la de hace un minuto de la cache.
- **`stream.mjpg`** es `multipart/x-mixed-replace`: la misma imagen, reemplazada cada vez
  que el fichero cambia. Se mira cada `stream_interval_s` (250 ms) y **solo se emite si
  ha cambiado** (`mtime` y tamano): con el worker a 5 fps salen 5 fotogramas por
  segundo, no 4 copias del mismo. La respuesta no termina nunca por su cuenta; termina
  cuando el cliente cierra, que es como funciona un `<img src="...stream.mjpg">`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from cvp.api.deps import IdDeProyecto
from cvp.api.errors import RESPUESTAS_DE_ERROR, ApiError, ErrorResponse
from cvp.api.preview import fotogramas_nuevos, leer_fotograma, ruta_de_preview

__all__ = ["FRONTERA", "router"]

router = APIRouter(
    tags=["stream"],
    responses={
        **RESPUESTAS_DE_ERROR,
        404: {"model": ErrorResponse, "description": "`snapshot_not_found`."},
    },
)

FRONTERA = "cvp-frame"
_SIN_CACHE = {"Cache-Control": "no-store"}


def _sin_fotograma(project_id: str) -> ApiError:
    return ApiError(
        404,
        "snapshot_not_found",
        f"el proyecto {project_id!r} no tiene todavia ningun fotograma en disco",
    )


@router.get(
    "/projects/{id}/snapshot.jpg",
    operation_id="get_snapshot",
    summary="El ultimo fotograma, como JPEG",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}, "description": "El ultimo fotograma."}},
)
def snapshot(id: IdDeProyecto, request: Request) -> Response:
    fotograma = leer_fotograma(ruta_de_preview(id, base=request.app.state.settings.preview_dir))
    if fotograma is None:
        raise _sin_fotograma(id)
    return Response(content=fotograma.datos, media_type="image/jpeg", headers=_SIN_CACHE)


def _parte(datos: bytes) -> bytes:
    cabecera = (
        f"--{FRONTERA}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(datos)}\r\n\r\n"
    ).encode("ascii")
    return cabecera + datos + b"\r\n"


async def _multipart(ruta: Path, intervalo_s: float) -> AsyncIterator[bytes]:
    async for datos in fotogramas_nuevos(ruta, intervalo_s=intervalo_s):
        yield _parte(datos)


@router.get(
    "/projects/{id}/stream.mjpg",
    operation_id="get_stream",
    summary="El fotograma en directo, como MJPEG",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {f"multipart/x-mixed-replace; boundary={FRONTERA}": {}},
            "description": "Un JPEG por cada cambio del fichero. No termina.",
        }
    },
)
def stream(id: IdDeProyecto, request: Request) -> StreamingResponse:
    ajustes = request.app.state.settings
    ruta = ruta_de_preview(id, base=ajustes.preview_dir)
    if leer_fotograma(ruta) is None:
        # Sin fotograma no hay nada que emitir todavia: 404 ahora, y el cliente reintenta.
        raise _sin_fotograma(id)
    return StreamingResponse(
        _multipart(ruta, ajustes.stream_interval_s),
        media_type=f"multipart/x-mixed-replace; boundary={FRONTERA}",
        headers=_SIN_CACHE,
    )
