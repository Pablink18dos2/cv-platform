"""El CLI: `cvp validate` y su codigo de salida.

Lo que se comprueba aqui es el contrato con quien lo llama desde un script o desde CI:
0 y un resumen en stdout, o 1 y el bloque de errores en stderr. Sin traceback: un
`ConfigError` es un error de quien escribio el YAML, no un fallo del programa.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from cvp.cli import build_parser, main

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

MINIMO = {
    "id": "mi-proyecto",
    "name": "Proyecto mínimo",
    "source": {"type": "webcam", "target_fps": 5},
    "detector": {"type": "yolo", "classes": ["car"]},
    "tracker": {"type": "bytetrack"},
    "analyzers": [
        {
            "type": "traffic_flow",
            "lines": [
                {
                    "id": "principal",
                    "p1": [0.1, 0.2],
                    "p2": [0.8, 0.7],
                    "positive_direction": "norte",
                    "negative_direction": "sur",
                }
            ],
        }
    ],
    "sinks": [{"type": "sqlite"}],
}


def test_la_version_sigue_funcionando_igual() -> None:
    with pytest.raises(SystemExit) as salida:
        build_parser().parse_args(["--version"])
    assert salida.value.code == 0


def test_validate_con_una_configuracion_valida_resume_y_sale_con_cero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate", str(FIXTURES / "valido.yaml")]) == 0
    salida = capsys.readouterr()
    assert salida.err == ""
    assert "valido" in salida.out
    assert "source youtube_live" in salida.out
    assert "detector yolo" in salida.out
    assert "2 analizadores" in salida.out
    assert "3 sinks" in salida.out


def test_validate_acepta_el_directorio_de_un_proyecto(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proyecto = tmp_path / "mi-proyecto"
    proyecto.mkdir()
    (proyecto / "project.yaml").write_text(
        yaml.safe_dump(copy.deepcopy(MINIMO), allow_unicode=True), encoding="utf-8"
    )
    assert main(["validate", str(proyecto)]) == 0
    assert "mi-proyecto" in capsys.readouterr().out


def test_validate_con_una_configuracion_invalida_sale_con_uno_y_escribe_en_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ruta = str(FIXTURES / "roto-tres-errores.yaml")
    assert main(["validate", ruta]) == 1
    salida = capsys.readouterr()
    assert salida.out == ""
    assert salida.err.startswith(f"{ruta}: 3 errores de configuración")
    assert "  - detector.conf: debe estar entre 0 y 1, y vale 1.4" in salida.err
    assert "Traceback" not in salida.err


def test_validate_de_un_fichero_que_no_existe_no_revienta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(tmp_path / "no-existe.yaml")]) == 1
    assert "Traceback" not in capsys.readouterr().err
