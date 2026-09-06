"""El esquema: `project`, `run`, `event`, `metric` y `metric_minute`.

Cinco tablas y una sola capa de modelos para SQLite en local y Postgres en Docker, que es
la decision de stack de `_docs/plan.md`. **Aqui no hay SQL de ningun dialecto**: ni un
`json_extract`, ni un `strftime`, ni un `INSERT OR REPLACE`. Lo unico que cambia entre las
dos bases es la URL, y lo especifico de SQLite (WAL, claves ajenas) vive en `engine.py`,
donde se configura la conexion y no el esquema.

Tres decisiones que explican la forma de las tablas:

**1. Los eventos y las metricas se guardan en la forma canonica de #19.** El contenido
libre —`attributes` de un evento, `labels` de una metrica— se escribe con
`event_to_dict` / `metric_to_dict` y se lee con `event_from_dict` / `metric_from_dict`. No
hay una segunda forma que mantener sincronizada: lo que sale de la base vuelve a dar el
mismo `Event` o la misma `Metric`, y hay un test que lo comprueba. Lo que se saca a
columnas propias (`analyzer`, `name`, `timestamp`, `value`) es exactamente lo que se
consulta y se filtra; lo demas va como JSON en una columna de texto.

**2. Los timestamps son columnas de fecha en UTC, no texto.** El texto ISO-8601 ordena mal
en cuanto conviven marcas con y sin microsegundos (`...:18Z` es mayor que `...:18.5Z` en
orden alfabetico), y el orden temporal es justo lo que la API va a pedir. SQLite no guarda
la zona horaria en una columna `DATETIME`, asi que al leer se vuelve a marcar como UTC:
`cvp.core` rechaza un `datetime` sin zona, y esa puerta trasera no se abre desde aqui.

**3. `metric_minute` cuelga del proyecto, no de la ejecucion.** Es lo que hace posible el
criterio "reescribir un minuto acumula, no reemplaza": un worker que se reinicia estrena
`run_id`, y si el agregado fuese por ejecucion nunca reescribiria nada —tendria dos filas
para el mismo minuto y la grafica del dashboard se partiria en dos justo donde hubo una
caida—. Los datos crudos (`event`, `metric`) si van por ejecucion, que es donde importa
saber quien los escribio.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

from cvp.core import Event, Metric, event_from_dict, event_to_dict, metric_from_dict, metric_to_dict

__all__ = [
    "EventRow",
    "MetricMinute",
    "MetricRow",
    "Project",
    "Run",
    "clave_de_labels",
    "en_utc",
]


def _columna_de_fecha() -> Column[Any]:
    """Una columna de fecha con zona.

    En Postgres es un `timestamptz` de verdad; en SQLite el driver guarda el texto y
    devuelve un `datetime` naive, que `en_utc` vuelve a marcar al leer. Es una funcion y no
    una constante porque una instancia de `Column` no se puede compartir entre dos tablas.
    """
    return Column(DateTime(timezone=True), nullable=False)


def en_utc(marca: datetime) -> datetime:
    """La misma marca, con zona y en UTC.

    Todo lo que se escribe va en UTC, asi que una marca sin zona **es** UTC: la zona la
    perdio el driver de SQLite, no el dato. Es el unico sitio donde se asume eso.
    """
    if marca.tzinfo is None:
        return marca.replace(tzinfo=UTC)
    return marca.astimezone(UTC)


def clave_de_labels(labels: dict[str, str]) -> str:
    """Las etiquetas como JSON con las claves ordenadas.

    Ordenar no es cosmetico: es lo que hace que `{"a": "1", "b": "2"}` y
    `{"b": "2", "a": "1"}` sean el mismo minuto y no dos, porque el texto resultante forma
    parte de la clave unica de `metric_minute`. Sin separadores compactos el mismo mapa
    daria dos textos distintos segun quien lo escribiera.
    """
    return json.dumps(dict(labels), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Project(SQLModel, table=True):
    """Un proyecto del YAML. El `id` es el mismo `id:` del `project.yaml`."""

    __tablename__ = "project"

    id: str = Field(primary_key=True)
    name: str
    # La primera vez que se vio el proyecto. Sale del `run_id` de su primera ejecucion, no
    # del reloj: en este modulo no se consulta la hora.
    created_at: datetime = Field(sa_column=_columna_de_fecha())


class Run(SQLModel, table=True):
    """Una ejecucion del worker. El `id` es el `run_id` canonico de `cvp.storage.run_id`.

    `ended_at` es **el instante de la ultima muestra escrita**, no el del apagado: nadie
    garantiza que un worker llegue a cerrar (un corte de luz no avisa), y lo que se quiere
    saber al mirar una ejecucion caida es hasta cuando hubo datos. Es `None` mientras no se
    haya escrito ninguna muestra.

    Ojo con las dos bases de tiempo: `started_at` es reloj de pared (sale del `run_id`) y
    `ended_at` es el tiempo de las muestras. Con una fuente en directo coinciden; con un
    fichero, que reproduce tiempo de video, `ended_at` puede quedar incluso antes que
    `started_at` si arrancar el modelo tardo mas que el trozo de video procesado. No es un
    error de la fila: es que un video no pasa a la misma velocidad que el reloj.
    """

    __tablename__ = "run"

    id: str = Field(primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    started_at: datetime = Field(sa_column=_columna_de_fecha())
    ended_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class EventRow(SQLModel, table=True):
    """Algo que paso en un instante, en la forma canonica de #19.

    `attributes` es el JSON de `event_to_dict(evento)["attributes"]`, tal cual. Se guarda
    como texto y no como columnas porque cada analizador mete lo suyo: sacar a columnas lo
    que cambia con cada analizador obligaria a migrar el esquema cada vez que aparece uno.
    """

    __tablename__ = "event"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="run.id", index=True)
    analyzer: str = Field(index=True)
    name: str = Field(index=True)
    timestamp: datetime = Field(sa_column=_columna_de_fecha())
    attributes: str

    @classmethod
    def from_event(cls, run_id: str, evento: Event) -> EventRow:
        datos = event_to_dict(evento)
        return cls(
            run_id=run_id,
            analyzer=datos["analyzer"],
            name=datos["name"],
            timestamp=evento.timestamp,
            attributes=json.dumps(datos["attributes"], sort_keys=True, ensure_ascii=False),
        )

    def as_dict(self) -> dict[str, Any]:
        """La fila en la forma canonica de #19, lista para `event_from_dict` o para la API."""
        return {
            "analyzer": self.analyzer,
            "name": self.name,
            "timestamp": en_utc(self.timestamp).isoformat(),
            "attributes": json.loads(self.attributes),
        }

    def to_event(self) -> Event:
        """El mismo `Event` que entro. Es el viaje de vuelta que fija el test de ida y vuelta."""
        return event_from_dict(self.as_dict())


