"""`cvp.core` no arrastra nada pesado ni depende del resto del paquete.

Es lo que mantiene la suite en milisegundos y lo que permite que la API, que solo lee de
la base de datos, arranque sin cargar un modelo.

Tiene que ser un **subproceso**: en este mismo interprete cualquier otro test podria
haber importado torch antes, y entonces la comprobacion no valdria nada. Y mira
`sys.modules`, no el texto de los ficheros, para cazar tambien las importaciones
transitivas (un `import` en un modulo que el core importa).
"""

from __future__ import annotations

import json
import subprocess
import sys

# Lo que no puede acabar cargado por importar `cvp.core`. Torch y ultralytics tardan
# segundos en importarse; cv2, PIL y supervision arrastran binarios; pydantic, sqlmodel,
# sqlalchemy y fastapi son de los bloques de configuracion, almacenamiento y API, que no
# tienen nada que hacer en el contrato.
MODULOS_PROHIBIDOS = (
    "torch",
    "torchvision",
    "cv2",
    "ultralytics",
    "yt_dlp",
    "supervision",
    "pydantic",
    "sqlmodel",
    "sqlalchemy",
    "fastapi",
    "PIL",
)

# Se pasa a `python -c` como una sola linea: un argumento con saltos de linea no
# sobrevive igual de bien al paso por la linea de comandos de Windows.
PROGRAMA = (
    "import json, sys; "
    "import cvp.core; "
    f"prohibidos = set({list(MODULOS_PROHIBIDOS)!r}); "
    "raices = {m.split('.')[0] for m in sys.modules}; "
    "otros_cvp = sorted(m for m in sys.modules "
    "if m.startswith('cvp') and m != 'cvp' and m != 'cvp.core' "
    "and not m.startswith('cvp.core.')); "
    "print(json.dumps({'pesados': sorted(raices & prohibidos), 'cvp': otros_cvp}))"
)


def _modulos_cargados_al_importar_el_core() -> dict[str, list[str]]:
    resultado = subprocess.run(
        [sys.executable, "-c", PROGRAMA],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, f"el subproceso fallo:\n{resultado.stderr}"
    return json.loads(resultado.stdout)


def test_importar_el_core_no_carga_ninguna_dependencia_pesada() -> None:
    cargados = _modulos_cargados_al_importar_el_core()["pesados"]
    assert cargados == [], (
        f"importar cvp.core ha cargado {', '.join(cargados)}. "
        "El core solo puede importar biblioteca estandar y numpy"
    )


def test_importar_el_core_no_arrastra_al_resto_del_paquete() -> None:
    cargados = _modulos_cargados_al_importar_el_core()["cvp"]
    assert cargados == [], (
        f"importar cvp.core ha cargado {', '.join(cargados)}. "
        "core/ no importa nada de los demas bloques; los demas importan core/"
    )
