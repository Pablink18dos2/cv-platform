"""Los cinco protocolos, probados con implementaciones falsas escritas a mano.

Los falsos son locales a este fichero a proposito: los que compartiran los tests de
componente (`FakeVideoSource`, `FakeDetector`) los define el issue 8, y hasta entonces
un falso compartido solo seria una abstraccion adivinada.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import numpy as np
import pytest

from cvp.core import (
    Analyzer,
    AnalyzerOutput,
    BBox,
    Detection,
    Detector,
    Frame,
    Sink,
    SourceInfo,
    Track,
    Tracker,
    VideoSource,
)

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def fotograma(ancho: int = 1920, alto: int = 1080) -> Frame:
    return Frame(image=np.zeros((alto, ancho, 3), dtype=np.uint8), index=0, timestamp=AHORA)


# --- Implementaciones falsas ----------------------------------------------------------


class FuenteFalsa:
    def __init__(self) -> None:
        self.abierta = False

    @property
    def info(self) -> SourceInfo:
        return SourceInfo(width=1920, height=1080, native_fps=25.0, is_live=False)

    def open(self) -> None:
        self.abierta = True

    def frames(self) -> Iterator[Frame]:
        yield fotograma()

    def close(self) -> None:
        self.abierta = False


class DetectorFalso:
    """Detector que "ve" siempre el cuadrante central de lo que analiza.

    Lo unico que implementa de verdad es el contrato del ROI: recortar, y devolver las
    cajas traducidas al sistema de coordenadas del fotograma original. La inferencia real
    es el issue 6.
    """

    def __init__(self) -> None:
        self.calentado = False

    def warmup(self) -> None:
        self.calentado = True

    def detect(self, frame: Frame, roi: BBox | None = None) -> list[Detection]:
        alto, ancho = frame.image.shape[:2]
        region = BBox(0, 0, ancho, alto) if roi is None else roi.clipped(ancho, alto)
        recorte = frame.image[int(region.y1) : int(region.y2), int(region.x1) : int(region.x2)]
        alto_recorte, ancho_recorte = recorte.shape[:2]
        en_el_recorte = BBox(
            ancho_recorte * 0.25, alto_recorte * 0.25, ancho_recorte * 0.75, alto_recorte * 0.75
        )
        return [
            Detection(
                bbox=en_el_recorte.translated(region.x1, region.y1),
                class_id=2,
                label="car",
                confidence=0.8,
            )
        ]


class TrackerFalso:
    def update(self, frame: Frame, detections: list[Detection]) -> list[Track]:
        return [Track.from_detection(d, track_id=i) for i, d in enumerate(detections)]

    def reset(self) -> None:
        pass


class AnalizadorFalso:
    def __init__(self) -> None:
        self.name = "traffic_flow"
        self.every_n_frames = 1

    def process(self, frame: Frame, tracks: list[Track]) -> AnalyzerOutput:
        return AnalyzerOutput.empty()

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray:
        return image.copy()


class SinkFalso:
    def __init__(self) -> None:
        self.escrito: list[tuple[Frame, AnalyzerOutput]] = []

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        self.escrito.append((frame, output))

    def flush(self) -> None:
        pass


class SinkSinFlush:
    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        pass


# --- isinstance -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("falso", "protocolo"),
    [
        (FuenteFalsa(), VideoSource),
        (DetectorFalso(), Detector),
        (TrackerFalso(), Tracker),
        (AnalizadorFalso(), Analyzer),
        (SinkFalso(), Sink),
    ],
    ids=["VideoSource", "Detector", "Tracker", "Analyzer", "Sink"],
)
def test_una_implementacion_completa_encaja_en_su_protocolo(falso: object, protocolo: type) -> None:
    assert isinstance(falso, protocolo)


def test_a_un_sink_sin_flush_no_le_vale_con_escribir() -> None:
    assert isinstance(SinkSinFlush(), Sink) is False


def test_los_protocolos_se_comprueban_con_instancias_no_con_issubclass() -> None:
    # `Analyzer` tiene atributos ademas de metodos, y `issubclass` no esta soportado
    # sobre un data protocol. Es el motivo por el que todo esto se prueba con instancias.
    with pytest.raises(TypeError):
        issubclass(AnalizadorFalso, Analyzer)


# --- El contrato del ROI --------------------------------------------------------------


def test_con_roi_las_cajas_vuelven_en_coordenadas_del_fotograma_original() -> None:
    roi = BBox(600, 300, 1240, 700)
    detecciones = DetectorFalso().detect(fotograma(1920, 1080), roi=roi)

    assert detecciones
    for deteccion in detecciones:
        caja = deteccion.bbox
        assert caja.x1 >= 600
        assert caja.y1 >= 300
        assert caja.x2 <= 1240
        assert caja.y2 <= 700


def test_sin_roi_las_cajas_caen_donde_caen_en_el_fotograma_entero() -> None:
    [deteccion] = DetectorFalso().detect(fotograma(1920, 1080), roi=None)

    assert deteccion.bbox == BBox(480, 270, 1440, 810)
    # Y no es la misma caja que con ROI: el recorte cambia lo que se mira, no el sistema
    # de coordenadas en el que se contesta.
    assert deteccion.bbox.x1 < 600


def test_el_roi_por_defecto_es_el_fotograma_entero() -> None:
    frame = fotograma(1920, 1080)
    assert DetectorFalso().detect(frame) == DetectorFalso().detect(frame, roi=None)


def test_un_roi_que_se_sale_del_fotograma_se_recorta() -> None:
    detecciones = DetectorFalso().detect(fotograma(640, 480), roi=BBox(-100, -100, 320, 240))
    assert detecciones[0].bbox == BBox(80, 60, 240, 180)


def test_un_roi_sin_interseccion_con_el_fotograma_es_un_error() -> None:
    with pytest.raises(ValueError):
        DetectorFalso().detect(fotograma(640, 480), roi=BBox(900, 900, 1000, 1000))


def test_el_detector_no_guarda_el_roi_como_estado() -> None:
    detector = DetectorFalso()
    frame = fotograma(1920, 1080)

    con_roi = detector.detect(frame, roi=BBox(600, 300, 1240, 700))
    sin_roi = detector.detect(frame)

    assert con_roi[0].bbox != sin_roi[0].bbox
    assert sin_roi == detector.detect(frame)


def test_el_docstring_de_detect_fija_el_contrato_del_roi() -> None:
    doc = Detector.detect.__doc__ or ""
    assert "pixeles del fotograma original" in doc
    assert "roi=None" in doc
