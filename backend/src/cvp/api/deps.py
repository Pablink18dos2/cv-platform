"""Lo que comparten los routers: la sesion, el reloj y "el proyecto o 404".

`ID_DE_PROYECTO` es el mismo patron que `cvp.pipeline.config` exige al `id:` del YAML.
Se comprueba antes de mirar la base y antes de tocar el disco: un `{id}` con otra forma
no puede ser un proyecto, y como el snapshot deriva una ruta de el, tampoco puede llevar
un `..` ni una barra invertida hasta `data/preview`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Path
from sqlmodel import Session

from cvp.api.db import sesion
from cvp.api.errors import ApiError
from cvp.api.queries import proyecto_por_id
from cvp.api.reloj import ahora
from cvp.storage import Project

__all__ = [
    "ID_DE_PROYECTO",
    "Ahora",
    "IdDeProyecto",
    "Proyecto",
    "Sesion",
    "id_valido_o_404",
    "proyecto_o_404",
]

ID_DE_PROYECTO = re.compile(r"^[a-z0-9][a-z0-9-]*$")

Sesion = Annotated[Session, Depends(sesion)]
Ahora = Annotated[datetime, Depends(ahora)]


def _no_existe(project_id: str) -> ApiError:
    return ApiError(404, "project_not_found", f"no existe ningun proyecto con id {project_id!r}")


def id_valido_o_404(
    id: Annotated[
        str,
        Path(
            description="El `id:` del `project.yaml`: minusculas, digitos y guiones.",
            examples=["01-parking-trafico"],
        ),
    ],
) -> str:
    if not ID_DE_PROYECTO.match(id):
        raise _no_existe(id)
    return id


IdDeProyecto = Annotated[str, Depends(id_valido_o_404)]


def proyecto_o_404(id: IdDeProyecto, session: Sesion) -> Project:
    proyecto = proyecto_por_id(session, id)
    if proyecto is None:
        raise _no_existe(id)
    return proyecto


Proyecto = Annotated[Project, Depends(proyecto_o_404)]
