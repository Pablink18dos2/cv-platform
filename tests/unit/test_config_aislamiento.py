"""Validar la configuracion no puede costar lo que tarda ultralytics en importarse.

Es lo que hace que "el segundo 0" sea literal: `cvp validate` responde en milisegundos y
no necesita el stack de vision entero instalado. Por eso el `type:` se resuelve contra los
esquemas de `cvp.pipeline.config` y no contra los registros de `cvp.core`, que habria que
rellenar importando `cvp.sources`, `cvp.detectors`, ...

Tiene que ser un **subproceso**, igual que `test_core_aislamiento.py`: en este mismo
interprete cualquier otro test podria haber importado torch antes, y entonces la
comprobacion no valdria nada.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

VALIDO = Path(__file__).resolve().parents[1] / "fixtures" / "config" / "valido.yaml"

# Lo que no puede acabar cargado por validar un project.yaml. `yaml` y `pydantic` si
# pueden: son las dos dependencias que este modulo importa a proposito.
MODULOS_PROHIBIDOS = (
    "torch",
    "torchvision",
    "cv2",
    "ultralytics",
    "yt_dlp",
    "supervision",
    "sqlmodel",
    "sqlalchemy",
    "fastapi",
    "PIL",
)

PROGRAMA = (
    "import json, sys; "
    "from cvp.pipeline.config import load_project_config; "
    "config = load_project_config(sys.argv[1]); "
    f"prohibidos = set({list(MODULOS_PROHIBIDOS)!r}); "
    "raices = {m.split('.')[0] for m in sys.modules}; "
    "print(json.dumps({'id': config.id, 'pesados': sorted(raices & prohibidos), "
    "'ligeros': sorted(raices & {'yaml', 'pydantic'})}))"
)


def _modulos_cargados_al_validar() -> dict[str, object]:
    resultado = subprocess.run(
        [sys.executable, "-c", PROGRAMA, str(VALIDO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, f"el subproceso fallo:\n{resultado.stderr}"
    return json.loads(resultado.stdout)


def test_cargar_una_configuracion_no_importa_ninguna_dependencia_pesada() -> None:
    cargados = _modulos_cargados_al_validar()
    assert cargados["id"] == "valido"
    assert cargados["pesados"] == [], (
        f"validar ha cargado {', '.join(cargados['pesados'])}. "  # type: ignore[arg-type]
        "cvp.pipeline.config solo puede importar cvp.core, pydantic, yaml y la estandar"
    )


def test_la_comprobacion_anterior_mira_de_verdad_los_modulos_cargados() -> None:
    """Un test que pasara aunque el codigo no funcione no comprueba nada: si la sonda ve
    `yaml` y `pydantic`, es que esta mirando `sys.modules` del proceso correcto."""
    assert _modulos_cargados_al_validar()["ligeros"] == ["pydantic", "yaml"]
