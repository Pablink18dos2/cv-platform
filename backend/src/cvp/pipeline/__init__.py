"""El pipeline: la configuracion de un proyecto y, mas adelante, el runner que la ejecuta.

`cvp.pipeline` importa `cvp.core`, Pydantic, PyYAML y biblioteca estandar. Nada mas: ni
`cvp.sources`, ni `cvp.detectors`, ni `cvp.trackers`, ni `cvp.analyzers`, ni `cvp.sinks`.
Es lo que hace que `cvp validate` responda en milisegundos sin cargar ningun modelo.
"""

from __future__ import annotations

from cvp.pipeline.config import CONFIG_SCHEMAS, Point, ProjectConfig, Region, load_project_config
from cvp.pipeline.errors import ConfigError

__all__ = [
    "CONFIG_SCHEMAS",
    "ConfigError",
    "Point",
    "ProjectConfig",
    "Region",
    "load_project_config",
]
