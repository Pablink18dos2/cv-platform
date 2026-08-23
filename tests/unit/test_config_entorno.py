"""La expansion de variables de entorno, regla por regla.

Nada de `monkeypatch.setenv` salvo en el unico test que comprueba que `env=None` cae en
`os.environ`: el resto inyecta el mapping, igual que los timestamps se inyectan en vez de
llamar a `datetime.now()`.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from cvp.pipeline import ConfigError, ProjectConfig, load_project_config

MINIMO: dict[str, Any] = {
    "id": "minimo",
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


def escribir(tmp_path: Path, **cambios: object) -> Path:
    documento = copy.deepcopy(MINIMO) | cambios
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8")
    return fichero


def carga(tmp_path: Path, entorno: dict[str, str], **cambios: object) -> ProjectConfig:
    return load_project_config(escribir(tmp_path, **cambios), env=entorno)


def problemas(tmp_path: Path, entorno: dict[str, str], **cambios: object) -> tuple[str, ...]:
    with pytest.raises(ConfigError) as error:
        carga(tmp_path, entorno, **cambios)
    return error.value.problems


def rtsp(url: str) -> dict[str, object]:
    return {"type": "rtsp", "url": url, "target_fps": 2}


# --- Sintaxis ---------------------------------------------------------------------


def test_se_expande_la_forma_con_llaves(tmp_path: Path) -> None:
    config = carga(tmp_path, {"RTSP": "rtsp://camara/1"}, source=rtsp("${RTSP}"))
    assert config.source.url == "rtsp://camara/1"


def test_una_variable_sin_llaves_no_se_expande(tmp_path: Path) -> None:
    """Un `$` suelto es demasiado comun en contrasenas y en URLs."""
    config = carga(tmp_path, {"PASS": "secreta"}, source=rtsp("rtsp://user:$PASS@camara/1"))
    assert config.source.url == "rtsp://user:$PASS@camara/1"


def test_el_doble_dolar_produce_el_literal(tmp_path: Path) -> None:
    config = carga(tmp_path, {"NOMBRE": "expandido"}, name="$${NOMBRE}")
    assert config.name == "${NOMBRE}"


def test_se_expande_dentro_de_la_cadena_y_varias_veces(tmp_path: Path) -> None:
    entorno = {"USUARIO": "admin", "PASS": "s3creta", "IP": "192.168.1.10"}
    config = carga(tmp_path, entorno, source=rtsp("rtsp://${USUARIO}:${PASS}@${IP}/stream"))
    assert config.source.url == "rtsp://admin:s3creta@192.168.1.10/stream"


def test_se_expande_a_cualquier_profundidad_listas_incluidas(tmp_path: Path) -> None:
    entorno = {"CLASE": "truck", "SENTIDO": "sur"}
    lineas = [MINIMO["analyzers"][0]["lines"][0] | {"positive_direction": "${SENTIDO}"}]
    config = carga(
        tmp_path,
        entorno,
        detector={"type": "yolo", "classes": ["car", "${CLASE}"]},
        analyzers=[{"type": "traffic_flow", "lines": lineas}],
    )
    assert config.detector.classes == ["car", "truck"]
    assert config.analyzers[0].lines[0].positive_direction == "sur"


def test_en_las_claves_no_se_expande(tmp_path: Path) -> None:
    fichero = tmp_path / "project.yaml"
    documento = yaml.safe_dump(MINIMO, allow_unicode=True) + "${CAMPO}: 3\n"
    fichero.write_text(documento, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_project_config(fichero, env={"CAMPO": "name"})
    assert error.value.problems[0].startswith("${CAMPO}: campo desconocido")


def test_un_llave_sin_cerrar_es_un_error_no_un_literal(tmp_path: Path) -> None:
    (problema,) = problemas(tmp_path, {}, source=rtsp("rtsp://${IP/stream"))
    assert problema.startswith("source.url: hay un '${' sin cerrar")


# --- Variables ausentes y vacias --------------------------------------------------


def test_una_variable_que_no_existe_es_un_error_de_carga(tmp_path: Path) -> None:
    """Nunca se sustituye por vacio: una URL vacia falla lejos de su causa."""
    assert problemas(tmp_path, {}, source={"type": "youtube_live", "url": "${YT_URL}"}) == (
        "source.url: la variable de entorno YT_URL no está definida",
    )


def test_una_variable_ausente_con_valor_por_defecto_usa_el_defecto(tmp_path: Path) -> None:
    config = carga(tmp_path, {}, source=rtsp("${RTSP_URL:-rtsp://camara/por-defecto}"))
    assert config.source.url == "rtsp://camara/por-defecto"


def test_una_variable_vacia_se_trata_igual_que_ausente(tmp_path: Path) -> None:
    config = carga(tmp_path, {"RTSP_URL": ""}, source=rtsp("${RTSP_URL:-rtsp://camara/1}"))
    assert config.source.url == "rtsp://camara/1"
    (problema,) = problemas(tmp_path, {"RTSP_URL": ""}, source=rtsp("${RTSP_URL}"))
    assert problema.startswith("source.url: la variable de entorno RTSP_URL está definida pero")


def test_el_error_nombra_la_variable_y_el_campo(tmp_path: Path) -> None:
    problemas_ = problemas(
        tmp_path,
        {},
        source={"type": "youtube_live", "url": "${YT_URL}", "target_fps": "${FPS}"},
    )
    assert problemas_ == (
        "source.target_fps: la variable de entorno FPS no está definida",
        "source.url: la variable de entorno YT_URL no está definida",
    )


# --- Que se expande y que no ------------------------------------------------------


def test_la_expansion_ocurre_sobre_los_valores_parseados_no_sobre_el_texto_crudo(
    tmp_path: Path,
) -> None:
    """Preprocesar el texto dejaria que una variable inyecte sintaxis YAML."""
    inyeccion = "esto: es un mapa\ny otra linea"
    config = carga(tmp_path, {"INYECCION": inyeccion}, name="${INYECCION}")
    assert config.name == inyeccion
    assert config.id == "minimo"
    assert config.source.type == "webcam"


def test_el_resultado_de_una_expansion_es_una_cadena_y_el_tipo_lo_pone_pydantic(
    tmp_path: Path,
) -> None:
    config = carga(
        tmp_path,
        {"CVP_TARGET_FPS": "5"},
        source={"type": "webcam", "target_fps": "${CVP_TARGET_FPS}"},
    )
    assert config.source.target_fps == 5.0

    assert problemas(
        tmp_path,
        {"CVP_TARGET_FPS": "rapido"},
        source={"type": "webcam", "target_fps": "${CVP_TARGET_FPS}"},
    ) == ("source.target_fps: debe ser un número, y vale 'rapido'",)


# --- De donde salen las variables -------------------------------------------------


def test_sin_env_se_usa_os_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CVP_TEST_RTSP_URL", "rtsp://desde-el-entorno/1")
    fichero = escribir(tmp_path, source=rtsp("${CVP_TEST_RTSP_URL}"))
    assert load_project_config(fichero).source.url == "rtsp://desde-el-entorno/1"


def test_no_se_lee_ningun_env_ni_se_toca_el_entorno_del_proceso(tmp_path: Path) -> None:
    """Cargar el `.env` en el arranque del CLI es otra tarea, y es una decision del CLI."""
    (tmp_path / ".env").write_text("YT_URL=https://youtu.be/desde-el-env\n", encoding="utf-8")
    antes = dict(os.environ)
    assert problemas(tmp_path, {}, source={"type": "youtube_live", "url": "${YT_URL}"}) == (
        "source.url: la variable de entorno YT_URL no está definida",
    )
    assert dict(os.environ) == antes
