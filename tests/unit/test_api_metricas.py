"""`GET /projects/{id}/metrics` y `/metrics/latest`: la serie regular y los cuatro bordes.

Los cuatro bordes de `metric_minute` que #12 recoge (de #10 y #29) se reproducen aqui
**con el sink real**, no con filas escritas a mano: la fila fantasma con `samples == 0`
sale de un lote descartado, igual que en produccion, y los `seconds > 60` de dos
ejecuciones sobre el mismo minuto.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from api_soporte import OTRO_RUN, PROYECTO, RUN, Base, en, fotograma, metrica
from fastapi.testclient import TestClient

import cvp.sinks.sqlite
from cvp.api.routers.metrics import parsear_labels
from cvp.api.ventana import RANGO_MAXIMO
from cvp.core import AnalyzerOutput

URL = f"/api/v1/projects/{PROYECTO}/metrics"
COLUMNAS = {
    "minute",
    "samples",
    "seconds",
    "sum_value",
    "mean_value",
    "min_value",
    "max_value",
    "integral",
}


def serie(cliente: TestClient, **params: Any) -> dict[str, Any]:
    respuesta = cliente.get(URL, params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# --- Borde 1: la rejilla es regular y los huecos son `null` ----------------------------


def test_tres_minutos_con_datos_y_dos_huecos_dan_cinco_puntos(
    base: Base, cliente: TestClient
) -> None:
    """Datos en :20, :22 y :24; nada en :21 ni :23. Cinco puntos, dos de ellos `null`."""
    base.escribir_metricas(
        metrica(10.0, 0), metrica(20.0, 5), metrica(30.0, 120), metrica(40.0, 240)
    )
    respuesta = serie(cliente, **{"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:24:00Z"})

    assert respuesta["from"] == "2026-08-24T14:20:00Z"
    assert respuesta["to"] == "2026-08-24T14:24:00Z"
    assert respuesta["step"] == "minute"
    (unica,) = respuesta["series"]
    assert (unica["analyzer"], unica["name"], unica["labels"]) == (
        "traffic_flow",
        "vehicles_in_scene",
        {},
    )
    puntos = unica["points"]
    assert len(puntos) == 5
    assert [p is None for p in puntos] == [False, True, False, True, False]
    assert [p["minute"] for p in puntos if p] == [
        "2026-08-24T14:20:00Z",
        "2026-08-24T14:22:00Z",
        "2026-08-24T14:24:00Z",
    ]


def test_un_sentido_sin_cruces_no_tiene_serie_y_el_otro_lleva_el_conteo_en_sum_value(
    base: Base, cliente: TestClient
) -> None:
    """El borde 1 de #10: cinco cruces al norte y ninguno al sur. Hay una serie `norte`
    con `sum_value == 5` y **ninguna** `sur`: la API no inventa la serie ausente, lo
    dice `api.md`, y el frontend la pinta a cero."""
    cruces = [
        metrica(1.0, s, name="crossings", labels={"line": "principal", "direction": "norte"})
        for s in (3, 7, 12, 30, 55)
    ]
    base.escribir_metricas(*cruces, metrica(4.0, 59))
    respuesta = serie(cliente, **{"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:20:00Z"})

    por_nombre = {(s["name"], s["labels"].get("direction")): s for s in respuesta["series"]}
    assert set(por_nombre) == {("crossings", "norte"), ("vehicles_in_scene", None)}
    (punto,) = por_nombre[("crossings", "norte")]["points"]
    assert punto["sum_value"] == 5.0
    assert punto["samples"] == 5


# --- Borde 2: todas las columnas, y la API no elige por el frontend --------------------


def test_cada_punto_lleva_todas_las_columnas_del_agregado(base: Base, cliente: TestClient) -> None:
    """Una muestra cada 10 s de un gauge que sube: `mean_value` es la media en el tiempo
    (lo que vale para un gauge), `sum_value` la suma (lo que vale para un conteo), y las
    dos vienen, con `samples`, `seconds`, `min`, `max` e `integral`."""
    base.escribir_metricas(*(metrica(float(i), i * 10) for i in range(7)))
    (punto,) = serie(cliente, **{"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:20:00Z"})[
        "series"
    ][0]["points"]

    assert set(punto) == COLUMNAS
    assert punto["samples"] == 6  # la de las :21:00 es del minuto siguiente
    assert punto["sum_value"] == 15.0
    assert (punto["min_value"], punto["max_value"]) == (0.0, 5.0)
    assert punto["seconds"] == pytest.approx(60.0)
    # Cada muestra pesa su tramo: 0x5 + 1x10 + 2x10 + 3x10 + 4x10 + 5x15 = 175 en 60 s.
    assert punto["integral"] == pytest.approx(175.0)
    assert punto["mean_value"] == pytest.approx(175.0 / 60.0)
    assert punto["mean_value"] != pytest.approx(punto["sum_value"] / punto["samples"])


# --- Borde 3: `samples == 0` es un hueco -------------------------------------------------


def test_una_fila_fantasma_con_samples_cero_sale_como_null(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La fila fantasma de #29, reproducida como en produccion: un lote se descarta (el
    disco falla, el runner sigue) pero el agregador ya avanzo, y el lote siguiente
    escribe el **tramo** de la muestra perdida en un minuto que no tiene su muestra.
    Queda `samples=0, seconds=15, mean=40`: una media inventada. La API la sirve como
    `null`, no como un 40."""
    almacen = base.sink(batch_size=1)

    def falla(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("no queda espacio en el disco")

    with pytest.MonkeyPatch.context() as parche:
        parche.setattr(cvp.sinks.sqlite, "escribir_lote", falla)
        with pytest.raises(RuntimeError):
            almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(40.0, 60),)))
    almacen.write(RUN, fotograma(1), AnalyzerOutput(metrics=(metrica(7.0, 130),)))

    from sqlmodel import Session

    from cvp.storage import minutos_de

    with Session(almacen.engine) as session:
        fantasma, real = minutos_de(session, PROYECTO)
        assert (fantasma.samples, fantasma.seconds, fantasma.mean_value) == (0, 15.0, 40.0)
        assert real.samples == 1

    puntos = serie(cliente, **{"from": "2026-08-24T14:21:00Z", "to": "2026-08-24T14:22:00Z"})[
        "series"
    ][0]["points"]
    assert puntos[0] is None
    assert puntos[1]["samples"] == 1


