"""El pipeline entero contra el directo de verdad. **Fuera de la suite por defecto.**

Es el unico test que sale a internet y el unico que carga el modelo de verdad, asi que va
marcado `network` y `slow`: no corre ni en `uv run pytest` ni en CI. Se lanza a mano, y es
la version pequena de la medicion de `_docs/rendimiento.md`:

    uv run pytest tests/integration -m network

Lo que comprueba no son los fps —eso es una medida, no un criterio— sino que las piezas de
#4 a #7 encajan de verdad cuando la fuente es el directo: que se resuelve la URL, que el ROI
se traduce con la resolucion real, que el tracker recibe el `target_fps` del proyecto, que
`--max-frames` para y que el proceso sale con 0 sin dejar ningun hilo lector vivo.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cvp.cli import main

PROYECTO = Path(__file__).resolve().parents[2] / "projects" / "01-parking-trafico"


@pytest.mark.network
@pytest.mark.slow
def test_el_pipeline_corre_contra_el_directo_y_sale_limpio(
    capsys: pytest.CaptureFixture[str],
) -> None:
    vivos_antes = {h.name for h in threading.enumerate()}

    assert main(["run", str(PROYECTO), "--max-frames", "5", "--health-every", "5"]) == 0

    salida = capsys.readouterr().out
    assert "fps de salida" in salida
    assert "detector:" in salida
    assert "tracker:" in salida
    # El connector en directo levanta un hilo lector; si el `finally` del runner no cerrara
    # la fuente, aqui quedaria vivo consumiendo ancho de banda para nada.
    assert {h.name for h in threading.enumerate()} - vivos_antes == set()
