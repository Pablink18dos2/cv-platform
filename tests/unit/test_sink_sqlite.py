"""El sink `sqlite`: agrupar escrituras, y decir cuanto se pierde si el proceso muere.

La base va en un fichero temporal, no en `:memory:`: es la condicion de #9, porque una base
en memoria no ejercita WAL ni la concurrencia.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlmodel import Session

import cvp.sinks.sqlite
from cvp.core import AnalyzerOutput, Event, Frame, Metric
from cvp.sinks.sqlite import SqliteSink
from cvp.storage import crear_engine, eventos_de, metricas_de, minutos_de

RUN = "20260824T142018Z-2808cc47"
PROYECTO = "01-parking-trafico"
INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)


class CronometroFalso:
    """Cronometro monotono que solo avanza cuando se le dice."""

    def __init__(self) -> None:
        self.valor = 0.0

    def __call__(self) -> float:
        return self.valor

    def avanzar(self, segundos: float) -> None:
        self.valor += segundos


def fotograma(indice: int = 0) -> Frame:
    return Frame(
        image=np.zeros((4, 4, 3), dtype=np.uint8),
        index=indice,
        timestamp=INICIO + timedelta(seconds=indice),
    )


def metrica(valor: float, segundos: float = 0.0) -> Metric:
    return Metric(
        analyzer="traffic_flow",
        name="vehicles_in_scene",
        value=valor,
        timestamp=INICIO + timedelta(seconds=segundos),
    )


def evento(segundos: float = 0.0) -> Event:
    return Event(
        analyzer="traffic_flow",
        name="line_crossing",
        timestamp=INICIO + timedelta(seconds=segundos),
        attributes={"direction": "norte"},
    )


def sink(tmp_path: Path, **kwargs: Any) -> SqliteSink:
    argumentos: dict[str, Any] = {
        "project_id": PROYECTO,
        "project_name": "Parking y tráfico",
        "batch_size": 3,
        "max_interval_s": 5.0,
    }
    argumentos.update(kwargs)
    return SqliteSink(f"sqlite:///{(tmp_path / 'cvp.sqlite3').as_posix()}", **argumentos)


def leer(tmp_path: Path) -> tuple[list[Any], list[Any], list[Any]]:
    motor = crear_engine(f"sqlite:///{(tmp_path / 'cvp.sqlite3').as_posix()}")
    try:
        with Session(motor) as session:
            return (
                eventos_de(session, RUN),
                metricas_de(session, RUN),
                minutos_de(session, PROYECTO),
            )
    finally:
        motor.dispose()


# --- El lote --------------------------------------------------------------------------


def test_no_escribe_una_transaccion_por_metrica(tmp_path: Path) -> None:
    """Con `batch_size: 3`, dos metricas siguen pendientes: en la base no hay nada."""
    almacen = sink(tmp_path)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))
    almacen.write(RUN, fotograma(1), AnalyzerOutput(metrics=(metrica(2.0),)))

    assert almacen.pendientes == 2
    assert leer(tmp_path)[1] == []


def test_al_llenarse_el_lote_se_escribe_entero(tmp_path: Path) -> None:
    almacen = sink(tmp_path)
    for i in range(3):
        almacen.write(RUN, fotograma(i), AnalyzerOutput(metrics=(metrica(float(i)),)))

    assert almacen.pendientes == 0
    _, metricas, _ = leer(tmp_path)
    assert [f.value for f in metricas] == [0.0, 1.0, 2.0]


def test_los_eventos_cuentan_para_el_mismo_lote(tmp_path: Path) -> None:
    """El lote se mide en registros, no en metricas: un analizador que solo emite eventos
    tambien tiene que volcar."""
    almacen = sink(tmp_path)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(events=(evento(), evento(1), evento(2))))
    eventos, _, _ = leer(tmp_path)
    assert len(eventos) == 3


def test_el_intervalo_maximo_vuelca_aunque_el_lote_no_este_lleno(tmp_path: Path) -> None:
    """Con un analizador lento, esperar a juntar 200 registros seria esperar minutos."""
    cronometro = CronometroFalso()
    almacen = sink(tmp_path, timer=cronometro, max_interval_s=5.0)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))
    assert almacen.pendientes == 1

    cronometro.avanzar(5.0)
    almacen.write(RUN, fotograma(1), AnalyzerOutput(metrics=(metrica(2.0),)))

    assert almacen.pendientes == 0
    assert len(leer(tmp_path)[1]) == 2


def test_el_intervalo_se_mide_con_un_cronometro_monotono(tmp_path: Path) -> None:
    """No con el reloj de pared: una ejecucion de dias no puede descuadrarse porque NTP
    eche el reloj atras."""
    cronometro = CronometroFalso()
    almacen = sink(tmp_path, timer=cronometro, max_interval_s=5.0)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))
    cronometro.avanzar(4.9)
    almacen.write(RUN, fotograma(1), AnalyzerOutput(metrics=(metrica(2.0),)))
    assert almacen.pendientes == 2


def test_flush_vacia_el_lote_pendiente(tmp_path: Path) -> None:
    """Es lo que llama el runner al terminar, tambien en el camino de Ctrl+C (#8): un
    apagado ordenado no pierde nada."""
    almacen = sink(tmp_path)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))
    almacen.flush()

    assert almacen.pendientes == 0
    assert len(leer(tmp_path)[1]) == 1


def test_flush_sin_nada_pendiente_no_hace_nada(tmp_path: Path) -> None:
    almacen = sink(tmp_path)
    almacen.flush()
    almacen.flush()
    assert leer(tmp_path) == ([], [], [])


def test_lo_pendiente_es_exactamente_lo_que_se_perderia(tmp_path: Path) -> None:
    """La consecuencia real de agrupar, medida y no solo documentada.

    Se escriben cinco medidas con lotes de tres: tres entran y dos quedan pendientes. Un
    sink nuevo sobre la misma base —que es lo que ve el que llega despues de un `kill -9`—
    encuentra tres.
    """
    almacen = sink(tmp_path)
    for i in range(5):
        almacen.write(RUN, fotograma(i), AnalyzerOutput(metrics=(metrica(float(i), i),)))

    assert almacen.pendientes == 2
    del almacen  # el proceso muere sin flush

    _, metricas, _ = leer(tmp_path)
    assert [f.value for f in metricas] == [0.0, 1.0, 2.0]


def test_el_docstring_dice_cuantos_datos_se_pierden() -> None:
    """Criterio de #9: quien opera esto tiene derecho a saberlo sin leer el codigo.

    Es la consecuencia real de agrupar escrituras, asi que se pide por escrito y en el sitio
    donde se lee: el docstring del modulo, no un comentario suelto.
    """
    doc = cvp.sinks.sqlite.__doc__ or ""
    assert "batch_size" in doc and "max_interval_s" in doc
    assert "se pierde" in doc
    assert "flush()" in doc


# --- La agregacion llega a la base con el lote ----------------------------------------


def test_el_lote_escribe_tambien_los_minutos(tmp_path: Path) -> None:
    almacen = sink(tmp_path, batch_size=100)
    for i in range(4):
        almacen.write(RUN, fotograma(i), AnalyzerOutput(metrics=(metrica(float(i), i * 30),)))
    almacen.flush()

    _, metricas, minutos = leer(tmp_path)
    assert len(metricas) == 4
    assert [m.samples for m in minutos] == [2, 2]


def test_un_lote_a_caballo_entre_dos_minutos_se_reparte(tmp_path: Path) -> None:
    """El caso que resuelve mal un `ON CONFLICT` mal escrito, visto desde el sink."""
    almacen = sink(tmp_path, batch_size=100)
    almacen.write(
        RUN,
        fotograma(0),
        AnalyzerOutput(metrics=(metrica(10.0, 59.0), metrica(30.0, 60.0))),
    )
    almacen.flush()

    _, _, minutos = leer(tmp_path)
    assert [m.samples for m in minutos] == [1, 1]
    assert [m.mean_value for m in minutos] == [10.0, 30.0]


# --- Los fallos -----------------------------------------------------------------------


def _romper_la_escritura(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hace fallar la escritura como fallaria de verdad: al hablar con la base."""

    def falla(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("no queda espacio en el disco")

    monkeypatch.setattr(cvp.sinks.sqlite, "escribir_lote", falla)


def test_un_fallo_al_escribir_no_deja_el_lote_creciendo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el disco se llena, acumular en memoria hasta reventar el worker seria cambiar
    perder unos segundos de datos por perder la ejecucion entera."""
    almacen = sink(tmp_path, batch_size=100)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))
    _romper_la_escritura(monkeypatch)

    with pytest.raises(RuntimeError, match="disco"):
        almacen.flush()
    assert almacen.pendientes == 0


def test_el_fallo_sale_hacia_arriba_para_que_el_runner_lo_cuente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El runner envuelve cada escritura y suma un `sink_errors`; si el sink se tragara el
    fallo, la ejecucion figuraria como perfecta sin haber escrito nada."""
    almacen = sink(tmp_path, batch_size=1)
    _romper_la_escritura(monkeypatch)

    with pytest.raises(RuntimeError, match="disco"):
        almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))


