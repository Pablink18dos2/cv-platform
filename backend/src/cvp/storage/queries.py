"""Escribir un lote: las filas crudas y los agregados del minuto, en una transaccion.

Es lo que usa el sink `sqlite`, y lo que usara la API de #12 para leer. Todo lo que hay
aqui es SQL portable a traves de SQLModel: ni `INSERT OR REPLACE` de SQLite, ni
`ON CONFLICT` de ningun dialecto.

**Por que la acumulacion del minuto es un leer-modificar-escribir y no un
`INSERT ... ON CONFLICT DO UPDATE`.** El `ON CONFLICT` es mas corto, pero se escribe
distinto en cada dialecto (`sqlalchemy.dialects.sqlite.insert` y
`...postgresql.insert` son dos construcciones diferentes) y el criterio del issue es que el
esquema funcione igual en los dos sin SQL especifico de ninguno. Ademas, un `ON CONFLICT`
mal escrito es exactamente el fallo que este issue teme: el que resuelve mal el lote que
cruza una frontera de minuto y no se ve mirando la pantalla. La condicion que hace segura
la version larga esta escrita y se cumple por construccion: **hay un solo escritor por
proyecto**, el worker, y su `run_id` es unico; la API de #12 solo lee. Si algun dia hubiera
dos workers del mismo proyecto, esto pasaria a ser un `ON CONFLICT` con su tabla de
dialectos, y la clave unica `uq_metric_minute` esta puesta para que el problema se note
como un error y no como un dato mal sumado.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from sqlmodel import Session, select

from cvp.core import Event, Metric
from cvp.storage.aggregation import Agregado, ClaveMinuto, agregar_por_minuto
from cvp.storage.models import EventRow, MetricMinute, MetricRow, Project, Run, en_utc
from cvp.storage.run_id import instante_de_run_id, validar_run_id

__all__ = [
    "acumular_minutos",
    "asegurar_proyecto",
    "asegurar_run",
    "escribir_lote",
    "eventos_de",
    "metricas_de",
    "minutos_de",
]


def asegurar_proyecto(session: Session, project_id: str, name: str, creado: datetime) -> Project:
    """La fila del proyecto, creandola la primera vez.

    No actualiza el `name` si el proyecto ya existe: renombrar un proyecto en el YAML es
    cosa del operador y no algo que deba pasar como efecto colateral de escribir metricas.
    """
    proyecto = session.get(Project, project_id)
    if proyecto is None:
        proyecto = Project(id=project_id, name=name, created_at=creado)
        session.add(proyecto)
    return proyecto


def asegurar_run(session: Session, run_id: str, project_id: str) -> Run:
    """La fila de la ejecucion, creandola la primera vez.

    `started_at` sale del propio `run_id`, que lleva dentro el instante de arranque: asi
    este modulo no consulta ningun reloj, que es la regla del proyecto entero.
    """
    validar_run_id(run_id)
    ejecucion = session.get(Run, run_id)
    if ejecucion is None:
        ejecucion = Run(id=run_id, project_id=project_id, started_at=instante_de_run_id(run_id))
        session.add(ejecucion)
    return ejecucion


def escribir_lote(
    session: Session,
    *,
    run_id: str,
    project_id: str,
    project_name: str,
    eventos: Sequence[Event] = (),
    metricas: Sequence[Metric] = (),
    agregados: Mapping[ClaveMinuto, Agregado] | None = None,
) -> None:
    """Escribe un lote entero: eventos, metricas y los minutos que tocan.

    No hace `commit`: quien llama decide la frontera de la transaccion, y en el sink esa
    frontera es el lote completo. Asi, o entran las filas crudas **y** sus agregados, o no
    entra nada; un fallo a mitad no deja un minuto contando muestras que no estan.

    `agregados` es lo que este lote anade a cada minuto. El sink lo calcula con su
    `AgregadorPorMinuto`, que recuerda entre lotes la ultima muestra de cada serie y por
    eso escribe el tramo de esa muestra en el lote siguiente. Sin `agregados` se calcula
    aqui de un solo lote, y entonces la ultima muestra de cada serie se queda sin tramo:
    vale para una escritura suelta, no para un worker.
    """
    creado = instante_de_run_id(run_id)
    asegurar_proyecto(session, project_id, project_name, creado)
    asegurar_run(session, run_id, project_id)
    # El padre, antes que los hijos, y a mano. Las tablas se relacionan por clave ajena
    # pero **sin `relationship()`**, y sin relaciones declaradas SQLAlchemy no ordena las
    # inserciones entre si: mandaria el `INSERT` de `event` antes que el de su `run` y
    # SQLite —con `foreign_keys=ON`, que es como lo abre `engine.py`— lo rechazaria. Un
    # `flush` de dos filas por lote es un precio ridiculo por no tener que confiar en un
    # orden que nadie garantiza.
    session.flush()

    for evento in eventos:
        session.add(EventRow.from_event(run_id, evento))
    for metrica in metricas:
        session.add(MetricRow.from_metric(run_id, metrica))

    if agregados is None:
        agregados = agregar_por_minuto(metricas)
    acumular_minutos(session, project_id, agregados)
    _avanzar_fin_de_run(session, run_id, eventos, metricas)


def acumular_minutos(
    session: Session, project_id: str, agregados: Mapping[ClaveMinuto, Agregado]
) -> None:
    """Suma `agregados` a lo que ya hubiera en `metric_minute`.

    **Acumula, no reemplaza.** Un worker que se reinicia a mitad de minuto estrena
    `run_id`, y el agregado del minuto cuelga del proyecto justamente para que las muestras
    de antes y las de despues acaben en la misma fila en vez de partir la serie en dos.

    Se acumulan las dos familias por separado —muestras y tiempo—, porque un lote puede
    traer solo tiempo para un minuto: el tramo de la muestra que llego en el lote anterior.
    """
    for clave, agregado in agregados.items():
        fila = session.exec(
            select(MetricMinute).where(
                MetricMinute.project_id == project_id,
                MetricMinute.minute == clave.minute,
                MetricMinute.analyzer == clave.analyzer,
                MetricMinute.name == clave.name,
                MetricMinute.labels == clave.labels,
            )
        ).first()
        if fila is None:
            fila = MetricMinute(
                project_id=project_id,
                minute=clave.minute,
                analyzer=clave.analyzer,
                name=clave.name,
                labels=clave.labels,
                samples=0,
                sum_value=0.0,
                min_value=0.0,
                max_value=0.0,
                seconds=0.0,
                integral=0.0,
                mean_value=0.0,
            )
        unido = Agregado(
            samples=fila.samples,
            sum_value=fila.sum_value,
            min_value=fila.min_value,
            max_value=fila.max_value,
            seconds=fila.seconds,
            integral=fila.integral,
        ).unido_con(agregado)
        fila.samples = unido.samples
        fila.sum_value = unido.sum_value
        fila.min_value = unido.min_value
        fila.max_value = unido.max_value
        fila.seconds = unido.seconds
        fila.integral = unido.integral
        fila.mean_value = unido.mean_value
        session.add(fila)


def _avanzar_fin_de_run(
    session: Session, run_id: str, eventos: Iterable[Event], metricas: Iterable[Metric]
) -> None:
    """Lleva `run.ended_at` al timestamp de la ultima muestra del lote.

    Es tiempo de las muestras, no de pared: un worker que se cae no llega a cerrar su
    ejecucion, y lo que hace falta saber al mirarla es hasta cuando hubo datos.
    """
    marcas = [e.timestamp for e in eventos]
    marcas += [m.timestamp for m in metricas]
    if not marcas:
        return
    ultima = max(marcas)
    ejecucion = session.get(Run, run_id)
    if ejecucion is None:  # pragma: no cover - `escribir_lote` acaba de crearla
        return
    if ejecucion.ended_at is None or en_utc(ejecucion.ended_at) < ultima:
        ejecucion.ended_at = ultima
        session.add(ejecucion)


# --- Lecturas. Las usa la suite, y son la base de lo que servira #12 -------------------


def eventos_de(session: Session, run_id: str) -> list[EventRow]:
    """Los eventos de una ejecucion, en orden cronologico."""
    return list(
        session.exec(
            select(EventRow)
            .where(EventRow.run_id == run_id)
            .order_by(EventRow.timestamp, EventRow.id)
        ).all()
    )


def metricas_de(session: Session, run_id: str) -> list[MetricRow]:
    """Las metricas de una ejecucion, en orden cronologico."""
    return list(
        session.exec(
            select(MetricRow)
            .where(MetricRow.run_id == run_id)
            .order_by(MetricRow.timestamp, MetricRow.id)
        ).all()
    )


def minutos_de(session: Session, project_id: str) -> list[MetricMinute]:
    """Los agregados por minuto de un proyecto, ordenados por minuto."""
    return list(
        session.exec(
            select(MetricMinute)
            .where(MetricMinute.project_id == project_id)
            .order_by(MetricMinute.minute, MetricMinute.analyzer, MetricMinute.name)
        ).all()
    )
