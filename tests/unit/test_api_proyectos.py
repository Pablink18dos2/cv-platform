"""`GET /projects` y `GET /projects/{id}`: la lista, el detalle, y el `status` con reloj.

El `status` se deriva de la ultima `run` y de un reloj que estos tests controlan: los
tres estados se comprueban sin esperar 90 segundos y sin `datetime.now()` por medio.
"""

from __future__ import annotations

from datetime import timedelta

from api_soporte import NOMBRE, OTRO_RUN, PROYECTO, RUN, Base, en, evento, metrica
from fastapi.testclient import TestClient
from sqlmodel import Session

from cvp.api.routers.projects import UMBRAL_RUNNING, estado_de
from cvp.storage import Run, asegurar_proyecto, asegurar_run, crear_engine, en_utc

# --- La lista ------------------------------------------------------------------------


def test_con_la_base_vacia_la_lista_esta_vacia_y_no_es_un_error(cliente: TestClient) -> None:
    respuesta = cliente.get("/api/v1/projects")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"projects": []}


def test_con_la_base_ausente_la_api_arranca_y_responde_vacio(base: Base) -> None:
    """La API arranca contra un `DATABASE_URL` cuyo fichero todavia no existe (el worker
    no ha corrido nunca) y responde listas vacias, no un 500 por tabla inexistente."""
    from cvp.api import crear_app

    ajustes = base.settings(database_url=f"sqlite:///{(base.directorio / 'nueva.db').as_posix()}")
    with TestClient(crear_app(ajustes, clock=base.reloj)) as cliente:
        assert cliente.get("/api/v1/projects").json() == {"projects": []}


