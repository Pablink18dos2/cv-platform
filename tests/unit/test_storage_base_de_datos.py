"""El esquema y la escritura: WAL, ida y vuelta por la forma canonica, y acumulacion.

**La base va siempre en un fichero temporal, nunca en `:memory:`.** Una base en memoria no
ejercita WAL —una base `:memory:` no tiene fichero de journal— ni la concurrencia entre el
worker que escribe y la API que lee, que es justo lo que hay que probar. Es la condicion
que pide #9 y no un detalle de estilo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from cvp.core import Event, Metric
from cvp.storage import (
    crear_engine,
    crear_esquema,
    en_utc,
    escribir_lote,
    eventos_de,
    journal_mode,
    metricas_de,
    minutos_de,
)
from cvp.storage.models import MetricMinute, Project, Run

RUN = "20260824T142018Z-2808cc47"
OTRO_RUN = "20260824T142530Z-9f1b0c33"
PROYECTO = "01-parking-trafico"
INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)


@pytest.fixture
def motor(tmp_path: Path) -> Iterator[Engine]:
    """Una base en un fichero temporal, con el esquema ya creado."""
    creado = crear_engine(f"sqlite:///{(tmp_path / 'cvp.sqlite3').as_posix()}")
    crear_esquema(creado)
    yield creado
    creado.dispose()


def evento(marca: datetime, **atributos: object) -> Event:
    return Event(
        analyzer="traffic_flow",
        name="line_crossing",
        timestamp=marca,
        attributes=atributos,
    )


def metrica(marca: datetime, valor: float, **labels: str) -> Metric:
    return Metric(
        analyzer="traffic_flow",
        name="vehicles_in_scene",
        value=valor,
        timestamp=marca,
        labels=labels,
    )


def escribir(
    motor: Engine,
    *,
    run_id: str = RUN,
    eventos: list[Event] | None = None,
    metricas: list[Metric] | None = None,
) -> None:
    with Session(motor) as session:
        escribir_lote(
            session,
            run_id=run_id,
            project_id=PROYECTO,
            project_name="Parking y tráfico",
            eventos=eventos or [],
            metricas=metricas or [],
        )
        session.commit()


# --- WAL ------------------------------------------------------------------------------


def test_una_base_recien_creada_esta_en_modo_wal(motor: Engine) -> None:
    """Sin WAL, un lector bloquea al escritor: la API haria esperar al worker en cada
    peticion del dashboard, y el bucle de inferencia perderia fotogramas por una consulta."""
    assert journal_mode(motor) == "wal"


def test_el_wal_sobrevive_a_reabrir_la_base(tmp_path: Path) -> None:
    """Es una propiedad del fichero, no de la conexion: se pone una vez y se queda."""
    fichero = tmp_path / "cvp.sqlite3"
    primero = crear_engine(f"sqlite:///{fichero.as_posix()}")
    crear_esquema(primero)
    primero.dispose()

    conexion = sqlite3.connect(fichero)
    try:
        modo = conexion.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conexion.close()
    assert modo.lower() == "wal"


def test_las_claves_ajenas_estan_activas(motor: Engine) -> None:
    """SQLite las ignora por defecto, y entonces el esquema seria decorativo."""
    with Session(motor) as session:
        session.add(Run(id=RUN, project_id="proyecto-que-no-existe", started_at=INICIO))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            session.commit()


# --- El esquema -----------------------------------------------------------------------


def test_un_run_apunta_a_su_proyecto_y_se_crea_solo(motor: Engine) -> None:
    escribir(motor, metricas=[metrica(INICIO, 3.0)])
    with Session(motor) as session:
        proyecto = session.get(Project, PROYECTO)
        ejecucion = session.get(Run, RUN)
        assert proyecto is not None and proyecto.name == "Parking y tráfico"
        assert ejecucion is not None and ejecucion.project_id == PROYECTO


def test_el_started_at_del_run_sale_del_propio_run_id(motor: Engine) -> None:
    """Ningun `datetime.now()`: el instante de arranque ya viaja dentro del identificador."""
    escribir(motor, metricas=[metrica(INICIO, 1.0)])
    with Session(motor) as session:
        ejecucion = session.get(Run, RUN)
        assert ejecucion is not None
        assert en_utc(ejecucion.started_at) == datetime(2026, 8, 24, 14, 20, 18, tzinfo=UTC)


def test_el_ended_at_es_la_ultima_muestra_escrita(motor: Engine) -> None:
    """Un worker que se cae no llega a cerrar su ejecucion; lo que hace falta saber al
    mirarla es hasta cuando hubo datos."""
    escribir(motor, metricas=[metrica(INICIO, 1.0), metrica(INICIO + timedelta(minutes=3), 2.0)])
    with Session(motor) as session:
        ejecucion = session.get(Run, RUN)
        assert ejecucion is not None
        assert en_utc(ejecucion.ended_at) == INICIO + timedelta(minutes=3)


def test_un_run_id_con_otro_formato_no_entra_en_la_base(motor: Engine) -> None:
    with pytest.raises(ValueError, match="run_id invalido"):
        escribir(motor, run_id="ejecucion-de-ayer")


def test_renombrar_el_proyecto_en_el_yaml_no_reescribe_la_fila(motor: Engine) -> None:
    """Renombrar es cosa del operador, no un efecto colateral de escribir metricas."""
    escribir(motor, metricas=[metrica(INICIO, 1.0)])
    with Session(motor) as session:
        escribir_lote(
            session,
            run_id=RUN,
            project_id=PROYECTO,
            project_name="Otro nombre",
            metricas=[metrica(INICIO, 2.0)],
        )
        session.commit()
        proyecto = session.get(Project, PROYECTO)
        assert proyecto is not None and proyecto.name == "Parking y tráfico"


# --- Ida y vuelta por la forma canonica de #19 ----------------------------------------


def test_un_evento_vuelve_de_la_base_siendo_el_mismo_evento(motor: Engine) -> None:
    """Se guarda con `event_to_dict` y se lee con `event_from_dict`: no hay una segunda
    forma que pueda separarse de la de #19."""
    original = evento(
        INICIO + timedelta(seconds=12),
        line="principal",
        direction="norte",
        track_id=7,
        speed=None,
    )
    escribir(motor, eventos=[original])
    with Session(motor) as session:
        (fila,) = eventos_de(session, RUN)
    assert fila.to_event() == original


