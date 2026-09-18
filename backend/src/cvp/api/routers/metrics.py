"""`GET /projects/{id}/metrics` y `/metrics/latest`: las series por minuto y el ultimo valor.

## Lo que la serie hace con los cuatro bordes de `metric_minute`

Estan enumerados y medidos en #12 (vienen de #10 y #29) y `_docs/api.md` los explica al
que va a pintar la grafica. Lo que se hace aqui:

1. **Un minuto sin fila sale como `null`.** La respuesta es una rejilla regular, un punto
   por minuto de `[from, to]`, y el hueco va explicito en vez de ausente. Un sentido sin
   cruces no tiene fila (#10): la API no sabe si ese `null` es un 0 (para un conteo) o
   "sin dato" (para un gauge), y no lo adivina; lo deja al frontend, que si sabe que
   metrica esta pintando.
2. **Se devuelven todas las columnas.** `sum_value` es el conteo de una metrica de conteo
   y `mean_value` el valor de un gauge, y elegir por nombre de metrica acoplaria la API a
   los analizadores. Cada punto lleva `samples`, `seconds`, `sum_value`, `mean_value`,
   `min_value`, `max_value` e `integral`.
3. **`samples == 0` es un hueco, no un dato.** La fila fantasma de #29 trae una media
   inventada; se filtra en la consulta y sale como `null`.
4. **`seconds` se sirve tal cual, y puede pasar de 60** (#29): dos ejecuciones sobre el
   mismo minuto suman. No se recorta ni se normaliza; se documenta.

Una serie por `(analyzer, name, labels)` que tenga **algun** dato en la ventana; una serie
sin ninguna fila en la ventana no aparece, porque la API no tiene de donde sacar sus
etiquetas. Es otra forma del borde 1: el frontend que espera "norte" y "sur" y solo
recibe "norte" tiene que pintar el "sur" a cero el mismo.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from cvp.api.deps import Ahora, Proyecto, Sesion
from cvp.api.errors import RESPUESTAS_DE_ERROR, ApiError, ErrorDetail, ErrorResponse
from cvp.api.queries import minutos_en_rango, ultimos_minutos
from cvp.api.schemas import LatestPoint, LatestResponse, MinutePoint, Series, SeriesResponse
from cvp.api.ventana import ventana_de
from cvp.storage import MetricMinute, en_utc

__all__ = ["parsear_labels", "router"]

router = APIRouter(
    tags=["metrics"],
    responses={
        **RESPUESTAS_DE_ERROR,
        404: {"model": ErrorResponse, "description": "`project_not_found`."},
    },
)

_DESCRIPCION_LABELS = (
    "Filtro por igualdad de etiquetas, `clave:valor` separados por comas: "
    "`direction:norte` o `line:principal,direction:norte`. Las claves que no se dan "
    "no se miran."
)
_DESCRIPCION_FECHA = (
    "ISO-8601 **con zona**: `2026-08-24T14:20:00Z`. Un `+` en la zona hay que "
    "codificarlo (`%2B`); mejor `Z`."
)

Analyzer = Annotated[str | None, Query(description="El `name` del analizador en el YAML.")]
Name = Annotated[str | None, Query(description="El nombre de la metrica: `crossings`...")]
Labels = Annotated[str | None, Query(description=_DESCRIPCION_LABELS)]
Desde = Annotated[AwareDatetime | None, Query(alias="from", description=_DESCRIPCION_FECHA)]
Hasta = Annotated[AwareDatetime | None, Query(alias="to", description=_DESCRIPCION_FECHA)]
Step = Annotated[Literal["minute"], Query(description="Solo `minute` por ahora.")]


def parsear_labels(crudo: str | None) -> dict[str, str]:
    """`"line:principal,direction:norte"` -> `{"line": "principal", "direction": "norte"}`."""
    if crudo is None or not crudo.strip():
        return {}
    etiquetas: dict[str, str] = {}
    for trozo in crudo.split(","):
        clave, separador, valor = trozo.partition(":")
        if not separador or not clave.strip():
            raise ApiError(
                422,
                "validation_error",
                "labels tiene que ser clave:valor separados por comas",
                details=[
                    ErrorDetail(
                        field="labels",
                        message=f"{trozo!r} no tiene la forma clave:valor",
                    )
                ],
            )
        etiquetas[clave.strip()] = valor.strip()
    return etiquetas


def _punto(fila: MetricMinute) -> MinutePoint:
    return MinutePoint(
        minute=en_utc(fila.minute),
        samples=fila.samples,
        seconds=fila.seconds,
        sum_value=fila.sum_value,
        mean_value=fila.mean_value,
        min_value=fila.min_value,
        max_value=fila.max_value,
        integral=fila.integral,
    )


def _series(filas: list[MetricMinute], rejilla: list[datetime]) -> list[Series]:
    por_serie: dict[tuple[str, str, str], dict[datetime, MetricMinute]] = defaultdict(dict)
    for fila in filas:
        por_serie[(fila.analyzer, fila.name, fila.labels)][en_utc(fila.minute)] = fila
    return [
        Series(
            analyzer=analyzer,
            name=name,
            labels=json.loads(labels),
            points=[_punto(minutos[m]) if m in minutos else None for m in rejilla],
        )
        for (analyzer, name, labels), minutos in por_serie.items()
    ]


@router.get(
    "/projects/{id}/metrics",
    operation_id="get_metrics",
    response_model=SeriesResponse,
    response_model_by_alias=True,
    summary="Series por minuto, regulares y con `null` en los huecos",
    responses={400: {"model": ErrorResponse, "description": "`invalid_range`."}},
)
def series(
    proyecto: Proyecto,
    session: Sesion,
    ahora: Ahora,
    analyzer: Analyzer = None,
    name: Name = None,
    labels: Labels = None,
    desde: Desde = None,
    hasta: Hasta = None,
    step: Step = "minute",
) -> SeriesResponse:
    ventana = ventana_de(desde, hasta, ahora=ahora)
    filas = minutos_en_rango(
        session,
        proyecto.id,
        desde=ventana.desde,
        hasta=ventana.hasta,
        analyzer=analyzer,
        name=name,
        labels=parsear_labels(labels),
    )
    return SeriesResponse(
        project_id=proyecto.id,
        from_=ventana.desde,
        to=ventana.hasta,
        step=step,
        series=_series(filas, ventana.minutos),
    )


@router.get(
    "/projects/{id}/metrics/latest",
    operation_id="get_latest_metrics",
    response_model=LatestResponse,
    summary="El ultimo minuto con muestras de cada serie: los KPIs",
)
def ultimos(proyecto: Proyecto, session: Sesion) -> LatestResponse:
    return LatestResponse(
        project_id=proyecto.id,
        latest=[
            LatestPoint(
                **_punto(fila).model_dump(),
                analyzer=fila.analyzer,
                name=fila.name,
                labels=json.loads(fila.labels),
            )
            for fila in ultimos_minutos(session, proyecto.id)
        ],
    )
