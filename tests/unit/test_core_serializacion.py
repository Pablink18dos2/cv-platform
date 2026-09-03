"""La forma canonica en JSON de `Event` y `Metric`.

Se prueban las dos direcciones. Solo la ida deja pasar que la serializacion pierda un campo
que el constructor rellena por defecto: el objeto reconstruido saldria igual y el dict no.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest

from cvp.core import (
    Event,
    Metric,
    event_from_dict,
    event_to_dict,
    metric_from_dict,
    metric_to_dict,
)

MARCA = datetime(2026, 9, 3, 7, 20, 53, 123456, tzinfo=UTC)


def _eventos() -> list[Event]:
    return [
        Event(analyzer="traffic_flow", name="line_crossing", timestamp=MARCA),
        Event(analyzer="a", name="b", timestamp=MARCA, attributes={"direction": "norte"}),
        Event(
            analyzer="parking_occupancy",
            name="space_freed",
            timestamp=MARCA,
            attributes={"space": 7, "confidence": 0.82, "occupied": False, "prev": None},
        ),
        Event(
            analyzer="a",
            name="anidado",
            timestamp=MARCA,
            attributes={"lista": [1, 2, {"x": "y"}], "mapa": {"dentro": {"mas": [True]}}},
        ),
    ]


def _metricas() -> list[Metric]:
    return [
        Metric(analyzer="traffic_flow", name="vehicles_per_minute", value=12.5, timestamp=MARCA),
        Metric(analyzer="a", name="b", value=0.0, timestamp=MARCA, labels={"dir": "sur"}),
        Metric(analyzer="a", name="entero", value=3, timestamp=MARCA),
        Metric(analyzer="a", name="negativo", value=-1.5, timestamp=MARCA, labels={"k": ""}),
    ]


# --- Ida y vuelta -----------------------------------------------------------------------


@pytest.mark.parametrize("evento", _eventos())
def test_un_evento_sobrevive_a_la_ida_y_la_vuelta(evento: Event) -> None:
    assert event_from_dict(event_to_dict(evento)) == evento


@pytest.mark.parametrize("metrica", _metricas())
def test_una_metrica_sobrevive_a_la_ida_y_la_vuelta(metrica: Metric) -> None:
    assert metric_from_dict(metric_to_dict(metrica)) == metrica


@pytest.mark.parametrize("evento", _eventos())
def test_el_dict_de_un_evento_sobrevive_a_la_vuelta_y_la_ida(evento: Event) -> None:
    # La otra direccion: solo la ida dejaria pasar que la serializacion se coma un campo que
    # el constructor rellena por defecto.
    d = event_to_dict(evento)
    assert event_to_dict(event_from_dict(d)) == d


@pytest.mark.parametrize("metrica", _metricas())
def test_el_dict_de_una_metrica_sobrevive_a_la_vuelta_y_la_ida(metrica: Metric) -> None:
    d = metric_to_dict(metrica)
    assert metric_to_dict(metric_from_dict(d)) == d


def test_un_value_entero_vuelve_como_float() -> None:
    m = metric_from_dict(metric_to_dict(Metric(analyzer="a", name="b", value=3, timestamp=MARCA)))
    assert m.value == 3.0
    assert type(m.value) is float


# --- Es JSON de verdad ------------------------------------------------------------------


@pytest.mark.parametrize("objeto", [*_eventos(), *_metricas()])
def test_el_resultado_es_json_serializable(objeto: Any) -> None:
    # Lo que separa "un dict" de "un dict que se puede escribir en un fichero".
    d = event_to_dict(objeto) if isinstance(objeto, Event) else metric_to_dict(objeto)
    assert json.loads(json.dumps(d)) == d


# --- Timestamps -------------------------------------------------------------------------


def test_el_timestamp_se_escribe_en_utc_con_z() -> None:
    assert event_to_dict(_eventos()[0])["timestamp"] == "2026-09-03T07:20:53.123456Z"


def test_una_marca_en_otra_zona_se_escribe_normalizada_a_utc() -> None:
    madrid = datetime(2026, 9, 3, 9, 20, 53, 123456, tzinfo=timezone(timedelta(hours=2)))
    d = event_to_dict(Event(analyzer="a", name="b", timestamp=madrid))
    assert d["timestamp"] == "2026-09-03T07:20:53.123456Z"


@pytest.mark.parametrize(
    "texto",
    ["2026-09-03T07:20:53.123456Z", "2026-09-03T07:20:53.123456+00:00"],
)
def test_al_leer_se_aceptan_las_dos_formas(texto: str) -> None:
    # Postgres y muchas librerias escriben `+00:00`. Un lector estricto convertiria eso en un
    # bug de integracion.
    d = {"analyzer": "a", "name": "b", "timestamp": texto, "attributes": {}}
    assert event_from_dict(d).timestamp == MARCA


def test_un_timestamp_sin_zona_se_rechaza() -> None:
    d = {"analyzer": "a", "name": "b", "timestamp": "2026-09-03T07:20:53", "attributes": {}}
    with pytest.raises(ValueError, match="zona"):
        event_from_dict(d)


@pytest.mark.parametrize("basura", ["ayer", "", "2026-13-45T99:99:99Z", 1757000000, None])
def test_un_timestamp_que_no_es_iso_se_rechaza(basura: Any) -> None:
    d = {"analyzer": "a", "name": "b", "timestamp": basura, "attributes": {}}
    with pytest.raises(ValueError):
        event_from_dict(d)


# --- Los nombres de campo ---------------------------------------------------------------


def test_las_claves_del_evento_son_exactamente_sus_campos() -> None:
    # Asi, anadir un campo al tipo y olvidar la serializacion rompe la suite.
    assert set(event_to_dict(_eventos()[0])) == {f.name for f in dataclasses.fields(Event)}


def test_las_claves_de_la_metrica_son_exactamente_sus_campos() -> None:
    assert set(metric_to_dict(_metricas()[0])) == {f.name for f in dataclasses.fields(Metric)}


# --- Campos que faltan o sobran ---------------------------------------------------------


@pytest.mark.parametrize("campo", ["analyzer", "name", "timestamp", "attributes"])
def test_a_un_evento_al_que_le_falta_un_campo_se_le_dice_cual(campo: str) -> None:
    d = event_to_dict(_eventos()[0])
    del d[campo]
    with pytest.raises(ValueError, match=campo):
        event_from_dict(d)


@pytest.mark.parametrize("campo", ["analyzer", "name", "value", "timestamp", "labels"])
def test_a_una_metrica_a_la_que_le_falta_un_campo_se_le_dice_cual(campo: str) -> None:
    d = metric_to_dict(_metricas()[0])
    del d[campo]
    with pytest.raises(ValueError, match=campo):
        metric_from_dict(d)


def test_un_campo_de_mas_es_un_error_y_no_se_ignora() -> None:
    # Casi siempre es una errata o un esquema de otra version, y tragarselo pierde datos.
    d = event_to_dict(_eventos()[0]) | {"analizador": "typo"}
    with pytest.raises(ValueError, match="analizador"):
        event_from_dict(d)


# --- Los mapas vuelven inmutables -------------------------------------------------------


def test_los_attributes_vuelven_inmutables() -> None:
    evento = event_from_dict(event_to_dict(_eventos()[1]))
    assert isinstance(evento.attributes, MappingProxyType)
    with pytest.raises(TypeError):
        evento.attributes["x"] = 1  # type: ignore[index]


def test_los_labels_vuelven_inmutables() -> None:
    metrica = metric_from_dict(metric_to_dict(_metricas()[1]))
    assert isinstance(metrica.labels, MappingProxyType)


def test_mutar_el_dict_de_origen_no_cambia_el_objeto() -> None:
    d = event_to_dict(_eventos()[1])
    evento = event_from_dict(d)
    d["attributes"]["direction"] = "sur"
    assert evento.attributes["direction"] == "norte"
