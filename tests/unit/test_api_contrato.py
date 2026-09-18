"""`openapi.yaml` es el contrato con #13, y este test es lo que lo convierte en contrato.

Sin el, el fichero seria documentacion: alguien cambiaria una ruta, se olvidaria de
regenerarlo, y el cliente generado del frontend seguiria hablando con una API que ya no
existe. Con el, cambiar la API sin regenerar el contrato rompe CI en el mismo commit.

Compara los documentos **parseados**, no el texto: el formato del YAML no forma parte
del contrato, y un editor que reindente el fichero no tiene por que romper nada.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cvp.api import Settings, crear_app
from cvp.cli import RUTA_OPENAPI, exportar_openapi, main

RAIZ = Path(__file__).resolve().parents[2]
CONTRATO = RAIZ / RUTA_OPENAPI

COMO_REGENERARLO = (
    "openapi.yaml no coincide con lo que genera la app. Si el cambio en la API es "
    "intencionado, regeneralo con `uv run cvp api --export-openapi` desde la raiz del "
    "repo y commitea el fichero junto con el codigo"
)


def _generado() -> dict:
    return crear_app(Settings(database_url="sqlite://")).openapi()


def test_el_contrato_commiteado_coincide_con_lo_que_genera_la_app() -> None:
    assert CONTRATO.is_file(), f"falta {CONTRATO}: {COMO_REGENERARLO}"
    commiteado = yaml.safe_load(CONTRATO.read_text(encoding="utf-8"))
    assert commiteado == _generado(), COMO_REGENERARLO


def test_exportar_el_contrato_produce_exactamente_lo_que_se_compara(tmp_path: Path) -> None:
    """La ida y vuelta por YAML no pierde nada: lo que `--export-openapi` escribe es lo
    que este test lee. Sin esto, un tipo que YAML no representara igual (un `datetime`,
    una tupla) haria fallar el test de arriba sin que regenerar arreglara nada."""
    destino = tmp_path / "openapi.yaml"
    exportar_openapi(crear_app(Settings(database_url="sqlite://")), destino)
    assert yaml.safe_load(destino.read_text(encoding="utf-8")) == _generado()


def test_cvp_api_export_openapi_escribe_el_fichero_y_sale_con_cero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["api", "--export-openapi"], env={}) == 0
    assert RUTA_OPENAPI in capsys.readouterr().out
    escrito = yaml.safe_load((tmp_path / RUTA_OPENAPI).read_text(encoding="utf-8"))
    assert escrito == _generado()
    # No ha creado `data/`: exportar un fichero de texto no abre la base por defecto.
    assert not (tmp_path / "data").exists()


def test_cvp_api_export_openapi_acepta_otra_ruta(tmp_path: Path) -> None:
    destino = tmp_path / "otro" / "contrato.yaml"
    destino.parent.mkdir()
    assert main(["api", "--export-openapi", str(destino)], env={}) == 0
    assert destino.is_file()


def test_el_contrato_documenta_los_bordes_de_metric_minute() -> None:
    """Lo que el cliente de #13 vera en su editor: que `null` es un hueco, que `samples`
    nunca es 0, que `seconds` puede pasar de 60, y que columna es para que."""
    esquemas = _generado()["components"]["schemas"]
    punto = esquemas["MinutePoint"]["properties"]
    assert "60" in punto["seconds"]["description"]
    assert "null" in punto["samples"]["description"]
    assert "conteo" in punto["sum_value"]["description"]
    assert "gauge" in punto["mean_value"]["description"]
    assert "null" in esquemas["Series"]["properties"]["points"]["description"]


def test_los_errores_tienen_una_sola_forma_en_el_contrato() -> None:
    """Ningun `HTTPValidationError` de FastAPI: los 422 se documentan con `ErrorResponse`,
    que es la forma que fija `api.md`."""
    documento = _generado()
    assert "HTTPValidationError" not in documento["components"]["schemas"]
    for ruta, metodos in documento["paths"].items():
        for respuesta in metodos["get"]["responses"].values():
            for contenido in respuesta.get("content", {}).values():
                referencia = contenido.get("schema", {}).get("$ref", "")
                if referencia.endswith("Error"):
                    assert referencia == "#/components/schemas/ErrorResponse", ruta
