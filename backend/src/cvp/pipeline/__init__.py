"""El pipeline: la configuracion de un proyecto y el runner que la ejecuta.

**Este `__init__` importa solo lo ligero**: `cvp.core`, Pydantic, PyYAML y biblioteca
estandar. Ni `cvp.sources`, ni `cvp.detectors`, ni `cvp.trackers`, ni `cvp.analyzers`, ni
`cvp.sinks`. Es lo que hace que `cvp validate` responda en milisegundos sin cargar ningun
modelo, y `tests/unit/test_config_aislamiento.py` lo comprueba en un subproceso.

Por eso el runner **no se reexporta aqui**: `cvp.pipeline.runner` y `cvp.pipeline.factory`
se importan por su nombre completo, y quien lo hace ya sabe que va a pagar OpenCV y
ultralytics. Lo unico suyo que vive en este `__init__` es la excepcion, porque escribir un
`except` no puede costar un import pesado.
"""

from __future__ import annotations

from cvp.pipeline.config import CONFIG_SCHEMAS, Point, ProjectConfig, Region, load_project_config
from cvp.pipeline.errors import AbortRequested, ConfigError, PipelineError

__all__ = [
    "CONFIG_SCHEMAS",
    "AbortRequested",
    "ConfigError",
    "PipelineError",
    "Point",
    "ProjectConfig",
    "Region",
    "load_project_config",
]
