"""El sink `jsonl`: una linea por evento y por metrica, y el viaje de vuelta.

Lo que fija este fichero es que el volcado se lee **sin traducir nada**: quitando el sobre
(`type` y `run_id`), lo que queda es exactamente lo que `event_from_dict` y
`metric_from_dict` esperan. Si alguien anadiera un campo al vuelco, los dos rechazan campos
desconocidos y este test lo cazaria.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cvp.core import AnalyzerOutput, Event, Frame, Metric, event_from_dict, metric_from_dict
from cvp.sinks.jsonl import JsonlSink

RUN = "20260824T142018Z-2808cc47"
INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)


def fotograma(indice: int = 0) -> Frame:
    return Frame(
        image=np.zeros((4, 4, 3), dtype=np.uint8),
        index=indice,
        timestamp=INICIO + timedelta(seconds=indice),
    )


def evento(segundos: float = 0.0, **atributos: object) -> Event:
    return Event(
        analyzer="traffic_flow",
        name="line_crossing",
        timestamp=INICIO + timedelta(seconds=segundos),
        attributes=atributos,
    )


def metrica(valor: float, **labels: str) -> Metric:
    return Metric(
        analyzer="parking_occupancy",
        name="free_spaces",
        value=valor,
        timestamp=INICIO,
        labels=labels,
    )


def leer(path: Path) -> list[Event | Metric]:
    """Reconstruye los objetos del fichero, que es la prueba de que el formato se basta."""
    objetos: list[Event | Metric] = []
    for linea in path.read_text(encoding="utf-8").splitlines():
        datos = json.loads(linea)
        assert datos.pop("run_id") == RUN
        tipo = datos.pop("type")
        objetos.append(event_from_dict(datos) if tipo == "event" else metric_from_dict(datos))
    return objetos


def test_una_linea_por_evento_y_por_metrica(tmp_path: Path) -> None:
    destino = tmp_path / "eventos.jsonl"
    sink = JsonlSink(destino)
    sink.write(
        RUN,
        fotograma(),
        AnalyzerOutput(events=(evento(), evento(1)), metrics=(metrica(3.0),)),
    )

    assert len(destino.read_text(encoding="utf-8").splitlines()) == 3


def test_el_fichero_se_lee_de_vuelta_y_reconstruye_los_objetos(tmp_path: Path) -> None:
    destino = tmp_path / "eventos.jsonl"
    eventos = (evento(0, line="principal", direction="norte", track_id=7),)
    metricas = (metrica(8.0, zone="norte"), metrica(3.0))
    JsonlSink(destino).write(RUN, fotograma(), AnalyzerOutput(events=eventos, metrics=metricas))

    assert leer(destino) == [*eventos, *metricas]


def test_cada_linea_lleva_su_run_id(tmp_path: Path) -> None:
    """Es lo que permite volcar dos ejecuciones al mismo fichero y separarlas despues."""
    destino = tmp_path / "eventos.jsonl"
    sink = JsonlSink(destino)
    sink.write(RUN, fotograma(), AnalyzerOutput(events=(evento(),)))
    otro = "20260824T142530Z-9f1b0c33"
    sink.write(otro, fotograma(1), AnalyzerOutput(events=(evento(1),)))

    ids = [json.loads(linea)["run_id"] for linea in destino.read_text("utf-8").splitlines()]
    assert ids == [RUN, otro]


def test_lo_que_se_escribe_es_la_forma_canonica_de_19_mas_el_sobre(tmp_path: Path) -> None:
    destino = tmp_path / "eventos.jsonl"
    JsonlSink(destino).write(RUN, fotograma(), AnalyzerOutput(metrics=(metrica(3.0),)))

    datos = json.loads(destino.read_text(encoding="utf-8"))
    assert set(datos) == {"type", "run_id", "analyzer", "name", "value", "timestamp", "labels"}
    assert datos["timestamp"] == "2026-08-24T14:20:00Z"


def test_escribir_dos_veces_anade_al_final_y_no_pisa(tmp_path: Path) -> None:
    destino = tmp_path / "eventos.jsonl"
    sink = JsonlSink(destino)
    for i in range(3):
        sink.write(RUN, fotograma(i), AnalyzerOutput(events=(evento(i),)))

    assert len(leer(destino)) == 3


def test_una_salida_vacia_no_escribe_una_linea_en_blanco(tmp_path: Path) -> None:
    destino = tmp_path / "eventos.jsonl"
    sink = JsonlSink(destino)
    sink.write(RUN, fotograma(), AnalyzerOutput.empty())
    sink.flush()

    assert not destino.exists()


def test_el_sink_crea_el_directorio_al_arrancar(tmp_path: Path) -> None:
    """La configuracion no crea directorios (#3), pero construir el sink a mano tampoco
    puede exigir que exista."""
    destino = tmp_path / "sin" / "crear" / "eventos.jsonl"
    JsonlSink(destino)
    assert destino.parent.is_dir()


def test_no_deja_el_fichero_abierto_entre_escrituras(tmp_path: Path) -> None:
    """El `Protocol` de un sink no tiene `close()`: un descriptor abierto durante dias es lo
    que impide rotar o borrar el fichero sin reiniciar el worker."""
    destino = tmp_path / "eventos.jsonl"
    sink = JsonlSink(destino)
    sink.write(RUN, fotograma(), AnalyzerOutput(events=(evento(),)))
    destino.unlink()  # en Windows esto falla si alguien lo tiene abierto

    sink.write(RUN, fotograma(1), AnalyzerOutput(events=(evento(1),)))
    assert len(leer(destino)) == 1


def test_from_config_acepta_y_descarta_los_extras_de_la_fabrica(tmp_path: Path) -> None:
    """A un volcado de depuracion el proyecto no le anade nada: el fichero ya es de un
    proyecto por la ruta a la que apunta."""

    class Cfg:
        type = "jsonl"
        path = tmp_path / "eventos.jsonl"

    sink = JsonlSink.from_config(Cfg(), project_id="p", project_name="Proyecto")
    assert sink.path == tmp_path / "eventos.jsonl"


@pytest.mark.parametrize("acentuado", ["Parking y tráfico", "línea"])
def test_los_acentos_se_escriben_tal_cual(tmp_path: Path, acentuado: str) -> None:
    """Un volcado que hay que leer con `tail -f` no puede estar lleno de `\\u00e1`."""
    destino = tmp_path / "eventos.jsonl"
    JsonlSink(destino).write(RUN, fotograma(), AnalyzerOutput(events=(evento(0, z=acentuado),)))
    assert acentuado in destino.read_text(encoding="utf-8")
