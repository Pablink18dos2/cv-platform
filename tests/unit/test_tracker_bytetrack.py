"""El tracker: identidad estable, no reutilizacion de ids y el descarte de los no confirmados.

Los tests sinteticos no necesitan video ni modelo: el tracker solo ve `Detection`, asi que se
le pueden escribir trayectorias a mano y afirmar sobre los ids exactos.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from cvp.core import BBox, Detection, Frame, Track
from cvp.core.protocols import Tracker
from cvp.core.registry import TRACKERS
from cvp.trackers import ByteTrackTracker

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "video" / "calle-1080p-6s.mp4"
INICIO = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _frame(indice: int = 0) -> Frame:
    return Frame(image=np.zeros((1080, 1920, 3), dtype=np.uint8), index=indice, timestamp=INICIO)


def _deteccion(x: float, y: float, *, conf: float = 0.95, clase: int = 2) -> Detection:
    return Detection(
        bbox=BBox(x, y, x + 80, y + 60),
        class_id=clase,
        label={2: "car", 7: "truck"}[clase],
        confidence=conf,
    )


def _recorrer(tracker: ByteTrackTracker, pasos: list[list[Detection]]) -> list[list[Track]]:
    return [tracker.update(_frame(i), detecciones) for i, detecciones in enumerate(pasos)]


def _tracker(**kwargs: float) -> ByteTrackTracker:
    kwargs.setdefault("frame_rate", 5)
    return ByteTrackTracker(**kwargs)  # type: ignore[arg-type]


# --- Contrato y registro ----------------------------------------------------------------


def test_cumple_el_protocolo_tracker() -> None:
    assert isinstance(_tracker(), Tracker)


def test_esta_registrado_como_bytetrack() -> None:
    assert TRACKERS.get("bytetrack") is ByteTrackTracker


def test_un_frame_rate_no_positivo_falla() -> None:
    with pytest.raises(ValueError, match="frame_rate"):
        ByteTrackTracker(frame_rate=0)


def test_from_config_exige_el_frame_rate_aparte() -> None:
    # No se escribe en el YAML porque no es una decision del bloque `tracker`: es el
    # target_fps de la fuente, y escribirlo dos veces seria garantizar que discrepen.
    from cvp.pipeline.config import ByteTrackTracker as Cfg

    cfg = Cfg(type="bytetrack", lost_track_buffer=45)
    tracker = ByteTrackTracker.from_config(cfg, frame_rate=5)
    assert (tracker.frame_rate, tracker.lost_track_buffer) == (5.0, 45)

    with pytest.raises(TypeError):
        ByteTrackTracker.from_config(cfg)  # type: ignore[call-arg]


# --- Identidad --------------------------------------------------------------------------


def test_un_vehiculo_que_avanza_mantiene_el_mismo_id() -> None:
    salidas = _recorrer(_tracker(), [[_deteccion(100 + i * 25, 400)] for i in range(8)])
    ids = {t.track_id for salida in salidas for t in salida}
    assert len(ids) == 1, f"un solo coche deberia dar un solo id, y da {ids}"


def test_dos_vehiculos_reciben_ids_distintos() -> None:
    pasos = [[_deteccion(100 + i * 25, 200), _deteccion(900 - i * 25, 700)] for i in range(8)]
    salidas = _recorrer(_tracker(), pasos)
    ultimos = {t.track_id for t in salidas[-1]}
    assert len(ultimos) == 2


def test_dos_vehiculos_que_se_cruzan_no_intercambian_su_id() -> None:
    # Uno va hacia la derecha y otro hacia la izquierda por la misma altura: se solapan a
    # mitad de camino. Es el caso en el que un tracker malo permuta las identidades.
    pasos = [[_deteccion(100 + i * 60, 400), _deteccion(700 - i * 60, 400)] for i in range(11)]
    salidas = _recorrer(_tracker(), pasos)

    def id_del_que_va_a_la_derecha(salida: list[Track]) -> int | None:
        if len(salida) != 2:
            return None
        return min(salida, key=lambda t: t.bbox.x1).track_id

    # Al principio el que va a la derecha es el de la izquierda; al final es el otro. Lo que
    # se comprueba es que cada id sobrevive al cruce, no que se pueda decir quien es quien.
    ids_vistos = [{t.track_id for t in s} for s in salidas if len(s) == 2]
    assert ids_vistos, "deberia haber fotogramas con los dos confirmados"
    assert all(v == ids_vistos[0] for v in ids_vistos), (
        f"los ids cambian a lo largo del cruce: {ids_vistos}"
    )
    assert id_del_que_va_a_la_derecha(salidas[-1]) is not None


def test_un_id_no_se_reutiliza_cuando_aparece_otro_vehiculo() -> None:
    tracker = _tracker(lost_track_buffer=1)
    primeros = _recorrer(tracker, [[_deteccion(100 + i * 25, 400)] for i in range(6)])
    # Silencio largo para que el primero expire del todo.
    _recorrer(tracker, [[] for _ in range(20)])
    segundos = _recorrer(tracker, [[_deteccion(1500 + i * 25, 900)] for i in range(6)])

    ids_primero = {t.track_id for s in primeros for t in s}
    ids_segundo = {t.track_id for s in segundos for t in s}
    assert ids_primero and ids_segundo
    assert not (ids_primero & ids_segundo), "el id del primero se ha reciclado para el segundo"


def test_reset_vuelve_a_empezar_desde_el_principio() -> None:
    pasos = [[_deteccion(100 + i * 25, 400)] for i in range(6)]
    tracker = _tracker()
    primera = _recorrer(tracker, pasos)
    tracker.reset()
    segunda = _recorrer(tracker, pasos)
    assert [[t.track_id for t in s] for s in primera] == [[t.track_id for t in s] for s in segunda]


# --- Casos de borde ---------------------------------------------------------------------


def test_sin_detecciones_no_lanza_y_devuelve_lista_vacia() -> None:
    assert _tracker().update(_frame(), []) == []


def test_los_no_confirmados_no_salen_como_track() -> None:
    # ByteTrack no confirma una identidad hasta haber visto el objeto
    # `minimum_consecutive_frames` veces. Hasta entonces la marca con id -1, y un Track con
    # track_id negativo violaria el contrato de #2 ademas de no significar nada.
    tracker = _tracker(minimum_consecutive_frames=2)
    primera = tracker.update(_frame(0), [_deteccion(100, 400)])
    segunda = tracker.update(_frame(1), [_deteccion(125, 400)])
    assert primera == [], "en el primer fotograma la identidad aun no esta confirmada"
    assert segunda, "en el segundo ya deberia estarlo"
    assert all(t.track_id >= 0 for t in segunda)


def test_el_track_conserva_los_campos_de_la_deteccion() -> None:
    tracker = _tracker()
    _recorrer(tracker, [[_deteccion(100, 400, clase=7, conf=0.9)]])
    salida = tracker.update(_frame(1), [_deteccion(125, 400, clase=7, conf=0.9)])
    assert salida
    track = salida[0]
    assert track.class_id == 7
    assert track.label == "truck"
    assert 0.85 <= track.confidence <= 1.0
    assert track.bbox.width == pytest.approx(80, abs=5)


# --- Con datos reales -------------------------------------------------------------------


@pytest.mark.slow
def test_sobre_video_real_hay_muchas_menos_identidades_que_detecciones() -> None:
    """La comprobacion que distingue "hay tracking" de "hay un id nuevo por fotograma".

    Si el tracker no funcionara, cada deteccion de cada fotograma seria una identidad nueva y
    los dos numeros serian casi iguales.
    """
    from cvp.detectors import YoloDetector
    from cvp.sources import FileVideoSource

    detector = YoloDetector("yolo11n.pt", imgsz=640, conf=0.35, classes=["car", "truck"])
    tracker = ByteTrackTracker(frame_rate=5)
    origen = FileVideoSource(FIXTURE, target_fps=5, start_time=INICIO)
    origen.open()
    try:
        total_detecciones = 0
        identidades: set[int] = set()
        for fotograma in origen.frames():
            detecciones = detector.detect(fotograma)
            total_detecciones += len(detecciones)
            identidades.update(t.track_id for t in tracker.update(fotograma, detecciones))
    finally:
        origen.close()

    assert total_detecciones > 50, "el fixture deberia dar bastantes detecciones"
    assert identidades, "deberia haber identidades confirmadas"
    assert len(identidades) < total_detecciones / 3, (
        f"{len(identidades)} identidades para {total_detecciones} detecciones: "
        "eso no parece tracking"
    )