class MetricRow(SQLModel, table=True):
    """Un valor medido en un instante, en la forma canonica de #19.

    `labels` es el JSON ordenado de las etiquetas: las dimensiones de la medida
    (`{"direction": "norte"}`) van aparte del `name` para poder preguntar por sentido sin
    inventar nombres como `vehicles_per_minute_norte`, que es la decision de #2.
    """

    __tablename__ = "metric"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="run.id", index=True)
    analyzer: str = Field(index=True)
    name: str = Field(index=True)
    value: float
    timestamp: datetime = Field(sa_column=_columna_de_fecha())
    labels: str

    @classmethod
    def from_metric(cls, run_id: str, metrica: Metric) -> MetricRow:
        datos = metric_to_dict(metrica)
        return cls(
            run_id=run_id,
            analyzer=datos["analyzer"],
            name=datos["name"],
            value=datos["value"],
            timestamp=metrica.timestamp,
            labels=clave_de_labels(datos["labels"]),
        )

    def as_dict(self) -> dict[str, Any]:
        """La fila en la forma canonica de #19."""
        return {
            "analyzer": self.analyzer,
            "name": self.name,
            "value": self.value,
            "timestamp": en_utc(self.timestamp).isoformat(),
            "labels": json.loads(self.labels),
        }

    def to_metric(self) -> Metric:
        return metric_from_dict(self.as_dict())


class MetricMinute(SQLModel, table=True):
    """El agregado de un minuto para un `(analyzer, name, labels)` de un proyecto.

    Guarda `samples`, `sum_value`, `min_value` y `max_value`; la media se lee de
    `mean_value`, que se recalcula en cada acumulacion como `sum_value / samples`. La suma
    esta ademas de la media porque es lo unico que permite **acumular**: sin ella, sumar un
    lote nuevo a un minuto que ya existia obligaria a promediar medias, que es falso en
    cuanto los dos trozos tienen distinto numero de muestras.

    **`samples` es informativo, nunca un divisor de tiempo.** Desde #23 la tasa efectiva se
    adapta a lo que el analisis sostiene (QA midio 5,0 -> 2,74 -> 5,0 fps en una misma
    ejecucion), asi que el numero de muestras de un minuto no es constante y no dice cuanto
    duro el minuto. Lo que dice el tiempo es el `minute`, que sale del timestamp de cada
    muestra. Ver `cvp.storage.aggregation`.
    """

    __tablename__ = "metric_minute"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "minute",
            "analyzer",
            "name",
            "labels",
            name="uq_metric_minute",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    project_id: str = Field(foreign_key="project.id", index=True)
    minute: datetime = Field(sa_column=_columna_de_fecha())
    analyzer: str
    name: str
    labels: str
    samples: int
    sum_value: float
    mean_value: float
    min_value: float
    max_value: float