def test_una_metrica_vuelve_de_la_base_siendo_la_misma_metrica(motor: Engine) -> None:
    original = metrica(INICIO + timedelta(seconds=12, microseconds=250000), 12.5, direction="norte")
    escribir(motor, metricas=[original])
    with Session(motor) as session:
        (fila,) = metricas_de(session, RUN)
    assert fila.to_metric() == original


def test_los_microsegundos_y_la_zona_sobreviven_al_viaje(motor: Engine) -> None:
    """SQLite no guarda la zona en una columna `DATETIME`, y `cvp.core` rechaza un
    `datetime` sin ella: si la vuelta no la repusiera, esto reventaria al construir."""
    original = metrica(datetime(2026, 8, 24, 23, 59, 59, 123456, tzinfo=UTC), 1.0)
    escribir(motor, metricas=[original])
    with Session(motor) as session:
        (fila,) = metricas_de(session, RUN)
    vuelta = fila.to_metric()
    assert vuelta.timestamp == original.timestamp
    assert vuelta.timestamp.tzinfo is not None


def test_un_evento_sin_atributos_tambien_va_y_vuelve(motor: Engine) -> None:
    original = evento(INICIO)
    escribir(motor, eventos=[original])
    with Session(motor) as session:
        (fila,) = eventos_de(session, RUN)
    assert fila.to_event() == original
    assert fila.attributes == "{}"


# --- La agregacion por minuto, ya contra la base --------------------------------------


def test_las_muestras_se_reparten_por_el_minuto_de_su_propia_marca(motor: Engine) -> None:
    escribir(
        motor,
        metricas=[
            metrica(INICIO + timedelta(seconds=59), 10.0),
            metrica(INICIO + timedelta(seconds=60), 30.0),
        ],
    )
    with Session(motor) as session:
        minutos = minutos_de(session, PROYECTO)
    assert [en_utc(m.minute) for m in minutos] == [INICIO, INICIO + timedelta(minutes=1)]
    assert [m.samples for m in minutos] == [1, 1]


