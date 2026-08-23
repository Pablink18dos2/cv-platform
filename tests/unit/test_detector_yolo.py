"""El detector YOLO: filtrado, traduccion de coordenadas del ROI y frontera con ultralytics.

Los tests que cargan el modelo real van marcados `slow` y quedan fuera de CI. Todo lo que se
puede probar con un doble del modelo —filtrado, ROI, errores, tipos— corre en la suite por
defecto, que es donde de verdad protege.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from cvp.core import BBox, Detection, Frame
from cvp.core.protocols import Detector
from cvp.core.registry import DETECTORS
from cvp.detectors import DetectorError, YoloDetector

# El detector se prueba contra el fixture a 1080p, que es la resolucion a la que
# correra el pipeline de verdad. El de 640x360 sirve para probar el connector (cuentas
# de muestreo) pero no el detector: a 360p los coches del plano medio ya son manchas y
# cualquier conclusion sobre imgsz sacada de ahi no transfiere.
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "video" / "calle-1080p-6s.mp4"
INICIO = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
CLASES_COCO = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


# --- Dobles del modelo ------------------------------------------------------------------


class _Escalar:
    """Imita un tensor de torch de cero dimensiones: se compara y se imprime como numero."""

    def __init__(self, valor: float) -> None:
        self._valor = valor

    def item(self) -> float:
        return self._valor


class _Vector:
    """Imita la fila de un tensor de torch: se convierte a lista con `.tolist()`."""

    def __init__(self, valores: tuple[float, float, float, float]) -> None:
        self._valores = valores

    def tolist(self) -> list[float]:
        return list(self._valores)


class _CajaFalsa:
    def __init__(self, xyxy: tuple[float, float, float, float], cls: int, conf: float) -> None:
        self.xyxy = [_Vector(xyxy)]
        self.cls = _Escalar(cls)
        self.conf = _Escalar(conf)


class _ResultadoFalso:
    def __init__(self, cajas: list[_CajaFalsa]) -> None:
        self.boxes = cajas


class _ModeloFalso:
    """Devuelve cajas fijas en coordenadas de lo que se le pase, y registra las llamadas."""

    def __init__(self, cajas: list[_CajaFalsa] | None = None) -> None:
        self.names = dict(CLASES_COCO)
        self.cajas = cajas if cajas is not None else [_CajaFalsa((10, 20, 50, 60), 2, 0.9)]
        self.llamadas: list[dict[str, Any]] = []

    def predict(self, imagen: np.ndarray, **kwargs: Any) -> list[_ResultadoFalso]:
        self.llamadas.append({"shape": imagen.shape, **kwargs})
        return [_ResultadoFalso(self.cajas)]


def _frame(ancho: int = 1920, alto: int = 1080) -> Frame:
    return Frame(image=np.zeros((alto, ancho, 3), dtype=np.uint8), index=0, timestamp=INICIO)


def _detector(**kwargs: Any) -> YoloDetector:
    kwargs.setdefault("modelo", _ModeloFalso())
    kwargs.setdefault("classes", ["car"])
    return YoloDetector("yolo11n.pt", **kwargs)


# --- Contrato y registro ----------------------------------------------------------------


def test_cumple_el_protocolo_detector() -> None:
    assert isinstance(_detector(), Detector)


def test_esta_registrado_como_yolo() -> None:
    assert DETECTORS.get("yolo") is YoloDetector


def test_from_config_construye_desde_el_modelo_de_configuracion() -> None:
    from cvp.pipeline.config import YoloDetector as Cfg

    cfg = Cfg(type="yolo", weights="yolo11n.pt", imgsz=960, conf=0.5, classes=["car", "truck"])
    detector = YoloDetector.from_config(cfg, modelo=_ModeloFalso())
    assert (detector.imgsz, detector.conf, detector.classes) == (960, 0.5, ["car", "truck"])


# --- La frontera con ultralytics --------------------------------------------------------


def test_solo_salen_detection_del_core() -> None:
    detecciones = _detector().detect(_frame())
    assert all(type(d) is Detection for d in detecciones)


def test_los_campos_son_tipos_de_python_y_no_tensores() -> None:
    # `float(x) == x` es verdad tambien para un tensor de cero dimensiones, asi que un
    # tensor colado aqui no se notaria hasta que alguien intentara serializarlo. Por eso
    # se mira el type() y no el valor.
    d = _detector().detect(_frame())[0]
    assert type(d.confidence) is float
    assert type(d.class_id) is int
    assert type(d.label) is str
    assert all(type(v) is float for v in (d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2))


def test_la_etiqueta_y_el_id_de_clase_son_coherentes() -> None:
    d = _detector().detect(_frame())[0]
    assert (d.class_id, d.label) == (2, "car")


def test_sin_detecciones_devuelve_una_lista_vacia() -> None:
    detector = _detector(modelo=_ModeloFalso(cajas=[]))
    assert detector.detect(_frame()) == []


# --- Filtrado ---------------------------------------------------------------------------


def test_las_clases_se_configuran_por_nombre_y_los_ids_llegan_a_ultralytics() -> None:
    modelo = _ModeloFalso()
    detector = _detector(modelo=modelo, classes=["car", "bus", "truck"])
    detector.detect(_frame())
    assert modelo.llamadas[-1]["classes"] == [2, 5, 7]


def test_una_clase_que_el_modelo_no_conoce_falla_al_construir() -> None:
    # Al construir y no en la primera inferencia: asi el worker no arranca con un YAML malo
    # y se cae cuarenta minutos despues.
    with pytest.raises(ValueError, match="coche"):
        _detector(classes=["coche"])


def test_el_error_de_clase_desconocida_lista_las_disponibles() -> None:
    with pytest.raises(ValueError, match="car"):
        _detector(classes=["coche"])


def test_sin_clases_no_se_filtra_por_clase() -> None:
    modelo = _ModeloFalso()
    detector = YoloDetector("yolo11n.pt", modelo=modelo, classes=None)
    detector.detect(_frame())
    assert modelo.llamadas[-1]["classes"] is None


def test_la_confianza_configurada_llega_a_ultralytics() -> None:
    modelo = _ModeloFalso()
    _detector(modelo=modelo, conf=0.42).detect(_frame())
    assert modelo.llamadas[-1]["conf"] == 0.42


# --- Region de interes ------------------------------------------------------------------


def test_sin_roi_se_infiere_sobre_el_fotograma_entero() -> None:
    modelo = _ModeloFalso()
    _detector(modelo=modelo).detect(_frame(1920, 1080))
    assert modelo.llamadas[-1]["shape"] == (1080, 1920, 3)


def test_con_roi_se_infiere_solo_sobre_el_recorte() -> None:
    modelo = _ModeloFalso()
    _detector(modelo=modelo).detect(_frame(1920, 1080), roi=BBox(600, 300, 1240, 700))
    assert modelo.llamadas[-1]["shape"] == (400, 640, 3)


def test_las_cajas_vuelven_en_coordenadas_del_fotograma_original() -> None:
    # El modelo falso devuelve (10, 20, 50, 60) en coordenadas del recorte. Con un ROI que
    # empieza en (600, 300), la caja tiene que salir en (610, 320, 650, 360). Un fallo de
    # traduccion que restara en vez de sumar seguiria cayendo "dentro del ROI" si solo
    # comprobaramos eso, y aqui se ve.
    detector = _detector()
    d = detector.detect(_frame(1920, 1080), roi=BBox(600, 300, 1240, 700))[0]
    assert (d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) == (610.0, 320.0, 650.0, 360.0)


def test_el_roi_es_por_llamada_y_no_estado() -> None:
    # Dos analizadores tienen que poder pedir regiones distintas del mismo fotograma.
    detector = _detector()
    fotograma = _frame(1920, 1080)
    a = detector.detect(fotograma, roi=BBox(600, 300, 1240, 700))[0]
    b = detector.detect(fotograma, roi=BBox(0, 0, 640, 400))[0]
    c = detector.detect(fotograma)[0]
    assert a.bbox.x1 == 610.0
    assert b.bbox.x1 == 10.0
    assert c.bbox.x1 == 10.0


def test_un_roi_que_se_sale_del_fotograma_se_recorta_a_sus_limites() -> None:
    modelo = _ModeloFalso()
    _detector(modelo=modelo).detect(_frame(640, 360), roi=BBox(400, 200, 5000, 5000))
    assert modelo.llamadas[-1]["shape"] == (160, 240, 3)


def test_un_roi_sin_interseccion_con_el_fotograma_falla() -> None:
    with pytest.raises(ValueError):
        _detector().detect(_frame(640, 360), roi=BBox(2000, 2000, 2100, 2100))


# --- Pesos ------------------------------------------------------------------------------


def test_una_ruta_de_pesos_explicita_que_no_existe_falla_con_la_ruta(tmp_path: Path) -> None:
    with pytest.raises(DetectorError, match="no existe"):
        YoloDetector(str(tmp_path / "sub" / "no-estan.pt"), classes=["car"])


def test_los_pesos_estan_ignorados_por_git() -> None:
    import subprocess

    raiz = Path(__file__).resolve().parents[2]
    resultado = subprocess.run(
        ["git", "check-ignore", "models/yolo11n.pt"],
        cwd=raiz,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, "models/yolo11n.pt deberia estar en .gitignore"


# --- Calentamiento ----------------------------------------------------------------------


def test_warmup_hace_una_inferencia_en_vacio() -> None:
    modelo = _ModeloFalso()
    _detector(modelo=modelo, imgsz=640).warmup()
    assert modelo.llamadas[-1]["shape"] == (640, 640, 3)


# --- Con el modelo de verdad ------------------------------------------------------------


@pytest.fixture(scope="module")
def detector_real() -> YoloDetector:
    detector = YoloDetector("yolo11n.pt", imgsz=640, conf=0.3, classes=["car", "bus", "truck"])
    detector.warmup()
    return detector


@pytest.fixture(scope="module")
def fotograma_real() -> Frame:
    from cvp.sources import FileVideoSource

    origen = FileVideoSource(FIXTURE, target_fps=1, start_time=INICIO)
    origen.open()
    try:
        return next(iter(origen.frames()))
    finally:
        origen.close()


@pytest.mark.slow
def test_detecta_vehiculos_en_el_fixture(
    detector_real: YoloDetector, fotograma_real: Frame
) -> None:
    detecciones = detector_real.detect(fotograma_real)
    assert len(detecciones) > 5, "una calle con aparcamiento deberia dar bastantes vehiculos"
    assert {d.label for d in detecciones} <= {"car", "bus", "truck"}
    assert all(0.3 <= d.confidence <= 1.0 for d in detecciones)


@pytest.mark.slow
def test_dos_llamadas_sobre_el_mismo_fotograma_dan_exactamente_lo_mismo(
    detector_real: YoloDetector, fotograma_real: Frame
) -> None:
    # Sin determinismo no se puede afirmar nada en un test ni comparar dos configuraciones.
    primera = detector_real.detect(fotograma_real)
    segunda = detector_real.detect(fotograma_real)
    assert primera == segunda


@pytest.mark.slow
def test_con_roi_un_vehiculo_sale_en_el_mismo_sitio_que_sin_roi(
    detector_real: YoloDetector, fotograma_real: Frame
) -> None:
    """La prueba de verdad de la traduccion de coordenadas.

    No basta con comprobar que las cajas caen dentro del ROI: un fallo que restara en vez de
    sumar tambien caeria dentro. Se coge un vehiculo detectado con el fotograma entero, se
    pide un ROI que lo contiene, y se comprueba que sale en el mismo sitio.
    """
    completas = detector_real.detect(fotograma_real)
    assert completas, "el fixture deberia dar detecciones"
    objetivo = max(completas, key=lambda d: d.confidence)

    margen = 60.0
    alto, ancho = fotograma_real.image.shape[:2]
    roi = BBox(
        max(0.0, objetivo.bbox.x1 - margen),
        max(0.0, objetivo.bbox.y1 - margen),
        min(float(ancho), objetivo.bbox.x2 + margen),
        min(float(alto), objetivo.bbox.y2 + margen),
    )

    con_roi = detector_real.detect(fotograma_real, roi=roi)
    assert con_roi, "el vehiculo deberia seguir detectandose dentro de su propio ROI"

    cx, cy = objetivo.bbox.center

    def _distancia(d: Detection) -> float:
        dx, dy = d.bbox.center
        return abs(dx - cx) + abs(dy - cy)

    mas_cercana = min(con_roi, key=_distancia)
    dcx, dcy = mas_cercana.bbox.center
    assert abs(dcx - cx) < 15, f"centro x: {dcx} frente a {cx}"
    assert abs(dcy - cy) < 15, f"centro y: {dcy} frente a {cy}"
