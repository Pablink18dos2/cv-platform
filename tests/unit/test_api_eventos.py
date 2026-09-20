"""`GET /projects/{id}/events`: la forma canonica de #19, y la paginacion hacia atras.

El criterio de #12 es recorrer 1200 eventos en tres paginas de 500 sin repetir ni saltar
ninguno. Se comprueba eso y el borde que un cursor por timestamp tiene de verdad: dos
eventos del mismo fotograma comparten timestamp, y la pagina no puede cortar entre ellos.
"""

from __future__ import annotations

from typing import Any

from api_soporte import OTRO_RUN, PROYECTO, RUN, Base, en, evento
from fastapi.testclient import TestClient

from cvp.core import event_from_dict, event_to_dict

URL = f"/api/v1/projects/{PROYECTO}/events"


def pagina(cliente: TestClient, **params: Any) -> dict[str, Any]:
    respuesta = cliente.get(URL, params=params)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# --- La forma -----------------------------------------------------------------------------


def test_cada_evento_es_la_forma_canonica_de_core_mas_run_id(
    base: Base, cliente: TestClient
) -> None:
    """Exactamente `event_to_dict(evento)` y `run_id`: nada mas, nada menos, y
    `event_from_dict` lo vuelve a leer. Un timestamp con microsegundos y otro sin ellos,
    porque son los dos formatos que `isoformat` escribe distinto."""
    con_micro = evento(1.5, attributes={"direction": "norte", "track_id": 7, "label": "car"})
    sin_micro = evento(3, name="space_freed", analyzer="plazas", attributes={"zone": "b1"})
    base.escribir_eventos(con_micro, sin_micro)

    eventos = pagina(cliente)["events"]
    assert eventos == [
        {**event_to_dict(sin_micro), "run_id": RUN},
        {**event_to_dict(con_micro), "run_id": RUN},
    ]
    assert eventos[1]["timestamp"] == "2026-08-24T14:20:01.500000Z"
    for datos in eventos:
        run_id = datos.pop("run_id")
        assert run_id == RUN
        assert event_from_dict(datos).analyzer == datos["analyzer"]


def test_sin_eventos_la_lista_esta_vacia_y_no_hay_cursor(base: Base, cliente: TestClient) -> None:
    from api_soporte import metrica

    base.escribir_metricas(metrica(1.0, 0))
    assert pagina(cliente) == {"project_id": PROYECTO, "events": [], "next_before": None}


def test_los_eventos_van_por_timestamp_descendente(base: Base, cliente: TestClient) -> None:
    base.escribir_eventos(evento(5), evento(1), evento(9))
    assert [e["timestamp"][17:19] for e in pagina(cliente)["events"]] == ["09", "05", "01"]


# --- La paginacion --------------------------------------------------------------------------


def test_1200_eventos_en_tres_paginas_sin_repetir_ni_saltar_ninguno(
    base: Base, cliente: TestClient
) -> None:
    """Cada evento lleva su indice en `attributes`, asi que se comprueba el conjunto
    exacto y no solo la cuenta."""
    base.escribir_eventos(*(evento(i * 0.7, attributes={"i": i}) for i in range(1200)))

    vistos: list[int] = []
    before: str | None = None
    paginas = 0
    while True:
        respuesta = pagina(cliente, limit=500, **({"before": before} if before else {}))
        vistos.extend(e["attributes"]["i"] for e in respuesta["events"])
        paginas += 1
        before = respuesta["next_before"]
        if before is None:
            break

    assert paginas == 3
    assert vistos == list(range(1199, -1, -1))


def test_la_pagina_no_corta_nunca_un_timestamp_compartido(base: Base, cliente: TestClient) -> None:
    """Tres cruces en el mismo fotograma (mismo timestamp) justo en la frontera de la
    pagina: la pagina se alarga hasta los tres y el cursor deja fuera exactamente lo
    anterior. Ni se repite ni se pierde ninguno."""
    base.escribir_eventos(
        evento(1, attributes={"i": 1}),
        evento(2, attributes={"i": 2}),
        evento(5, attributes={"i": 3}),
        evento(5, attributes={"i": 4}),
        evento(5, attributes={"i": 5}),
        evento(9, attributes={"i": 6}),
    )
    primera = pagina(cliente, limit=2)
    assert [e["attributes"]["i"] for e in primera["events"]] == [6, 5, 4, 3]
    assert primera["next_before"] == "2026-08-24T14:20:05Z"

    segunda = pagina(cliente, limit=2, before=primera["next_before"])
    assert [e["attributes"]["i"] for e in segunda["events"]] == [2, 1]
    assert segunda["next_before"] is None


