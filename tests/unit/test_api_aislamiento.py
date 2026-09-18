"""La API no importa nada pesado.

Es lo que la hace desplegable en un plan gratuito de cloud dejando el worker en casa, y la
razon de que sea un proceso aparte (`_docs/architecture.md`): arranca en milisegundos
porque no carga ni torch, ni OpenCV, ni ultralytics, ni yt-dlp.

Tiene que ser un **subproceso**, por lo mismo que `test_core_aislamiento.py`: en este
interprete cualquier otro test podria haber importado torch antes. Y mira `sys.modules`,
no el texto de los ficheros, para cazar tambien las importaciones transitivas: el
`cvp.sinks.preview` que escribe el JPEG importa cv2, y basta un `from cvp.sinks import ...`
en la API para arrastrarlo.

Se construye la app entera, no solo se importa el paquete: un import pesado dentro de
`crear_app` o de un router se paga igual al arrancar.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Lo que no puede acabar cargado por arrancar la API. Es la lista del criterio de #12 mas
# `trackers`, que entra por `cvp.trackers` y tarda lo suyo.
MODULOS_PROHIBIDOS = (
    "torch",
    "torchvision",
    "cv2",
    "ultralytics",
    "yt_dlp",
    "supervision",
    "trackers",
    "PIL",
)

# Los bloques del worker. La API lee de `cvp.storage` y no necesita ninguno; si alguno
# aparece es que un import ha cruzado la frontera entre los dos procesos.
PAQUETES_DEL_WORKER = (
    "cvp.sources",
    "cvp.detectors",
    "cvp.trackers",
    "cvp.analyzers",
    "cvp.sinks",
    "cvp.pipeline.runner",
    "cvp.pipeline.factory",
)


def _programa(base: Path) -> str:
    """Una sola linea para `python -c`: un argumento con saltos de linea no sobrevive igual
    de bien al paso por la linea de comandos de Windows."""
    url = f"sqlite:///{base.as_posix()}"
    return (
        "import json, sys; "
        "import cvp.api; "
        f"app = cvp.api.crear_app(cvp.api.Settings(database_url={url!r})); "
        "app.openapi(); "
        f"prohibidos = set({list(MODULOS_PROHIBIDOS)!r}); "
        f"worker = {list(PAQUETES_DEL_WORKER)!r}; "
        "raices = {m.split('.')[0] for m in sys.modules}; "
        "cruzados = sorted(m for m in sys.modules "
        "if any(m == w or m.startswith(w + '.') for w in worker)); "
        "print(json.dumps({'pesados': sorted(raices & prohibidos), 'worker': cruzados}))"
    )


def _modulos_cargados_al_arrancar_la_api(tmp_path: Path) -> dict[str, list[str]]:
    resultado = subprocess.run(
        [sys.executable, "-c", _programa(tmp_path / "cvp.sqlite3")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, f"el subproceso fallo:\n{resultado.stderr}"
    return json.loads(resultado.stdout)


def test_arrancar_la_api_no_carga_ninguna_dependencia_pesada(tmp_path: Path) -> None:
    cargados = _modulos_cargados_al_arrancar_la_api(tmp_path)["pesados"]
    assert cargados == [], (
        f"arrancar la API ha cargado {', '.join(cargados)}. "
        "La API solo lee de la base de datos y de un fichero JPEG: no puede importar "
        "torch, cv2, ultralytics, yt-dlp, supervision ni trackers, ni nada que los importe "
        "(`cvp.sinks.preview` importa cv2)"
    )


def test_arrancar_la_api_no_arrastra_los_bloques_del_worker(tmp_path: Path) -> None:
    cargados = _modulos_cargados_al_arrancar_la_api(tmp_path)["worker"]
    assert cargados == [], (
        f"arrancar la API ha cargado {', '.join(cargados)}. "
        "Worker y API son dos procesos: la API lee `cvp.storage` y no necesita ningun "
        "bloque del pipeline"
    )
