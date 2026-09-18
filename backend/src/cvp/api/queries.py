"""Las consultas de lectura de la API. Van aqui y no en `cvp.storage.queries` (#12).

Todo es SQL portable a traves de SQLModel, como en `cvp.storage`: nada de `json_extract`
ni de `strftime`. Las etiquetas se filtran en Python sobre el JSON ordenado de
`clave_de_labels`, con un `contains` portable como prefiltro para no traer lo que
seguro no encaja.

Dos cosas que las consultas de metricas hacen siempre, porque son bordes medidos y no
teoricos (los cuatro de `metric_minute` estan en `_docs/api.md`):

- **`samples > 0`.** Una fila con `samples == 0` es el tramo de una muestra que se perdio
  en un lote descartado (#29): trae una media inventada y no es un dato. No se sirve.
- **Todas las columnas.** La API no elige `sum_value` o `mean_value` por el nombre de la
  metrica: eso la acoplaria a los analizadores. Devuelve todas y documenta cual es cual.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, col, select

from cvp.storage import EventRow, MetricMinute, Project, Run, en_utc

__all__ = [
    "analizadores_de",
    "eventos_con_timestamp",
    "listar_proyectos",
    "minutos_en_rango",
    "pagina_de_eventos",
    "proyecto_por_id",
    "ultima_run_de",
    "ultimos_minutos",
]


def listar_proyectos(session: Session) -> list[Project]:
    return list(session.exec(select(Project).order_by(col(Project.id))).all())


def proyecto_por_id(session: Session, project_id: str) -> Project | None:
    return session.get(Project, project_id)


def ultima_run_de(session: Session, project_id: str) -> Run | None:
    """La ejecucion mas reciente. El `run_id` ordena por tiempo (#9), asi que `id DESC`."""
    return session.exec(
        select(Run).where(Run.project_id == project_id).order_by(col(Run.id).desc()).limit(1)
    ).first()


def analizadores_de(session: Session, project_id: str) -> list[str]:
    """Los `analyzer` distintos que han escrito algun minuto del proyecto."""
    filas = session.exec(
        select(MetricMinute.analyzer)
        .where(MetricMinute.project_id == project_id)
        .distinct()
        .order_by(col(MetricMinute.analyzer))
    ).all()
    return [str(a) for a in filas]


def _encaja(labels_json: str, filtro: dict[str, str]) -> bool:
    etiquetas = json.loads(labels_json)
    return all(etiquetas.get(clave) == valor for clave, valor in filtro.items())


def minutos_en_rango(
    session: Session,
    project_id: str,
    *,
    desde: datetime,
    hasta: datetime,
    analyzer: str | None = None,
    name: str | None = None,
    labels: dict[str, str] | None = None,
) -> list[MetricMinute]:
    """Los minutos `[desde, hasta]` con muestras, filtrados por serie.

    `labels` filtra por igualdad de las claves dadas: `{"direction": "norte"}` encaja con
    `{"line": "principal", "direction": "norte"}`. Las claves que no se dan no se miran.
    """
    consulta = select(MetricMinute).where(
        MetricMinute.project_id == project_id,
        col(MetricMinute.minute) >= en_utc(desde),
        col(MetricMinute.minute) <= en_utc(hasta),
        col(MetricMinute.samples) > 0,
    )
    if analyzer is not None:
        consulta = consulta.where(MetricMinute.analyzer == analyzer)
    if name is not None:
        consulta = consulta.where(MetricMinute.name == name)
    filtro = dict(labels or {})
    for clave, valor in filtro.items():
        # Prefiltro portable sobre el JSON ordenado y compacto: `"clave":"valor"` aparece
        # literal en la columna. `autoescape` cubre un `%` o un `_` en el valor. Lo que
        # decide es `_encaja`, abajo; esto solo evita traer lo que seguro no encaja.
        trozo = json.dumps({clave: valor}, ensure_ascii=False, separators=(",", ":"))[1:-1]
        consulta = consulta.where(col(MetricMinute.labels).contains(trozo, autoescape=True))
    consulta = consulta.order_by(
        col(MetricMinute.analyzer),
        col(MetricMinute.name),
        col(MetricMinute.labels),
        col(MetricMinute.minute),
    )
    filas = session.exec(consulta).all()
    if not filtro:
        return list(filas)
    return [fila for fila in filas if _encaja(fila.labels, filtro)]


def ultimos_minutos(session: Session, project_id: str) -> list[MetricMinute]:
    """El ultimo minuto con muestras de cada `(analyzer, name, labels)` del proyecto."""
    ultimo = (
        select(
            MetricMinute.analyzer,
            MetricMinute.name,
            MetricMinute.labels,
            func.max(MetricMinute.minute).label("minute"),
        )
        .where(MetricMinute.project_id == project_id, col(MetricMinute.samples) > 0)
        .group_by(MetricMinute.analyzer, MetricMinute.name, MetricMinute.labels)
        .subquery()
    )
    consulta = (
        select(MetricMinute)
        .join(
            ultimo,
            (MetricMinute.analyzer == ultimo.c.analyzer)
            & (MetricMinute.name == ultimo.c.name)
            & (MetricMinute.labels == ultimo.c.labels)
            & (MetricMinute.minute == ultimo.c.minute),
        )
        .where(MetricMinute.project_id == project_id)
        .order_by(col(MetricMinute.analyzer), col(MetricMinute.name), col(MetricMinute.labels))
    )
    return list(session.exec(consulta).all())


def pagina_de_eventos(
    session: Session,
    project_id: str,
    *,
    analyzer: str | None,
    name: str | None,
    desde: datetime | None,
    hasta: datetime | None,
    before: datetime | None,
    limite: int,
) -> list[EventRow]:
    """Hasta `limite` eventos del proyecto, los mas recientes primero.

    `before` es exclusivo y `desde`/`hasta` inclusivos. El orden es `(timestamp, id)`
    descendente: el `id` desempata a los eventos del mismo fotograma, que comparten
    timestamp, para que el orden sea estable entre peticiones.

    Las marcas se pasan al `bind` **en UTC**: SQLite guarda los componentes sin zona, y
    un `+02:00` del cliente colado tal cual compararia hora local contra hora UTC.
    """
    consulta = (
        select(EventRow)
        .join(Run, col(EventRow.run_id) == Run.id)
        .where(Run.project_id == project_id)
    )
    if analyzer is not None:
        consulta = consulta.where(EventRow.analyzer == analyzer)
    if name is not None:
        consulta = consulta.where(EventRow.name == name)
    if desde is not None:
        consulta = consulta.where(col(EventRow.timestamp) >= en_utc(desde))
    if hasta is not None:
        consulta = consulta.where(col(EventRow.timestamp) <= en_utc(hasta))
    if before is not None:
        consulta = consulta.where(col(EventRow.timestamp) < en_utc(before))
    consulta = consulta.order_by(col(EventRow.timestamp).desc(), col(EventRow.id).desc()).limit(
        limite
    )
    return list(session.exec(consulta).all())


def eventos_con_timestamp(
    session: Session,
    project_id: str,
    *,
    analyzer: str | None,
    name: str | None,
    timestamp: datetime,
    antes_del_id: int,
) -> list[EventRow]:
    """Los eventos con exactamente ese timestamp y un `id` menor: el resto del fotograma
    que una pagina ha cortado por la mitad."""
    consulta = (
        select(EventRow)
        .join(Run, col(EventRow.run_id) == Run.id)
        .where(
            Run.project_id == project_id,
            col(EventRow.timestamp) == en_utc(timestamp),
            col(EventRow.id) < antes_del_id,
        )
    )
    if analyzer is not None:
        consulta = consulta.where(EventRow.analyzer == analyzer)
    if name is not None:
        consulta = consulta.where(EventRow.name == name)
    consulta = consulta.order_by(col(EventRow.id).desc())
    return list(session.exec(consulta).all())
