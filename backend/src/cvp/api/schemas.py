"""Las respuestas, como modelos de Pydantic: son lo que acaba en `openapi.yaml`.

Cada descripcion de campo se copia al contrato, asi que lo que va aqui es lo que el
cliente generado de #13 vera en su editor. La semantica larga —que columna de
`metric_minute` usar para que, y por que un `null` no es un cero— esta en `_docs/api.md`;
aqui solo la frase que evita el error mas probable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EventOut",
    "EventsResponse",
    "LatestPoint",
    "LatestResponse",
    "MinutePoint",
    "ProjectDetail",
    "ProjectList",
    "ProjectStatus",
    "ProjectSummary",
    "RunInfo",
    "Series",
    "SeriesResponse",
]

ProjectStatus = Literal["running", "stopped", "stale"]


class ProjectSummary(BaseModel):
    id: str
    name: str
    status: ProjectStatus = Field(
        description=(
            "Derivado de la ultima ejecucion: `running` si ha escrito en los ultimos 90 s, "
            "`stopped` si escribio alguna vez pero hace mas, `stale` si arranco y no ha "
            "escrito nada todavia (o no tiene ejecuciones)."
        )
    )
    last_seen_at: datetime | None = Field(
        description="El instante de la ultima muestra escrita por la ultima ejecucion."
    )


class ProjectList(BaseModel):
    projects: list[ProjectSummary]


class RunInfo(BaseModel):
    id: str = Field(description="El `run_id` canonico: `AAAAMMDDTHHMMSSZ-xxxxxxxx`.")
    started_at: datetime = Field(description="Reloj de pared, sacado del propio `run_id`.")
    ended_at: datetime | None = Field(
        description=(
            "El instante de la **ultima muestra escrita**, no del apagado: un worker que "
            "muere no cierra su ejecucion. `null` mientras no ha escrito nada."
        )
    )


class ProjectDetail(ProjectSummary):
    created_at: datetime
    last_run: RunInfo | None
    analyzers: list[str] = Field(
        description="Los `analyzer` que han escrito metricas por minuto en este proyecto."
    )


class MinutePoint(BaseModel):
    """Un minuto de `metric_minute`, con todas sus columnas. `_docs/api.md` dice cual usar."""

    minute: datetime = Field(description="El minuto UTC, truncado, al que pertenece el punto.")
    samples: int = Field(
        description="Muestras del minuto. Siempre > 0: una fila con 0 se sirve como `null`."
    )
    seconds: float = Field(
        description=(
            "Segundos del minuto cubiertos por muestras. **Puede pasar de 60** si dos "
            "ejecuciones cubrieron el mismo minuto; no es una fraccion de cobertura."
        )
    )
    sum_value: float = Field(
        description="Suma de los valores. Es el conteo de una metrica de conteo (`crossings`)."
    )
    mean_value: float = Field(
        description=(
            "Media **en el tiempo** (`integral / seconds`). Es el valor de un gauge "
            "(`vehicles_in_scene`, `zone_occupied`); no significa nada para un conteo."
        )
    )
    min_value: float
    max_value: float
    integral: float = Field(description="Suma de valor x segundos. `mean_value` sale de aqui.")


class Series(BaseModel):
    analyzer: str
    name: str
    labels: dict[str, str]
    points: list[MinutePoint | None] = Field(
        description=(
            "Un elemento por minuto de `[from, to]`, en orden. `null` es un minuto sin fila "
            "(o con `samples == 0`): para un conteo es un 0, para un gauge es 'sin dato'."
        )
    )


class SeriesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str
    from_: datetime = Field(alias="from", description="Primer minuto de la rejilla, incluido.")
    to: datetime = Field(description="Ultimo minuto de la rejilla, incluido.")
    step: Literal["minute"]
    series: list[Series] = Field(
        description="Una por `(analyzer, name, labels)` con datos en la ventana."
    )


class LatestPoint(MinutePoint):
    analyzer: str
    name: str
    labels: dict[str, str]


class LatestResponse(BaseModel):
    project_id: str
    latest: list[LatestPoint] = Field(
        description="El ultimo minuto con muestras de cada `(analyzer, name, labels)`."
    )


class EventOut(BaseModel):
    """Un evento en la forma canonica de `cvp.core.serialization`, mas su `run_id`."""

    analyzer: str
    name: str
    timestamp: datetime
    attributes: dict[str, Any]
    run_id: str


class EventsResponse(BaseModel):
    project_id: str
    events: list[EventOut] = Field(description="Por timestamp descendente.")
    next_before: datetime | None = Field(
        description=("Lo que pasar como `before` para la pagina siguiente. `null` si no hay mas.")
    )
