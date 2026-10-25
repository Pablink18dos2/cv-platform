"""La telemetria de la API es opcional y no dice nada que no deba.

El arranque real contra un colector (trazas, metricas y logs con el mismo `trace_id`) se
comprueba con `docker compose --profile observability`, no aqui: un test de la suite por
defecto no abre sockets. Lo que si se fija son las dos piezas con logica propia, el log JSON
y la frescura de los datos, y que sin endpoint no se arranca nada.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from api_soporte import INICIO, Base, metrica
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from cvp.api import Settings, crear_app
from cvp.api.telemetria import FormatoJson, _frescura


def _registro(mensaje: str, **extra: object) -> logging.LogRecord:
    registro = logging.makeLogRecord({"msg": mensaje, "levelname": "INFO", "name": "t"})
    registro.__dict__.update(extra)
    return registro


def test_sin_endpoint_no_se_arranca_telemetria(base: Base) -> None:
    app = crear_app(base.settings(), clock=base.reloj)
    assert app.state.apagar_telemetria is None


def test_el_endpoint_sale_del_entorno_y_vacio_cuenta_como_ausente() -> None:
    assert Settings.desde_entorno({}).otlp_endpoint is None
    assert Settings.desde_entorno({"OTEL_EXPORTER_OTLP_ENDPOINT": " "}).otlp_endpoint is None
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://observability:4318"}
    assert Settings.desde_entorno(env).otlp_endpoint == "http://observability:4318"


def test_el_log_json_lleva_el_trace_id_y_el_span_id_del_span_activo() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("peticion") as span:
        linea = json.loads(FormatoJson().format(_registro("GET /x 200", http_status=200)))
    contexto = span.get_span_context()
    assert linea["trace_id"] == format(contexto.trace_id, "032x")
    assert linea["span_id"] == format(contexto.span_id, "016x")
    assert linea["message"] == "GET /x 200"
    assert linea["http_status"] == 200


def test_el_log_json_fuera_de_un_span_no_inventa_ids() -> None:
    linea = json.loads(FormatoJson().format(_registro("arranque")))
    assert "trace_id" not in linea
    assert "span_id" not in linea


def test_la_frescura_es_el_tiempo_desde_el_ultimo_minuto_de_cada_proyecto(
    base: Base, cliente: TestClient
) -> None:
    base.escribir_metricas(metrica(3.0, 5), metrica(4.0, 125))  # minutos 14:20 y 14:22
    base.reloj.ahora = INICIO + timedelta(minutes=2, seconds=30)
    app = cliente.app
    (par,) = _frescura(app, base.reloj)  # type: ignore[arg-type]
    proyecto, segundos = par
    assert proyecto == "01-parking-trafico"
    assert segundos == 30.0  # ahora (14:22:30) - inicio del ultimo minuto (14:22:00)


def test_sin_datos_no_hay_gauge(base: Base, cliente: TestClient) -> None:
    assert _frescura(cliente.app, base.reloj) == []  # type: ignore[arg-type]
