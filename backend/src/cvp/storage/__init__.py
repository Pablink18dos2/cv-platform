"""La persistencia: el esquema, el engine y las consultas.

Es la primera vez que el sistema recuerda algo entre ejecuciones. Lo que producen los
analizadores se guarda crudo (`event`, `metric`) y agregado por minuto (`metric_minute`),
y es lo que serviran la API de #12 y el dashboard de #13.

Este paquete **no sabe nada de fotogramas ni de sinks**: recibe `Event` y `Metric` de
`cvp.core` y los escribe. Quien decide cuando escribir es `cvp.sinks`.
"""

from __future__ import annotations

from cvp.storage.aggregation import (
    HUECO_MAXIMO_S,
    Agregado,
    AgregadorPorMinuto,
    ClaveMinuto,
    ClaveSerie,
    agregar_por_minuto,
    minuto_de,
)
from cvp.storage.engine import URL_POR_DEFECTO, crear_engine, crear_esquema, journal_mode
from cvp.storage.models import (
    EventRow,
    MetricMinute,
    MetricRow,
    Project,
    Run,
    clave_de_labels,
    en_utc,
)
from cvp.storage.queries import (
    acumular_minutos,
    asegurar_proyecto,
    asegurar_run,
    escribir_lote,
    eventos_de,
    metricas_de,
    minutos_de,
)
from cvp.storage.run_id import (
    RUN_ID_RE,
    instante_de_run_id,
    nuevo_run_id,
    validar_run_id,
)

__all__ = [
    "HUECO_MAXIMO_S",
    "RUN_ID_RE",
    "URL_POR_DEFECTO",
    "Agregado",
    "AgregadorPorMinuto",
    "ClaveMinuto",
    "ClaveSerie",
    "EventRow",
    "MetricMinute",
    "MetricRow",
    "Project",
    "Run",
    "acumular_minutos",
    "agregar_por_minuto",
    "asegurar_proyecto",
    "asegurar_run",
    "clave_de_labels",
    "crear_engine",
    "crear_esquema",
    "en_utc",
    "escribir_lote",
    "eventos_de",
    "instante_de_run_id",
    "journal_mode",
    "metricas_de",
    "minuto_de",
    "minutos_de",
    "nuevo_run_id",
    "validar_run_id",
]