def test_la_lista_lleva_id_nombre_estado_y_ultima_escritura(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(metrica(3.0, 0), metrica(5.0, 10))
    base.reloj.ahora = en(10) + timedelta(seconds=30)

    (proyecto,) = cliente.get("/api/v1/projects").json()["projects"]
    assert proyecto == {
        "id": PROYECTO,
        "name": NOMBRE,
        "status": "running",
        "last_seen_at": "2026-08-24T14:20:10Z",
    }


def test_la_lista_ordena_por_id_y_cada_proyecto_mira_su_propia_ultima_run(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(metrica(1.0, 0), project_id="b-segundo", project_name="B")
    base.escribir_metricas(
        metrica(1.0, 600), project_id="a-primero", project_name="A", run_id=OTRO_RUN
    )
    base.reloj.ahora = en(600) + timedelta(seconds=10)

    proyectos = cliente.get("/api/v1/projects").json()["projects"]
    assert [(p["id"], p["status"]) for p in proyectos] == [
        ("a-primero", "running"),
        ("b-segundo", "stopped"),
    ]


# --- El `status`, con el reloj inyectado ------------------------------------------------


def _ultima_run(base: Base) -> Run:
    motor = crear_engine(base.url)
    try:
        with Session(motor) as session:
            ejecucion = session.get(Run, RUN)
            assert ejecucion is not None
            session.expunge(ejecucion)
            return ejecucion
    finally:
        motor.dispose()


def test_running_si_ha_escrito_en_los_ultimos_90_segundos(base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0), metrica(2.0, 20))
    ejecucion = _ultima_run(base)
    assert en_utc(ejecucion.ended_at) == en(20)  # la ultima muestra, no el apagado

    assert estado_de(ejecucion, en(20)) == "running"
    assert estado_de(ejecucion, en(20) + UMBRAL_RUNNING) == "running"


def test_stopped_si_escribio_pero_hace_mas_de_90_segundos(base: Base) -> None:
    base.escribir_metricas(metrica(1.0, 0), metrica(2.0, 20))
    ejecucion = _ultima_run(base)

    assert estado_de(ejecucion, en(20) + UMBRAL_RUNNING + timedelta(seconds=1)) == "stopped"
    assert estado_de(ejecucion, en(20) + timedelta(days=3)) == "stopped"


def test_stale_si_la_run_existe_pero_no_ha_escrito_nada(base: Base) -> None:
    """Una ejecucion sin `ended_at`: arranco y no ha escrito ninguna muestra. El sink de
    hoy no la produce (la fila de `run` nace con su primer lote, que ya pone `ended_at`),
    asi que se crea con `asegurar_run` de `cvp.storage`, que es lo que haria un runner
    que registrara la ejecucion al arrancar."""
    motor = crear_engine(base.url)
    try:
        from cvp.storage import crear_esquema

        crear_esquema(motor)
        with Session(motor) as session:
            asegurar_proyecto(session, PROYECTO, NOMBRE, en(0))
            asegurar_run(session, RUN, PROYECTO)
            session.commit()
    finally:
        motor.dispose()
    ejecucion = _ultima_run(base)
    assert ejecucion.ended_at is None

    assert estado_de(ejecucion, en(0)) == "stale"
    assert estado_de(ejecucion, en(0) + timedelta(days=3)) == "stale"
    assert estado_de(None, en(0)) == "stale"


def test_los_tres_estados_salen_por_la_api_moviendo_el_reloj(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_eventos(evento(0), evento(40))

    base.reloj.ahora = en(40) + timedelta(seconds=89)
    assert cliente.get("/api/v1/projects").json()["projects"][0]["status"] == "running"

    base.reloj.ahora = en(40) + timedelta(seconds=91)
    assert cliente.get("/api/v1/projects").json()["projects"][0]["status"] == "stopped"

    base.reloj.ahora = en(40) - timedelta(seconds=30)  # el reloj de la API va atrasado
    assert cliente.get("/api/v1/projects").json()["projects"][0]["status"] == "running"


def test_el_status_mira_la_ultima_run_y_no_la_primera(base: Base, cliente: TestClient) -> None:
    """Un reinicio estrena `run_id`: lo que cuenta es la ejecucion mas reciente, que es
    la mayor por `id` (el `run_id` ordena por tiempo)."""
    base.escribir_metricas(metrica(1.0, 0), run_id=RUN)
    base.escribir_metricas(metrica(1.0, 700), run_id=OTRO_RUN)
    base.reloj.ahora = en(700) + timedelta(seconds=5)

    detalle = cliente.get(f"/api/v1/projects/{PROYECTO}").json()
    assert detalle["status"] == "running"
    assert detalle["last_run"]["id"] == OTRO_RUN
    assert detalle["last_seen_at"] == "2026-08-24T14:31:40Z"


# --- El detalle ----------------------------------------------------------------------


def test_el_detalle_lleva_la_ultima_run_y_los_analizadores_que_han_escrito(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(
        metrica(1.0, 0, analyzer="traffic_flow"),
        metrica(0.0, 1, analyzer="plazas", name="zone_occupied", labels={"zone": "b1"}),
    )
    base.reloj.ahora = en(1) + timedelta(seconds=5)

    respuesta = cliente.get(f"/api/v1/projects/{PROYECTO}")
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "id": PROYECTO,
        "name": NOMBRE,
        "status": "running",
        "last_seen_at": "2026-08-24T14:20:01Z",
        "created_at": "2026-08-24T14:20:18Z",  # el instante del primer `run_id`
        "last_run": {
            "id": RUN,
            "started_at": "2026-08-24T14:20:18Z",
            "ended_at": "2026-08-24T14:20:01Z",
        },
        "analyzers": ["plazas", "traffic_flow"],
    }


def test_un_proyecto_que_no_existe_es_404_con_cuerpo_de_error(cliente: TestClient) -> None:
    respuesta = cliente.get("/api/v1/projects/no-existe")
    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "error": {
            "code": "project_not_found",
            "message": "no existe ningun proyecto con id 'no-existe'",
        }
    }


def test_un_id_sin_forma_de_proyecto_es_404_sin_mirar_la_base(cliente: TestClient) -> None:
    for malo in ["NO", "%2e%2e", "a_b", "-empieza-con-guion", "con%20espacio", "a%5cb"]:
        respuesta = cliente.get(f"/api/v1/projects/{malo}")
        assert respuesta.status_code == 404, malo
        assert respuesta.json()["error"]["code"] == "project_not_found"


def test_una_ruta_que_no_existe_tiene_la_misma_forma_de_error(cliente: TestClient) -> None:
    respuesta = cliente.get("/api/v1/nada")
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["code"] == "not_found"


def test_cors_deja_pasar_al_origen_configurado(base: Base) -> None:
    from cvp.api import crear_app

    ajustes = base.settings(cors_origins=("http://localhost:5173",))
    with TestClient(crear_app(ajustes, clock=base.reloj)) as cliente:
        permitido = cliente.get("/api/v1/projects", headers={"Origin": "http://localhost:5173"})
        assert permitido.headers.get("access-control-allow-origin") == "http://localhost:5173"
        otro = cliente.get("/api/v1/projects", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in otro.headers
