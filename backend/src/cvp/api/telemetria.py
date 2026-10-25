"""Observabilidad opcional de la API: trazas, metricas y logs JSON con el mismo `trace_id`.

**Opcional por entorno.** Solo se arranca si hay `OTEL_EXPORTER_OTLP_ENDPOINT`
(`Settings.otlp_endpoint`). Sin el, `configurar` no hace nada y **no importa
OpenTelemetry**: la imagen ligera de Render y los tests siguen como estaban, y el test de
aislamiento sigue viendo una API sin dependencias extra cargadas.

Que se emite, todo por OTLP/HTTP al colector (`grafana/otel-lgtm` en `compose.yaml`):

- **Trazas**: una por peticion (instrumentacion de FastAPI) con un span hijo por consulta SQL
  (instrumentacion de SQLAlchemy).
- **Metricas**: contador y latencia por ruta de la instrumentacion de FastAPI, mas el gauge
  propio `cvp_data_freshness_seconds{project}`: segundos desde el ultimo minuto con datos de
  `metric_minute` de cada proyecto, **calculado al exportar** con el reloj de la app. Es lo
  que ve el usuario: si el worker se para, el dashboard se queda con datos viejos.
- **Logs**: una linea JSON por peticion, a stdout y por OTLP, con `trace_id` y `span_id` del
  span activo (el mismo ID que la traza).

**Sin datos de cliente.** No hay usuarios, y aun asi no se registran cabeceras, cookies ni
query strings: la linea de log lleva metodo, plantilla de ruta (`/projects/{id}/...`),
estado y duracion; la instrumentacion de FastAPI no captura cabeceras salvo que se pida con
`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`, y aqui no se pide.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import func
from sqlmodel import Session, col, select

from cvp.storage import MetricMinute, en_utc

__all__ = ["FormatoJson", "configurar"]

logger = logging.getLogger("cvp.api.request")

# Atributos que `logging` pone en todo registro: lo que no esta aqui es un `extra=` nuestro.
_ESTANDAR = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}

# Atributos de span que la instrumentacion estandar anade y que no se exportan: quien pide.
_ATRIBUTOS_PERSONALES = (
    "client.address",
    "client.port",
    "user_agent.original",
    "url.query",
    "http.url",
    "http.target",
    "http.client_ip",
    "http.user_agent",
    "net.peer.ip",
    "net.peer.port",
)


class FormatoJson(logging.Formatter):
    """Una linea JSON por registro, con `trace_id` y `span_id` si hay un span activo."""

    def format(self, record: logging.LogRecord) -> str:
        from opentelemetry import trace

        linea: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        contexto = trace.get_current_span().get_span_context()
        if contexto.is_valid:
            linea["trace_id"] = format(contexto.trace_id, "032x")
            linea["span_id"] = format(contexto.span_id, "016x")
        linea.update({k: v for k, v in record.__dict__.items() if k not in _ESTANDAR})
        if record.exc_info:
            linea["exc"] = self.formatException(record.exc_info)
        return json.dumps(linea, default=str)


def _frescura(app: FastAPI, ahora: Callable[[], datetime]) -> list[tuple[str, float]]:
    """`(proyecto, segundos desde su ultimo minuto con muestras)`; vacio si la base falla."""
    from opentelemetry.instrumentation.utils import suppress_instrumentation

    try:
        with suppress_instrumentation(), Session(app.state.engine) as session:
            filas = session.exec(
                select(MetricMinute.project_id, func.max(col(MetricMinute.minute))).group_by(
                    MetricMinute.project_id
                )
            ).all()
    except Exception:
        logger.exception("no se pudo calcular cvp_data_freshness_seconds")
        return []
    marca = ahora()
    return [(p, max(0.0, (marca - en_utc(m)).total_seconds())) for p, m in filas if m]


def configurar(app: FastAPI, endpoint: str | None) -> Callable[[], None] | None:
    """Activa la telemetria si hay `endpoint`; devuelve la funcion que la vacia al apagar."""
    if not endpoint:
        return None

    # Convencion estable de HTTP: `http.server.request.duration` (s) con `http.route`.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # El endpoint es la base: cada exportador le anade /v1/traces, /v1/metrics, /v1/logs.
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    recurso = Resource.create({"service.name": "cvp-api"})

    def depurar(span: Any, _scope: Any) -> None:
        """Quita del span del servidor lo que identifica a quien pide: IP, navegador, query."""
        for clave in _ATRIBUTOS_PERSONALES:
            span._attributes.pop(clave, None)  # noqa: SLF001 - la API publica no permite borrar

    trazas = TracerProvider(resource=recurso)
    trazas.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    metricas = MeterProvider(
        resource=recurso, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
    )
    metricas.get_meter("cvp.api").create_observable_gauge(
        "cvp_data_freshness_seconds",
        callbacks=[
            lambda _opciones: [
                Observation(segundos, {"project": proyecto})
                for proyecto, segundos in _frescura(app, app.state.clock)
            ]
        ],
        unit="s",
        description="Segundos desde el ultimo minuto con muestras de cada proyecto.",
    )

    logs = LoggerProvider(resource=recurso)
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    raiz = logging.getLogger()
    # El JSON a stdout sustituye al texto plano de `basicConfig`.
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
    salida = logging.StreamHandler()
    salida.setFormatter(FormatoJson())
    exportador_de_logs = LoggingHandler(logger_provider=logs)
    raiz.addHandler(salida)
    raiz.addHandler(exportador_de_logs)
    raiz.setLevel(logging.INFO)

    @app.middleware("http")
    async def _log_de_peticion(request: Request, call_next: Callable[..., Any]) -> Any:
        inicio = time.perf_counter()
        respuesta = await call_next(request)
        # La plantilla completa (`/api/v1/projects/{id}/...`) la calcula el span del servidor;
        # `scope["route"]` solo trae la ruta relativa al router.
        span = trace.get_current_span()
        ruta = (getattr(span, "attributes", None) or {}).get("http.route", "no_route")
        logger.info(
            "%s %s %s",
            request.method,
            ruta,
            respuesta.status_code,
            extra={
                "http_method": request.method,
                "http_route": ruta,
                "http_status": respuesta.status_code,
                "duration_ms": round((time.perf_counter() - inicio) * 1000, 2),
            },
        )
        return respuesta

    # Despues de anadir el middleware de log: la instrumentacion tiene que quedar fuera
    # para que el span ya este activo cuando se escribe la linea.
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=trazas, meter_provider=metricas, server_request_hook=depurar
    )
    SQLAlchemyInstrumentor().instrument(engine=app.state.engine, tracer_provider=trazas)

    def apagar() -> None:
        FastAPIInstrumentor.uninstrument_app(app)
        raiz.removeHandler(exportador_de_logs)
        for proveedor in (trazas, metricas, logs):
            proveedor.shutdown()

    return apagar
