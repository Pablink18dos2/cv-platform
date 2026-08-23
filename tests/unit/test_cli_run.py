"""`cvp run`: el contrato del comando, sin abrir un directo ni cargar un modelo.

Lo que se prueba aqui es lo que ve quien lo llama desde un script o desde systemd: que
acepta lo mismo que `validate`, que un YAML mal escrito suena exactamente igual en los dos
comandos, que los limites se parsean y que `--log-level` respeta el entorno. El bucle en si
se prueba con dobles en `tests/component/test_pipeline_runner.py`, y contra el directo de
verdad en la ejecucion marcada `network`.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from cvp.cli import NIVEL_POR_DEFECTO, build_parser, main, nivel_por_defecto

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
ROTO = FIXTURES / "roto-tres-errores.yaml"
VIDEO = Path(__file__).resolve().parents[1] / "fixtures" / "video" / "calle-10s.mp4"

MINIMO: dict[str, Any] = {
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
                }
            ],
        }
    ],
    "sinks": [{"type": "sqlite"}],
}


def proyecto(tmp_path: Path, **cambios: Any) -> Path:
    """Escribe un directorio de proyecto con su `project.yaml` dentro."""
    directorio = tmp_path / "mi-proyecto"
    directorio.mkdir()
    documento = copy.deepcopy(MINIMO) | cambios
    (directorio / "project.yaml").write_text(
        yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8"
    )
    return directorio


# --- El YAML invalido suena igual que en `validate` -----------------------------------


def test_un_yaml_invalido_falla_exactamente_igual_que_en_validate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate", str(ROTO)]) == 1
    de_validate = capsys.readouterr()

    assert main(["run", str(ROTO)]) == 1
    de_run = capsys.readouterr()

    assert de_run.err == de_validate.err
    assert de_run.out == ""
    assert "Traceback" not in de_run.err
    assert de_run.err.startswith(f"{ROTO}: 3 errores de configuración")


def test_una_ruta_que_no_existe_no_revienta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(tmp_path / "no-existe")]) == 1
    salida = capsys.readouterr()
    assert "Traceback" not in salida.err
    assert "no existe" in salida.err


# --- Los limites y la salud -----------------------------------------------------------


def test_run_acepta_los_dos_limites_y_la_cadencia_de_salud() -> None:
    args = build_parser({}).parse_args(
        ["run", "proyectos/01", "--max-frames", "30", "--duration", "180", "--health-every", "5"]
    )
    assert args.comando == "run"
    assert args.path == "proyectos/01"
    assert args.max_frames == 30
    assert args.duration_s == 180.0
    assert args.health_every_s == 5.0


def test_sin_limites_no_hay_limites() -> None:
    """Un proceso cuya razon de ser es no terminar nunca tiene que poder no terminar."""
    args = build_parser({}).parse_args(["run", "proyectos/01"])
    assert args.max_frames is None
    assert args.duration_s is None
    assert args.health_every_s == 30.0


# --- `--log-level` y `CVP_LOG_LEVEL` --------------------------------------------------


def test_el_nivel_por_defecto_sale_de_la_variable_de_entorno() -> None:
    def nivel(valor: str) -> str:
        return build_parser({"CVP_LOG_LEVEL": valor}).parse_args(["run", "x"]).log_level

    assert nivel("DEBUG") == "DEBUG"
    assert nivel("warning") == "WARNING"  # se normaliza a mayusculas


def test_sin_variable_de_entorno_el_nivel_es_info() -> None:
    assert build_parser({}).parse_args(["run", "x"]).log_level == NIVEL_POR_DEFECTO


def test_la_bandera_pisa_a_la_variable_de_entorno() -> None:
    args = build_parser({"CVP_LOG_LEVEL": "DEBUG"}).parse_args(["run", "x", "--log-level", "ERROR"])
    assert args.log_level == "ERROR"


def test_un_nivel_de_entorno_que_no_existe_avisa_y_sigue_con_info(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """El worker corre desatendido: una errata en una variable de entorno no es motivo
    para no medir nada esta noche."""
    assert nivel_por_defecto({"CVP_LOG_LEVEL": "verboso"}) == NIVEL_POR_DEFECTO
    assert "no es un nivel de log" in capsys.readouterr().err


# --- El directorio del proyecto y el `type:` sin implementacion -----------------------


@pytest.mark.slow
def test_run_acepta_el_directorio_y_rechaza_un_type_sin_implementacion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Importa los paquetes de implementacion de verdad, y por eso va marcado `slow`.

    No carga ningun modelo ni abre ninguna fuente: el runner resuelve **los cinco nombres
    antes de construir nada**, y `traffic_flow` todavia no tiene implementacion (#10). Es
    el hueco entre `CONFIG_SCHEMAS` y los registros, visto desde el CLI.
    """
    directorio = proyecto(tmp_path, source={"type": "file", "path": str(VIDEO), "target_fps": 5})
    assert main(["run", str(directorio), "--max-frames", "1"]) == 1
    salida = capsys.readouterr()
    assert "analyzers[0].type" in salida.err
    assert "no hay ningun analyzer llamado 'traffic_flow'" in salida.err
    assert "Traceback" not in salida.err