def test_latest_tampoco_devuelve_una_fila_fantasma(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El KPI del dashboard es `latest`: si la ultima fila de una serie es fantasma, el
    ultimo valor es el minuto anterior con muestras, no la media inventada."""
    almacen = base.sink(batch_size=1)
    almacen.write(RUN, fotograma(0), AnalyzerOutput(metrics=(metrica(7.0, 0),)))

    def falla(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("disco")

    with pytest.MonkeyPatch.context() as parche:
        parche.setattr(cvp.sinks.sqlite, "escribir_lote", falla)
        with pytest.raises(RuntimeError):
            almacen.write(RUN, fotograma(1), AnalyzerOutput(metrics=(metrica(40.0, 60),)))
    # Cierra el tramo de la muestra perdida (fila fantasma en :21) sin muestra nueva en :21.
    with pytest.MonkeyPatch.context() as parche:
        parche.setattr(cvp.sinks.sqlite, "escribir_lote", falla)
        with pytest.raises(RuntimeError):
            almacen.write(RUN, fotograma(2), AnalyzerOutput(metrics=(metrica(50.0, 130),)))
    almacen.write(RUN, fotograma(3), AnalyzerOutput(metrics=(metrica(9.0, 190),)))

    (ultimo,) = cliente.get(f"{URL}/latest").json()["latest"]
    assert ultimo["minute"] == "2026-08-24T14:23:00Z"
    assert ultimo["samples"] == 1
    assert ultimo["sum_value"] == 9.0


# --- Borde 4: `seconds` puede pasar de 60 y se sirve tal cual --------------------------


def test_dos_ejecuciones_sobre_el_mismo_minuto_suman_mas_de_60_segundos(
    base: Base, cliente: TestClient
) -> None:
    """Un fichero reprocesado dos veces: cada ejecucion cubre los 60 s del minuto y el
    agregado suma 120. La API no lo recorta ni lo normaliza; lo documenta."""
    muestras = [metrica(2.0, s) for s in range(0, 61, 2)]  # la de :60 cierra el tramo
    base.escribir_metricas(*muestras, run_id=RUN)
    base.escribir_metricas(*muestras, run_id=OTRO_RUN)

    (punto, _siguiente) = serie(
        cliente, **{"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:21:00Z"}
    )["series"][0]["points"]
    assert punto["seconds"] == pytest.approx(120.0)
    assert punto["samples"] == 60
    assert punto["mean_value"] == pytest.approx(2.0)


# --- Filtros ---------------------------------------------------------------------------


def _poblar_dos_lineas(base: Base) -> None:
    base.escribir_metricas(
        metrica(1.0, 1, name="crossings", labels={"line": "principal", "direction": "norte"}),
        metrica(1.0, 2, name="crossings", labels={"line": "principal", "direction": "sur"}),
        metrica(1.0, 3, name="crossings", labels={"line": "lateral", "direction": "norte"}),
        metrica(3.0, 4),
        metrica(1.0, 5, analyzer="plazas", name="zone_occupied", labels={"zone": "b1"}),
    )


def test_labels_filtra_por_igualdad_de_las_claves_dadas(base: Base, cliente: TestClient) -> None:
    _poblar_dos_lineas(base)
    ventana = {"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:20:00Z"}

    norte = serie(cliente, labels="direction:norte", **ventana)["series"]
    assert sorted(s["labels"]["line"] for s in norte) == ["lateral", "principal"]

    una = serie(cliente, labels="line:principal,direction:sur", **ventana)["series"]
    assert [s["labels"] for s in una] == [{"direction": "sur", "line": "principal"}]

    assert serie(cliente, labels="direction:oeste", **ventana)["series"] == []


def test_analyzer_y_name_filtran_y_sin_filtros_salen_todas_las_series(
    base: Base, cliente: TestClient
) -> None:
    _poblar_dos_lineas(base)
    ventana = {"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:20:00Z"}

    assert len(serie(cliente, **ventana)["series"]) == 5
    assert len(serie(cliente, analyzer="plazas", **ventana)["series"]) == 1
    assert len(serie(cliente, name="crossings", **ventana)["series"]) == 3
    assert serie(cliente, analyzer="plazas", name="crossings", **ventana)["series"] == []


def test_las_series_salen_en_un_orden_estable(base: Base, cliente: TestClient) -> None:
    _poblar_dos_lineas(base)
    ventana = {"from": "2026-08-24T14:20:00Z", "to": "2026-08-24T14:20:00Z"}
    claves = [(s["analyzer"], s["name"], s["labels"]) for s in serie(cliente, **ventana)["series"]]
    assert claves == sorted(claves, key=lambda c: (c[0], c[1], sorted(c[2].items())))


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        (None, {}),
        ("", {}),
        ("direction:norte", {"direction": "norte"}),
        (" line : principal , direction:sur ", {"line": "principal", "direction": "sur"}),
        ("url:http://x:1", {"url": "http://x:1"}),  # solo el primer `:` separa
    ],
)
def test_parsear_labels(crudo: str | None, esperado: dict[str, str]) -> None:
    assert parsear_labels(crudo) == esperado


def test_labels_mal_formadas_son_422_con_el_campo(cliente: TestClient, base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    respuesta = cliente.get(URL, params={"labels": "direction"})
    assert respuesta.status_code == 422
    cuerpo = respuesta.json()["error"]
    assert cuerpo["code"] == "validation_error"
    assert cuerpo["details"] == [
        {"field": "labels", "message": "'direction' no tiene la forma clave:valor"}
    ]


# --- La ventana ------------------------------------------------------------------------


def test_sin_from_ni_to_es_la_ultima_hora_segun_el_reloj_inyectado(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    base.reloj.ahora = en(0) + timedelta(minutes=30, seconds=15)

    respuesta = serie(cliente)
    assert respuesta["from"] == "2026-08-24T13:50:00Z"
    assert respuesta["to"] == "2026-08-24T14:50:00Z"
    (unica,) = respuesta["series"]
    assert len(unica["points"]) == 61
    assert unica["points"][30]["minute"] == "2026-08-24T14:20:00Z"


def test_solo_from_llega_hasta_ahora_y_solo_to_cubre_la_hora_anterior(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    base.reloj.ahora = en(0) + timedelta(minutes=10)
    desde_solo = serie(cliente, **{"from": "2026-08-24T14:00:00Z"})
    assert desde_solo["from"] == "2026-08-24T14:00:00Z"
    assert desde_solo["to"] == "2026-08-24T14:30:00Z"
    hasta_solo = serie(cliente, to="2026-08-24T14:00:00Z")
    assert hasta_solo["from"] == "2026-08-24T13:00:00Z"
    assert hasta_solo["to"] == "2026-08-24T14:00:00Z"


def test_from_y_to_se_truncan_al_minuto_y_los_dos_entran(base: Base, cliente: TestClient) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    respuesta = serie(cliente, **{"from": "2026-08-24T14:20:30Z", "to": "2026-08-24T14:24:10Z"})
    assert respuesta["from"] == "2026-08-24T14:20:00Z"
    assert respuesta["to"] == "2026-08-24T14:24:00Z"
    assert len(respuesta["series"][0]["points"]) == 5


def test_una_zona_horaria_distinta_de_utc_se_convierte(base: Base, cliente: TestClient) -> None:
    """`14:20+02:00` es `12:20Z`: la rejilla y la consulta van en UTC, no en la hora del
    cliente. El `+` va codificado como `%2B`, que es lo que exige una URL."""
    base.escribir_metricas(metrica(5.0, 0))
    local = "2026-08-24T16:20:00+02:00"
    respuesta = serie(cliente, **{"from": local, "to": local})
    assert (respuesta["from"], respuesta["to"]) == ("2026-08-24T14:20:00Z",) * 2
    assert respuesta["series"][0]["points"][0]["sum_value"] == 5.0


def test_from_posterior_a_to_es_400_invalid_range(cliente: TestClient, base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    respuesta = cliente.get(
        URL, params={"from": "2026-08-24T15:00:00Z", "to": "2026-08-24T14:00:00Z"}
    )
    assert respuesta.status_code == 400
    assert respuesta.json() == {
        "error": {
            "code": "invalid_range",
            "message": "from (2026-08-24T15:00:00Z) es posterior a to (2026-08-24T14:00:00Z)",
        }
    }


def test_mas_de_siete_dias_es_400_con_mensaje(cliente: TestClient, base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    justo = cliente.get(URL, params={"from": "2026-08-17T14:20:00Z", "to": "2026-08-24T14:20:00Z"})
    assert justo.status_code == 200
    assert len(justo.json()["series"][0]["points"]) == int(RANGO_MAXIMO.total_seconds() / 60) + 1

    de_mas = cliente.get(URL, params={"from": "2026-08-17T14:19:00Z", "to": "2026-08-24T14:20:00Z"})
    assert de_mas.status_code == 400
    error = de_mas.json()["error"]
    assert error["code"] == "invalid_range"
    assert "7 dias" in error["message"]


def test_una_fecha_sin_zona_o_ilegible_es_422_con_el_campo(cliente: TestClient, base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    for valor in ["2026-08-24T14:20:00", "ayer", "2026-13-01T00:00:00Z"]:
        respuesta = cliente.get(URL, params={"from": valor})
        assert respuesta.status_code == 422, valor
        error = respuesta.json()["error"]
        assert error["code"] == "validation_error"
        assert [d["field"] for d in error["details"]] == ["from"]


def test_step_solo_admite_minute(cliente: TestClient, base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0))
    respuesta = cliente.get(URL, params={"step": "hour"})
    assert respuesta.status_code == 422
    assert [d["field"] for d in respuesta.json()["error"]["details"]] == ["step"]


def test_un_proyecto_que_no_existe_es_404_tambien_en_metricas(cliente: TestClient) -> None:
    for sufijo in ["/metrics", "/metrics/latest"]:
        respuesta = cliente.get(f"/api/v1/projects/no-existe{sufijo}")
        assert respuesta.status_code == 404
        assert respuesta.json()["error"]["code"] == "project_not_found"


# --- `latest` ----------------------------------------------------------------------------


def test_latest_da_el_ultimo_minuto_con_muestras_de_cada_serie(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(
        metrica(1.0, 0, name="crossings", labels={"line": "p", "direction": "norte"}),
        metrica(1.0, 61, name="crossings", labels={"line": "p", "direction": "norte"}),
        metrica(1.0, 62, name="crossings", labels={"line": "p", "direction": "norte"}),
        metrica(1.0, 5, name="crossings", labels={"line": "p", "direction": "sur"}),
        metrica(8.0, 0),
        metrica(6.0, 200),
    )
    respuesta = cliente.get(f"{URL}/latest")
    assert respuesta.status_code == 200
    ultimos = {(u["name"], u["labels"].get("direction")): u for u in respuesta.json()["latest"]}
    assert set(ultimos) == {
        ("crossings", "norte"),
        ("crossings", "sur"),
        ("vehicles_in_scene", None),
    }
    assert ultimos[("crossings", "norte")]["minute"] == "2026-08-24T14:21:00Z"
    assert ultimos[("crossings", "norte")]["sum_value"] == 2.0
    assert ultimos[("crossings", "sur")]["minute"] == "2026-08-24T14:20:00Z"
    assert ultimos[("vehicles_in_scene", None)]["minute"] == "2026-08-24T14:23:00Z"
    assert set(ultimos[("vehicles_in_scene", None)]) == COLUMNAS | {"analyzer", "name", "labels"}


def test_latest_con_un_proyecto_sin_metricas_es_una_lista_vacia(
    base: Base, cliente: TestClient
) -> None:
    from api_soporte import evento

    base.escribir_eventos(evento(0))
    assert cliente.get(f"{URL}/latest").json() == {"project_id": PROYECTO, "latest": []}
