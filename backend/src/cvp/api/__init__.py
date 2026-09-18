"""La API de lectura: proyectos, metricas, eventos y el ultimo fotograma, sobre HTTP.

Es el segundo de los dos procesos de `_docs/architecture.md`. El worker escribe en la base
de datos y en `data/preview/<id>.jpg`; la API **solo lee** de esos dos sitios y no sabe
nada mas del worker: ni lo arranca, ni lo para, ni comparte memoria con el. Por eso
arranca en milisegundos y se podria desplegar en un plan gratuito de cloud dejando el
worker en casa.

Lo que este paquete importa es deliberadamente corto: `cvp.core` (los tipos y la forma
canonica de #19), `cvp.storage` (el esquema y SQLModel), FastAPI y biblioteca estandar.
**Nada de `cvp.sinks`**: su `__init__` importa el `preview`, que importa cv2, y con eso
la API dejaria de ser ligera. El JPEG se sirve leyendo un fichero, y la ruta se deriva del
proyecto con la misma convencion que el sink (`cvp.api.preview`).
`tests/unit/test_api_aislamiento.py` lo comprueba en un subproceso.

El contrato con el frontend es `openapi.yaml`, en la raiz del repo, generado desde
`app.openapi()` con `uv run cvp api --export-openapi`. Un test falla si se desincroniza.
`_docs/api.md` explica la semantica de lo que se sirve, en particular los cuatro bordes de
`metric_minute`.
"""

from __future__ import annotations

from cvp.api.app import crear_app
from cvp.api.settings import Settings

__all__ = ["Settings", "crear_app"]
