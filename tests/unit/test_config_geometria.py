"""La geometria del YAML: fracciones 0-1 dentro, pixeles al salir.

Se prueba con aritmetica a mano, sin video y sin modelo: la conversion es una
multiplicacion, y lo que hay que asegurar es que ocurre en un solo sitio y en un solo
sentido. `cvp.core` sigue hablando solo en pixeles.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from cvp.core import BBox
from cvp.pipeline import ConfigError, Point, Region, load_project_config
from cvp.pipeline.config import ParkingOccupancyAnalyzer

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


def problemas(tmp_path: Path, **cambios: object) -> tuple[str, ...]:
    documento = copy.deepcopy(MINIMO) | cambios
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_project_config(fichero, env={})
    return error.value.problems


def detector(roi: object) -> dict[str, object]:
    return {"type": "yolo", "classes": ["car"], "roi": roi}


# --- La conversion ----------------------------------------------------------------


def test_un_punto_se_convierte_a_pixeles() -> None:
    assert Point(x=0.5, y=0.25).to_pixels(1920, 1080) == (960.0, 270.0)


def test_una_region_se_convierte_a_un_bbox_de_cvp_core() -> None:
    caja = Region(x1=0.0, y1=0.5, x2=1.0, y2=1.0).to_pixels(1920, 1080)
    assert isinstance(caja, BBox)
    assert (caja.x1, caja.y1, caja.x2, caja.y2) == (0.0, 540.0, 1920.0, 1080.0)


def test_la_conversion_no_redondea() -> None:
    """`BBox` acepta floats; redondear es cosa de quien recorte la imagen."""
    caja = Region(x1=0.1, y1=0.1, x2=0.5, y2=0.5).to_pixels(1281, 721)
    assert caja.x1 == pytest.approx(128.1)
    assert caja.y2 == pytest.approx(360.5)
    assert isinstance(caja.x2, float)


def test_las_mismas_fracciones_sobreviven_a_un_cambio_de_resolucion() -> None:
    """Es el motivo de la decision: la fuente sirve el mismo directo en 1080p y en 360p."""
    region = Region(x1=0.0, y1=0.33, x2=1.0, y2=1.0)
    en_1080 = region.to_pixels(1920, 1080)
    en_360 = region.to_pixels(640, 360)
    assert en_1080.x2 / 1920 == en_360.x2 / 640
    assert en_1080.y1 / 1080 == pytest.approx(en_360.y1 / 360)


@pytest.mark.parametrize(("width", "height"), [(0, 1080), (1920, 0), (-1, -1)])
def test_convertir_sin_dimensiones_es_un_valueerror(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="píxeles"):
        Point(x=0.5, y=0.5).to_pixels(width, height)
    with pytest.raises(ValueError, match="píxeles"):
        Region(x1=0.0, y1=0.0, x2=1.0, y2=1.0).to_pixels(width, height)


# --- Como se escriben en el YAML --------------------------------------------------


def test_en_el_yaml_un_punto_es_una_lista_de_dos_y_una_region_de_cuatro(tmp_path: Path) -> None:
    documento = copy.deepcopy(MINIMO)
    documento["detector"] = detector([0.0, 0.33, 1.0, 1.0])
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8")
    config = load_project_config(fichero, env={})
    assert config.detector.roi == Region(x1=0.0, y1=0.33, x2=1.0, y2=1.0)
    assert config.analyzers[0].lines[0].p1 == Point(x=0.1, y=0.2)


def test_la_geometria_en_pixeles_ya_no_cuela(tmp_path: Path) -> None:
    """`p1: [120, 640]` era valido cuando el YAML hablaba en pixeles; ahora no."""
    lineas = [MINIMO["analyzers"][0]["lines"][0] | {"p1": [120, 640]}]
    fallos = problemas(tmp_path, analyzers=[{"type": "traffic_flow", "lines": lineas}])
    assert fallos == (
        "analyzers[0].lines[0].p1[0]: la geometría del project.yaml va en fracciones 0-1 "
        "del fotograma, y vale 120.0",
        "analyzers[0].lines[0].p1[1]: la geometría del project.yaml va en fracciones 0-1 "
        "del fotograma, y vale 640.0",
    )


@pytest.mark.parametrize("indice", [0, 1, 2, 3])
def test_una_fraccion_fuera_de_rango_nombra_el_campo_y_el_indice(
    tmp_path: Path, indice: int
) -> None:
    roi = [0.1, 0.2, 0.8, 0.9]
    roi[indice] = 1.4
    (problema,) = problemas(tmp_path, detector=detector(roi))
    assert problema.startswith(f"detector.roi[{indice}]:")
    assert problema.endswith("y vale 1.4")


@pytest.mark.parametrize("roi", [[0.1, 0.2, 0.8], [0.1, 0.2, 0.8, 0.9, 1.0], []])
def test_una_region_con_otro_numero_de_elementos_dice_la_forma_correcta(
    tmp_path: Path, roi: list[float]
) -> None:
    (problema,) = problemas(tmp_path, detector=detector(roi))
    assert "una región son 4 fracciones [x1, y1, x2, y2] entre 0 y 1" in problema


def test_un_punto_con_otro_numero_de_elementos_dice_la_forma_correcta(tmp_path: Path) -> None:
    lineas = [MINIMO["analyzers"][0]["lines"][0] | {"p2": [0.1, 0.2, 0.3]}]
    (problema,) = problemas(tmp_path, analyzers=[{"type": "traffic_flow", "lines": lineas}])
    assert "un punto son 2 fracciones [x, y] entre 0 y 1" in problema


@pytest.mark.parametrize(
    "roi",
    [[0.8, 0.2, 0.2, 0.9], [0.1, 0.9, 0.8, 0.2]],
    ids=["x-invertida", "y-invertida"],
)
def test_una_region_invertida_falla(tmp_path: Path, roi: list[float]) -> None:
    (problema,) = problemas(tmp_path, detector=detector(roi))
    assert problema.startswith("detector.roi: la región está invertida")


@pytest.mark.parametrize(
    "roi",
    [[0.4, 0.2, 0.4, 0.9], [0.1, 0.5, 0.8, 0.5]],
    ids=["ancho-cero", "alto-cero"],
)
def test_una_region_de_area_cero_falla(tmp_path: Path, roi: list[float]) -> None:
    """Un `BBox` de area cero si es legitimo; un ROI que no contiene nada, no."""
    assert BBox(10.0, 10.0, 10.0, 20.0).area == 0.0
    (problema,) = problemas(tmp_path, detector=detector(roi))
    assert problema.startswith("detector.roi: una región de área cero")


def test_la_geometria_se_valida_en_carga_sin_saber_la_resolucion(tmp_path: Path) -> None:
    """No hace falta abrir la fuente para saber que 1.4 no es una fraccion."""
    assert problemas(tmp_path, detector=detector([0.0, 0.0, 1.4, 1.0]))


def test_los_poligonos_del_zones_json_van_tambien_en_fracciones() -> None:
    """Aqui solo se documenta y se comprueba que el fichero existe; leerlo es otra tarea."""
    documentacion = " ".join((ParkingOccupancyAnalyzer.__doc__ or "").split())
    assert "en fracciones" in documentacion
