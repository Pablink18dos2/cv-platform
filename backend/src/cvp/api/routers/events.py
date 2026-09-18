"""`GET /projects/{id}/events`: los eventos, en la forma canonica, paginados hacia atras.

Cada evento es exactamente `event_to_dict(evento)` de `cvp.core.serialization` (#19) mas
`run_id`. No hay una segunda forma: lo que sale de aqui es lo que escribe el sink `jsonl`,
y `event_from_dict` lo vuelve a leer.

## La paginacion

`limit` (hasta 500) y `before`, un cursor por timestamp **exclusivo**: la pagina
siguiente se pide con `before=<next_before de la anterior>`, y `next_before` es `null`
cuando no hay mas. Los eventos van por timestamp descendente, los mas recientes primero.

Un cursor por timestamp tiene un borde: dos vehiculos que cruzan en el mismo fotograma
producen dos eventos **con el mismo timestamp**, y si la pagina cortara entre los dos, el
segundo se perderia (`timestamp < before` lo excluye) o se repetiria (`<=` lo traeria
otra vez). Lo que se hace es **no cortar nunca un timestamp**: si el ultimo evento de la
pagina comparte timestamp con el siguiente, la pagina se alarga hasta incluir todos los
que lo comparten. Una pagina puede pasar de `limit` en unos pocos eventos —los de un
fotograma— y a cambio recorrer las paginas no repite ni salta ninguno.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from cvp.api.deps import Proyecto, Sesion
from cvp.api.errors import RESPUESTAS_DE_ERROR, ApiError, ErrorResponse
from cvp.api.queries import eventos_con_timestamp, pagina_de_eventos
from cvp.api.schemas import EventOut, EventsResponse
from cvp.api.ventana import iso
from cvp.core import event_to_dict
from cvp.storage import EventRow, en_utc

__all__ = ["LIMITE_MAXIMO", "LIMITE_POR_DEFECTO", "router"]

LIMITE_POR_DEFECTO = 100
LIMITE_MAXIMO = 500

router = APIRouter(
    tags=["events"],
    responses={
        **RESPUESTAS_DE_ERROR,
        404: {"model": ErrorResponse, "description": "`project_not_found`."},
    },
)

_DESCRIPCION_FECHA = "ISO-8601 con zona (`2026-08-24T14:20:00Z`)."


def _canonico(fila: EventRow) -> EventOut:
    datos = event_to_dict(fila.to_event())
    return EventOut(**datos, run_id=fila.run_id)


@router.get(
    "/projects/{id}/events",
    operation_id="list_events",
    response_model=EventsResponse,
    summary="Eventos por timestamp descendente, paginados con `before`",
    responses={400: {"model": ErrorResponse, "description": "`invalid_range`."}},
)
def eventos(
    proyecto: Proyecto,
    session: Sesion,
    analyzer: Annotated[str | None, Query(description="El `name` del analizador.")] = None,
    name: Annotated[str | None, Query(description="`line_crossing`, `space_freed`...")] = None,
    desde: Annotated[
        AwareDatetime | None, Query(alias="from", description=_DESCRIPCION_FECHA + " Incluido.")
    ] = None,
    hasta: Annotated[
        AwareDatetime | None, Query(alias="to", description=_DESCRIPCION_FECHA + " Incluido.")
    ] = None,
    before: Annotated[
        AwareDatetime | None,
        Query(description="Cursor: solo eventos **anteriores** a este instante (exclusivo)."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=LIMITE_MAXIMO, description=f"Eventos por pagina, hasta {LIMITE_MAXIMO}."),
    ] = LIMITE_POR_DEFECTO,
) -> EventsResponse:
    if desde is not None and hasta is not None and desde > hasta:
        raise ApiError(
            400,
            "invalid_range",
            f"from ({iso(desde)}) es posterior a to ({iso(hasta)})",
        )
    # Se pide uno mas que `limit`: es lo que dice si hay pagina siguiente y si esa pagina
    # empezaria partiendo un fotograma.
    filas = pagina_de_eventos(
        session,
        proyecto.id,
        analyzer=analyzer,
        name=name,
        desde=desde,
        hasta=hasta,
        before=before,
        limite=limit + 1,
    )
    if len(filas) <= limit:
        return EventsResponse(
            project_id=proyecto.id, events=[_canonico(f) for f in filas], next_before=None
        )
    pagina, siguiente = filas[:limit], filas[limit]
    if siguiente.timestamp == pagina[-1].timestamp:
        assert siguiente.id is not None
        pagina.append(siguiente)
        pagina.extend(
            eventos_con_timestamp(
                session,
                proyecto.id,
                analyzer=analyzer,
                name=name,
                timestamp=en_utc(siguiente.timestamp),
                antes_del_id=siguiente.id,
            )
        )
    return EventsResponse(
        project_id=proyecto.id,
        events=[_canonico(f) for f in pagina],
        next_before=en_utc(pagina[-1].timestamp),
    )
