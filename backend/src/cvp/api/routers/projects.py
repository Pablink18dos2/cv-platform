"""`GET /projects` y `GET /projects/{id}`: la lista, y un proyecto con su ultima ejecucion.

## El `status`

Se deriva de la ultima `run` y del reloj inyectado, en este orden:

1. `running`: la ejecucion ha escrito en los ultimos `UMBRAL_RUNNING` (90 s). "Ha
   escrito" es `run.ended_at`, que desde #9 es **el instante de la ultima muestra**, no
   el del apagado.
2. `stopped`: tiene `ended_at`, pero de hace mas de 90 s. Escribio y dejo de escribir;
   la API no distingue un Ctrl+C de un `kill -9` ni de una fuente caida diez minutos,
   porque el worker no escribe su salud en ningun sitio (fuera del alcance de #12).
3. `stale`: ni lo uno ni lo otro. La ejecucion existe pero no ha escrito ninguna
   muestra: esta cargando el modelo, o la fuente nunca abrio. Tambien un proyecto sin
   ejecuciones, que con el esquema de #9 no deberia darse (la fila del proyecto nace con
   la de su primera ejecucion, en la misma transaccion).

Con una fuente de fichero, `ended_at` es tiempo de video y no de pared, asi que un
proyecto que reproduce un fichero de 2024 sale `stopped` aunque el worker este corriendo.
Es la limitacion que documenta `Run` y no se disimula aqui.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter

from cvp.api.deps import Ahora, Proyecto, Sesion
from cvp.api.errors import RESPUESTAS_DE_ERROR, ErrorResponse
from cvp.api.queries import analizadores_de, listar_proyectos, ultima_run_de
from cvp.api.schemas import (
    ProjectDetail,
    ProjectList,
    ProjectStatus,
    ProjectSummary,
    RunInfo,
)
from cvp.storage import Run, en_utc

__all__ = ["UMBRAL_RUNNING", "estado_de", "router"]

UMBRAL_RUNNING = timedelta(seconds=90)

router = APIRouter(tags=["projects"], responses=RESPUESTAS_DE_ERROR)


def estado_de(ejecucion: Run | None, ahora: datetime) -> ProjectStatus:
    """`running` / `stopped` / `stale`, con el reloj que se le pasa."""
    if ejecucion is None or ejecucion.ended_at is None:
        return "stale"
    ultima_escritura = en_utc(ejecucion.ended_at)
    if ahora - ultima_escritura <= UMBRAL_RUNNING:
        return "running"
    return "stopped"


def _resumen(session: Sesion, project_id: str, name: str, ahora: datetime) -> ProjectSummary:
    ejecucion = ultima_run_de(session, project_id)
    return ProjectSummary(
        id=project_id,
        name=name,
        status=estado_de(ejecucion, ahora),
        last_seen_at=en_utc(ejecucion.ended_at) if ejecucion and ejecucion.ended_at else None,
    )


@router.get(
    "/projects",
    operation_id="list_projects",
    response_model=ProjectList,
    summary="Los proyectos que han escrito algo",
    description=(
        "Un proyecto aparece en cuanto su worker escribe el primer lote. Con la base "
        "vacia, la lista esta vacia: no es un error."
    ),
)
def listar(session: Sesion, ahora: Ahora) -> ProjectList:
    return ProjectList(
        projects=[_resumen(session, p.id, p.name, ahora) for p in listar_proyectos(session)]
    )


@router.get(
    "/projects/{id}",
    operation_id="get_project",
    response_model=ProjectDetail,
    summary="Un proyecto, con su ultima ejecucion y sus analizadores",
    responses={404: {"model": ErrorResponse, "description": "`project_not_found`."}},
)
def detalle(proyecto: Proyecto, session: Sesion, ahora: Ahora) -> ProjectDetail:
    ejecucion = ultima_run_de(session, proyecto.id)
    resumen = _resumen(session, proyecto.id, proyecto.name, ahora)
    return ProjectDetail(
        **resumen.model_dump(),
        created_at=en_utc(proyecto.created_at),
        last_run=(
            RunInfo(
                id=ejecucion.id,
                started_at=en_utc(ejecucion.started_at),
                ended_at=en_utc(ejecucion.ended_at) if ejecucion.ended_at else None,
            )
            if ejecucion is not None
            else None
        ),
        analyzers=analizadores_de(session, proyecto.id),
    )