def test_cambiar_de_ejecucion_con_cosas_pendientes_no_las_mezcla(tmp_path: Path) -> None:
    """No deberia pasar (un runner, un `run_id`), pero mezclarlas escribiria filas con el
    `run_id` que no es, y eso ya no se puede deshacer."""
    otro = "20260824T142530Z-9f1b0c33"
    almacen = sink(tmp_path, batch_size=100)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(1.0),)))
    almacen.write(otro, fotograma(1), AnalyzerOutput(metrics=(metrica(2.0),)))
    almacen.flush()

    motor = crear_engine(f"sqlite:///{(tmp_path / 'cvp.sqlite3').as_posix()}")
    try:
        with Session(motor) as session:
            assert [f.value for f in metricas_de(session, RUN)] == [1.0]
            assert [f.value for f in metricas_de(session, otro)] == [2.0]
    finally:
        motor.dispose()


# --- Construccion ---------------------------------------------------------------------


def test_from_config_toma_la_url_y_la_ventana_del_yaml_y_el_proyecto_de_la_fabrica(
    tmp_path: Path,
) -> None:
    class Cfg:
        type = "sqlite"
        url = f"sqlite:///{(tmp_path / 'otra.sqlite3').as_posix()}"
        batch_size = 7
        max_interval_s = 1.5

    almacen = SqliteSink.from_config(Cfg(), project_id="p", project_name="Proyecto")
    assert (almacen.batch_size, almacen.max_interval_s) == (7, 1.5)
    assert (almacen.project_id, almacen.project_name) == ("p", "Proyecto")
    assert (tmp_path / "otra.sqlite3").exists()


def test_el_sink_crea_el_directorio_de_la_base_al_arrancar(tmp_path: Path) -> None:
    """La configuracion no crea directorios (#3); el sink si, y al arrancar."""
    destino = tmp_path / "sin" / "crear" / "cvp.sqlite3"
    SqliteSink(f"sqlite:///{destino.as_posix()}", project_id=PROYECTO)
    assert destino.parent.is_dir()


@pytest.mark.parametrize(
    ("kwargs", "mensaje"),
    [({"batch_size": 0}, "batch_size"), ({"max_interval_s": 0}, "max_interval_s")],
)
def test_una_ventana_de_escritura_imposible_se_rechaza_al_construir(
    tmp_path: Path, kwargs: dict[str, Any], mensaje: str
) -> None:
    with pytest.raises(ValueError, match=mensaje):
        sink(tmp_path, **kwargs)