def test_una_pagina_exacta_no_anuncia_pagina_siguiente(base: Base, cliente: TestClient) -> None:
    base.escribir_eventos(evento(1), evento(2), evento(3))
    respuesta = pagina(cliente, limit=3)
    assert len(respuesta["events"]) == 3
    assert respuesta["next_before"] is None


def test_before_es_exclusivo(base: Base, cliente: TestClient) -> None:
    base.escribir_eventos(evento(1), evento(2), evento(3))
    respuesta = pagina(cliente, before="2026-08-24T14:20:02Z")
    assert [e["timestamp"] for e in respuesta["events"]] == ["2026-08-24T14:20:01Z"]


def test_limit_por_encima_de_500_o_por_debajo_de_1_es_422(base: Base, cliente: TestClient) -> None:
    base.escribir_eventos(evento(1))
    for limite in (0, 501, -3):
        respuesta = cliente.get(URL, params={"limit": limite})
        assert respuesta.status_code == 422, limite
        assert [d["field"] for d in respuesta.json()["error"]["details"]] == ["limit"]


# --- Los filtros ---------------------------------------------------------------------------


def _poblar(base: Base) -> None:
    base.escribir_eventos(
        evento(1, name="line_crossing"),
        evento(2, name="line_crossing", attributes={"direction": "sur"}),
        evento(3, analyzer="plazas", name="space_occupied", attributes={"zone": "b1"}),
        evento(4, analyzer="plazas", name="space_freed", attributes={"zone": "b1"}),
    )
    base.escribir_eventos(evento(700, name="line_crossing"), run_id=OTRO_RUN)


def test_analyzer_name_from_y_to_filtran(base: Base, cliente: TestClient) -> None:
    _poblar(base)
    assert len(pagina(cliente)["events"]) == 5
    assert [e["name"] for e in pagina(cliente, analyzer="plazas")["events"]] == [
        "space_freed",
        "space_occupied",
    ]
    assert len(pagina(cliente, name="line_crossing")["events"]) == 3
    ventana = pagina(cliente, **{"from": "2026-08-24T14:20:02Z", "to": "2026-08-24T14:20:03Z"})
    assert [e["timestamp"][17:19] for e in ventana["events"]] == ["03", "02"]


def test_los_eventos_de_todas_las_runs_del_proyecto_salen_con_su_run_id(
    base: Base, cliente: TestClient
) -> None:
    _poblar(base)
    eventos = pagina(cliente)["events"]
    assert eventos[0]["run_id"] == OTRO_RUN
    assert {e["run_id"] for e in eventos[1:]} == {RUN}


def test_los_eventos_de_otro_proyecto_no_se_mezclan(base: Base, cliente: TestClient) -> None:
    _poblar(base)
    base.escribir_eventos(
        evento(50), project_id="otro", project_name="Otro", run_id=OTRO_RUN[:-1] + "0"
    )
    assert len(pagina(cliente)["events"]) == 5
    assert len(cliente.get("/api/v1/projects/otro/events").json()["events"]) == 1


def test_from_posterior_a_to_es_400_invalid_range(base: Base, cliente: TestClient) -> None:
    _poblar(base)
    respuesta = cliente.get(
        URL, params={"from": "2026-08-24T15:00:00Z", "to": "2026-08-24T14:00:00Z"}
    )
    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["code"] == "invalid_range"


def test_una_zona_horaria_distinta_de_utc_se_convierte(base: Base, cliente: TestClient) -> None:
    _poblar(base)
    respuesta = pagina(cliente, before="2026-08-24T16:20:02+02:00")  # = 14:20:02Z
    assert [e["timestamp"] for e in respuesta["events"]] == ["2026-08-24T14:20:01Z"]


def test_un_proyecto_que_no_existe_es_404(cliente: TestClient) -> None:
    respuesta = cliente.get("/api/v1/projects/no-existe/events")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["code"] == "project_not_found"


def test_el_timestamp_de_ida_y_vuelta_es_el_mismo_instante(base: Base, cliente: TestClient) -> None:
    """Lo que se serializa vuelve a ser el mismo `datetime` UTC que entro."""
    original = evento(12.345678)
    base.escribir_eventos(original)
    (datos,) = pagina(cliente)["events"]
    datos.pop("run_id")
    assert event_from_dict(datos).timestamp == en(12.345678)
