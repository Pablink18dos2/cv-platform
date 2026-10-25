"""Construir la app: la base, el reloj, CORS, los manejadores de error y los routers.

`crear_app` es una fabrica y no un `app = FastAPI()` a nivel de modulo a proposito: los
tests construyen una app por base de datos temporal y con un reloj falso, y el CLI otra
con lo que diga el entorno. El engine vive en `app.state.engine` y el reloj en
`app.state.clock`; las rutas los cogen de ahi a traves de `cvp.api.db.sesion` y
`cvp.api.reloj.ahora`.

**El reloj se inyecta.** El `status` de un proyecto (`running` / `stopped` / `stale`) y la
ventana por defecto de las metricas dependen de "ahora", y `datetime.now()` solo aparece
aqui, como valor por defecto de `clock`. Es la regla del proyecto entero: los tests fijan
la hora y comprueban los tres estados sin esperar 90 segundos.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cvp import __version__
from cvp.api.db import abrir_engine
from cvp.api.errors import registrar_manejadores
from cvp.api.routers import events, metrics, projects, stream
from cvp.api.settings import Settings
from cvp.api.telemetria import configurar as configurar_telemetria

__all__ = ["BASE_PATH", "crear_app"]

BASE_PATH = "/api/v1"

DESCRIPCION = """\
API de lectura de CV Platform. Sirve lo que el worker escribe: los proyectos, sus
metricas agregadas por minuto, sus eventos y el ultimo fotograma anotado.

Es de solo lectura y no sabe nada del worker: no lo arranca ni lo para. La semantica de
las metricas —que columna usar para que, y que significan un hueco y un `null`— esta en
`_docs/api.md`, que hay que leer antes de pintar una grafica.
"""


def crear_app(
    settings: Settings | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """La app, lista para `uvicorn` o para un `TestClient`.

    Sin `settings` se leen del entorno. Sin `clock`, la hora es la del sistema en UTC; es
    el unico `datetime.now()` de la API y esta aqui como valor por defecto inyectable.
    """
    ajustes = settings if settings is not None else Settings.desde_entorno()
    reloj = clock if clock is not None else _ahora_del_sistema

    @asynccontextmanager
    async def _vida(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if app.state.apagar_telemetria is not None:
                app.state.apagar_telemetria()
            app.state.engine.dispose()

    app = FastAPI(
        title="CV Platform API",
        version=__version__,
        description=DESCRIPCION,
        lifespan=_vida,
        openapi_tags=[
            {"name": "projects", "description": "Los proyectos y su estado."},
            {"name": "metrics", "description": "Series por minuto y ultimos valores."},
            {"name": "events", "description": "Eventos en la forma canonica, paginados."},
            {"name": "stream", "description": "El ultimo fotograma, como JPEG o como MJPEG."},
        ],
    )
    app.state.settings = ajustes
    app.state.clock = reloj
    app.state.engine = abrir_engine(ajustes.database_url)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ajustes.cors_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    registrar_manejadores(app)
    # Sin OTEL_EXPORTER_OTLP_ENDPOINT no hace nada (ni importa OpenTelemetry).
    app.state.apagar_telemetria = configurar_telemetria(app, ajustes.otlp_endpoint)
    app.include_router(projects.router, prefix=BASE_PATH)
    app.include_router(metrics.router, prefix=BASE_PATH)
    app.include_router(events.router, prefix=BASE_PATH)
    app.include_router(stream.router, prefix=BASE_PATH)
    return app


def _ahora_del_sistema() -> datetime:
    return datetime.now(UTC)
