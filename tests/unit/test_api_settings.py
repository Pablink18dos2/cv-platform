"""`cvp api` y sus `Settings`: las variables de `.env.example`, y lo que pasa si no valen.

Arrancar uvicorn de verdad es la comprobacion manual del issue; aqui se prueba lo que
hay antes: que el entorno se lee con los mismos nombres que el worker, que una variable
vacia cuenta como ausente, y que un valor que no vale sale con 1 y sin traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cvp.api import Settings
from cvp.api.settings import (
    HOST_POR_DEFECTO,
    INTERVALO_STREAM_POR_DEFECTO_S,
    ORIGENES_POR_DEFECTO,
    PUERTO_POR_DEFECTO,
)
from cvp.cli import build_parser, main
from cvp.storage import URL_POR_DEFECTO


def test_sin_entorno_valen_los_defaults_de_env_example() -> None:
    ajustes = Settings.desde_entorno({})
    assert ajustes.database_url == URL_POR_DEFECTO == "sqlite:///data/cvp.sqlite3"
    assert (ajustes.host, ajustes.port) == (HOST_POR_DEFECTO, PUERTO_POR_DEFECTO)
    assert ajustes.cors_origins == ORIGENES_POR_DEFECTO == ("http://localhost:5173",)
    assert ajustes.preview_dir == Path("data") / "preview"
    assert ajustes.stream_interval_s == INTERVALO_STREAM_POR_DEFECTO_S == 0.25


def test_el_entorno_manda_y_database_url_es_la_misma_variable_que_el_worker() -> None:
    ajustes = Settings.desde_entorno(
        {
            "DATABASE_URL": "sqlite:///otra.sqlite3",
            "CVP_API_HOST": "0.0.0.0",
            "CVP_API_PORT": "9000",
            "CVP_CORS_ORIGINS": "http://a:1, http://b:2 ,,",
        }
    )
    assert ajustes.database_url == "sqlite:///otra.sqlite3"
    assert (ajustes.host, ajustes.port) == ("0.0.0.0", 9000)
    assert ajustes.cors_origins == ("http://a:1", "http://b:2")


def test_una_variable_vacia_cuenta_como_ausente() -> None:
    """La misma regla que la configuracion del worker (#3): una cadena vacia colada en
    una URL produce un fallo lejos de su causa."""
    ajustes = Settings.desde_entorno({"DATABASE_URL": "", "CVP_API_PORT": "  "})
    assert ajustes.database_url == URL_POR_DEFECTO
    assert ajustes.port == PUERTO_POR_DEFECTO


@pytest.mark.parametrize(
    ("kwargs", "mensaje"),
    [
        ({"port": 0}, "port"),
        ({"port": 70000}, "port"),
        ({"stream_interval_s": 0}, "stream_interval_s"),
    ],
)
def test_un_valor_imposible_se_rechaza_al_construir(kwargs: dict[str, Any], mensaje: str) -> None:
    with pytest.raises(ValueError, match=mensaje):
        Settings(**kwargs)


def test_un_puerto_que_no_es_un_numero_sale_con_1_y_sin_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["api"], env={"CVP_API_PORT": "abc"}) == 1
    salida = capsys.readouterr()
    assert "CVP_API_PORT='abc' no es un puerto" in salida.err
    assert "Traceback" not in salida.err


def test_host_y_port_de_la_linea_de_comandos_mandan_sobre_el_entorno() -> None:
    args = build_parser().parse_args(["api", "--host", "0.0.0.0", "--port", "8123"])
    assert (args.comando, args.host, args.port) == ("api", "0.0.0.0", 8123)
    assert args.export_openapi is None
    sin_nada = build_parser().parse_args(["api"])
    assert (sin_nada.host, sin_nada.port) == (None, None)


def test_export_openapi_sin_valor_apunta_a_la_raiz_del_repo() -> None:
    args = build_parser().parse_args(["api", "--export-openapi"])
    assert args.export_openapi == "openapi.yaml"
    args = build_parser().parse_args(["api", "--export-openapi", "otro.yaml"])
    assert args.export_openapi == "otro.yaml"


def test_el_reloj_por_defecto_es_utc_con_zona() -> None:
    """El unico `datetime.now()` de la API es el valor por defecto de `clock`, y tiene
    que dar una marca con zona: el resto del sistema rechaza las naive."""
    from datetime import UTC, datetime

    from cvp.api.app import _ahora_del_sistema

    marca = _ahora_del_sistema()
    assert marca.tzinfo is UTC
    assert abs((datetime.now(UTC) - marca).total_seconds()) < 5