def test_reescribir_un_minuto_acumula_y_no_reemplaza(motor: Engine) -> None:
    """Un worker que se reinicia a mitad de minuto no puede borrar lo que ya habia.

    El reinicio estrena `run_id` —es otra ejecucion— y el agregado cuelga del proyecto
    justamente para que las muestras de antes y las de despues acaben en la misma fila.
    """
    escribir(motor, metricas=[metrica(INICIO + timedelta(seconds=5), 10.0)])
    escribir(motor, run_id=OTRO_RUN, metricas=[metrica(INICIO + timedelta(seconds=45), 20.0)])

    with Session(motor) as session:
        (minuto,) = minutos_de(session, PROYECTO)
    assert minuto.samples == 2
    assert minuto.sum_value == 30.0
    assert minuto.mean_value == 15.0
    assert (minuto.min_value, minuto.max_value) == (10.0, 20.0)


def test_reescribir_un_minuto_acumula_tambien_el_tiempo_cubierto(motor: Engine) -> None:
    """El reinicio, con tramos de verdad: cada ejecucion cubre su trozo del minuto y la fila
    acaba con la suma de los dos, no con el ultimo.

    La primera ejecucion mide 10,0 de las :00 a las :20 y muere; la segunda arranca a las
    :40 y mide 30,0 hasta el final del minuto. La media del minuto es la temporal sobre lo
    cubierto, y no la de las muestras ni la de la ultima ejecucion.
    """
    escribir(
        motor,
        metricas=[metrica(INICIO + timedelta(seconds=s), 10.0) for s in range(0, 21, 2)],
    )
    escribir(
        motor,
        run_id=OTRO_RUN,
        metricas=[metrica(INICIO + timedelta(seconds=s), 30.0) for s in range(40, 61, 2)],
    )

    with Session(motor) as session:
        primero, segundo = minutos_de(session, PROYECTO)
    # La primera ejecucion cubre 19 s (su muestra de las :20 queda sin tramo) y la segunda
    # 20 s (la de las :58 llega hasta la frontera). 10 x 19 + 30 x 20, entre 39.
    assert primero.seconds == pytest.approx(39.0)
    assert primero.mean_value == pytest.approx(790.0 / 39.0)
    assert primero.samples == 21
    assert (primero.min_value, primero.max_value) == (10.0, 30.0)
    # La muestra de las :60 es del minuto siguiente.
    assert segundo.samples == 1


def test_la_media_acumulada_no_es_la_media_de_las_medias(motor: Engine) -> None:
    """Dos trozos del mismo minuto que cubren distinto tiempo: promediar medias daria 3,0 y
    la respuesta es 4,0. Es el motivo por el que la tabla guarda la integral y los segundos.

    Cada escritura suelta lleva dos muestras, y solo la primera cierra su tramo (la segunda
    queda pendiente): el primer trozo es 5,0 durante 3 s y el segundo 1,0 durante 1 s.
    """
    escribir(motor, metricas=[metrica(INICIO, 5.0), metrica(INICIO + timedelta(seconds=6), 5.0)])
    escribir(
        motor,
        run_id=OTRO_RUN,
        metricas=[
            metrica(INICIO + timedelta(seconds=30), 1.0),
            metrica(INICIO + timedelta(seconds=32), 1.0),
        ],
    )
    with Session(motor) as session:
        (minuto,) = minutos_de(session, PROYECTO)
    assert minuto.samples == 4
    # 5,0 x 3 s + 1,0 x 1 s, entre 4 s cubiertos.
    assert minuto.seconds == pytest.approx(4.0)
    assert minuto.mean_value == pytest.approx(4.0)


def test_un_lote_que_cruza_una_frontera_de_minuto_no_se_va_entero_a_un_lado(
    motor: Engine,
) -> None:
    """El caso que un `ON CONFLICT` mal escrito resuelve mal: una sola escritura, dos
    minutos, y la clave unica de la tabla decidiendo bien de que celda es cada muestra."""
    escribir(
        motor,
        metricas=[
            metrica(INICIO + timedelta(seconds=58), 10.0),
            metrica(INICIO + timedelta(seconds=59), 20.0),
            metrica(INICIO + timedelta(seconds=60), 30.0),
            metrica(INICIO + timedelta(seconds=61), 40.0),
        ],
    )
    with Session(motor) as session:
        primero, segundo = minutos_de(session, PROYECTO)
    assert (primero.samples, primero.sum_value) == (2, 30.0)
    assert (segundo.samples, segundo.sum_value) == (2, 70.0)
    # Y el tiempo tampoco cruza la frontera: :59 cubre hasta las :60 y :60 empieza ahi.
    assert primero.seconds == pytest.approx(2.0)
    assert segundo.seconds == pytest.approx(0.5)


def test_cada_combinacion_de_analizador_nombre_y_etiquetas_es_una_fila(motor: Engine) -> None:
    escribir(
        motor,
        metricas=[
            metrica(INICIO, 3.0, direction="norte"),
            metrica(INICIO, 5.0, direction="sur"),
            metrica(INICIO, 7.0),
        ],
    )
    with Session(motor) as session:
        minutos = minutos_de(session, PROYECTO)
    assert len(minutos) == 3
    assert {m.labels for m in minutos} == {'{"direction":"norte"}', '{"direction":"sur"}', "{}"}


def test_la_clave_unica_del_minuto_no_deja_duplicar_una_celda(motor: Engine) -> None:
    """La red de seguridad: si algun dia hubiera dos escritores, se enteraria la base y no
    la grafica."""
    escribir(motor, metricas=[metrica(INICIO, 1.0)])
    with Session(motor) as session:
        (existente,) = minutos_de(session, PROYECTO)
        session.add(
            MetricMinute(
                project_id=PROYECTO,
                minute=en_utc(existente.minute),
                analyzer=existente.analyzer,
                name=existente.name,
                labels=existente.labels,
                samples=1,
                sum_value=1.0,
                min_value=1.0,
                max_value=1.0,
                seconds=0.0,
                integral=0.0,
                mean_value=1.0,
            )
        )
        with pytest.raises(IntegrityError, match="UNIQUE"):
            session.commit()


def test_los_datos_crudos_siguen_siendo_de_su_ejecucion(motor: Engine) -> None:
    """El agregado es del proyecto, pero `event` y `metric` son de quien los escribio."""
    escribir(motor, metricas=[metrica(INICIO, 1.0)])
    escribir(motor, run_id=OTRO_RUN, metricas=[metrica(INICIO, 2.0)])
    with Session(motor) as session:
        assert [f.value for f in metricas_de(session, RUN)] == [1.0]
        assert [f.value for f in metricas_de(session, OTRO_RUN)] == [2.0]


def test_un_lote_vacio_no_crea_filas_de_mas(motor: Engine) -> None:
    escribir(motor)
    with Session(motor) as session:
        assert metricas_de(session, RUN) == []
        assert eventos_de(session, RUN) == []
        assert minutos_de(session, PROYECTO) == []
        ejecucion = session.get(Run, RUN)
        assert ejecucion is not None and ejecucion.ended_at is None


def test_los_eventos_no_alimentan_la_tabla_de_minutos(motor: Engine) -> None:
    """`metric_minute` resume medidas. Contar eventos por minuto es otra pregunta y la
    respondera #12 sobre la tabla `event`, que ya tiene su indice por marca."""
    escribir(motor, eventos=[evento(INICIO)])
    with Session(motor) as session:
        assert minutos_de(session, PROYECTO) == []
        assert len(eventos_de(session, RUN)) == 1


def test_el_esquema_no_lleva_sql_de_sqlite(motor: Engine) -> None:
    """El criterio de #9: el mismo esquema tiene que valer sobre Postgres.

    Se comprueba mirando los tipos de las columnas, que es donde se colaria un tipo que
    solo existe en SQLite, y que ninguna tabla traiga SQL a medida.
    """
    tipos = {
        str(columna.type).split("(")[0]
        for tabla in (Project.__table__, Run.__table__, MetricMinute.__table__)
        for columna in tabla.columns
    }
    assert tipos <= {"VARCHAR", "INTEGER", "FLOAT", "DATETIME"}


def test_dos_conexiones_a_la_vez_no_se_bloquean(motor: Engine) -> None:
    """Es para lo que esta WAL: la API lee mientras el worker escribe."""
    escribir(motor, metricas=[metrica(INICIO, 1.0)])
    with Session(motor) as lector, Session(motor) as escritor:
        antes = len(lector.exec(select(MetricMinute)).all())
        escribir_lote(
            escritor,
            run_id=RUN,
            project_id=PROYECTO,
            project_name="Parking y tráfico",
            metricas=[metrica(INICIO + timedelta(minutes=1), 2.0)],
        )
        escritor.commit()
        assert antes == 1
        assert len(lector.exec(select(MetricMinute)).all()) >= 1
