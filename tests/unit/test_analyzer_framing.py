"""El guardia del encuadre, con una imagen sintetica con textura y timestamps inyectados.

La puntuacion se prueba con comportamiento (mismo encuadre, otra luz, camara movida) y la
histeresis con puntuaciones forzadas, para no depender de SIFT en cada caso.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from cvp.analyzers import FramingGuardAnalyzer
from cvp.core import ANALYZERS, Frame

INICIO = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
RAIZ = Path(__file__).resolve().parents[2]


def escena() -> np.ndarray:
    """Cuadros y circulos al azar: la textura que SIFT necesita."""
    rng = np.random.default_rng(7)
    imagen = np.full((360, 640, 3), 128, dtype=np.uint8)
    for _ in range(150):
        x, y = int(rng.integers(0, 600)), int(rng.integers(0, 330))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            cv2.rectangle(imagen, (x, y), (x + int(rng.integers(8, 40)), y + 20), color, -1)
        else:
            cv2.circle(imagen, (x, y), int(rng.integers(5, 20)), color, -1)
    return cv2.GaussianBlur(imagen, (0, 0), 1.2)


def mover(imagen: np.ndarray, *, dx: float = 0, zoom: float = 1.0) -> np.ndarray:
    alto, ancho = imagen.shape[:2]
    m = cv2.getRotationMatrix2D((ancho / 2, alto / 2), 0, zoom)
    m[0, 2] += dx
    return cv2.warpAffine(imagen, m, (ancho, alto), borderMode=cv2.BORDER_REFLECT)


def fotograma(t: float, imagen: np.ndarray) -> Frame:
    return Frame(image=imagen, index=0, timestamp=INICIO + timedelta(seconds=t))


@pytest.fixture
def guardia() -> FramingGuardAnalyzer:
    return FramingGuardAnalyzer(escena(), lost_after_s=10, restored_after_s=10)


def test_esta_registrado_como_analizador() -> None:
    assert ANALYZERS.get("framing_guard") is FramingGuardAnalyzer


def test_la_misma_imagen_puntua_casi_uno(guardia: FramingGuardAnalyzer) -> None:
    assert guardia.puntuar(escena()) > 0.95


def test_otra_luz_sigue_siendo_el_mismo_encuadre(guardia: FramingGuardAnalyzer) -> None:
    oscura = cv2.convertScaleAbs(escena(), alpha=0.6, beta=-20)
    assert guardia.puntuar(oscura) > guardia.min_similarity


@pytest.mark.parametrize("cambio", [{"dx": 30}, {"zoom": 1.1}])
def test_una_camara_movida_puntua_bajo(guardia: FramingGuardAnalyzer, cambio: dict) -> None:
    assert guardia.puntuar(mover(escena(), **cambio)) < guardia.min_similarity


def test_una_imagen_sin_textura_puntua_cero(guardia: FramingGuardAnalyzer) -> None:
    assert guardia.puntuar(np.zeros((360, 640, 3), dtype=np.uint8)) == 0.0


def test_la_perdida_se_confirma_tras_lost_after_s_y_la_recuperacion_tras_restored_after_s(
    guardia: FramingGuardAnalyzer,
) -> None:
    bien, movida = escena(), mover(escena(), dx=40)
    secuencia = [
        (0, movida, []),  # arranca la racha
        (9, movida, []),  # 9 s < 10 s: un camion parado no basta
        (10, bien, []),  # una observacion buena la reinicia
        (11, movida, []),
        (21, movida, ["framing_lost"]),  # 10 s seguidos
        (22, movida, []),  # ya confirmado: no se repite
        (25, bien, []),
        (35, bien, ["framing_restored"]),
    ]
    for t, imagen, esperado in secuencia:
        salida = guardia.process(fotograma(t, imagen), [], [])
        assert [e.name for e in salida.events] == esperado, f"t={t}"
        assert [m.name for m in salida.metrics] == ["framing_score"]
        assert 0.0 <= salida.metrics[0].value <= 1.0
    assert guardia.encuadre_correcto


def test_el_evento_lleva_la_puntuacion_y_el_nombre_del_yaml() -> None:
    guardia = FramingGuardAnalyzer(escena(), name="encuadre", lost_after_s=5, restored_after_s=5)
    guardia.process(fotograma(0, mover(escena(), dx=40)), [], [])
    (evento,) = guardia.process(fotograma(5, mover(escena(), dx=40)), [], []).events
    assert evento.analyzer == "encuadre"
    assert evento.attributes["score"] < guardia.min_similarity


def test_annotate_devuelve_una_copia(guardia: FramingGuardAnalyzer) -> None:
    imagen = escena()
    original = imagen.copy()
    anotada = guardia.annotate(imagen, [])
    assert anotada is not imagen
    assert np.array_equal(imagen, original)


def test_una_referencia_sin_textura_se_rechaza() -> None:
    with pytest.raises(ValueError, match="textura"):
        FramingGuardAnalyzer(np.zeros((360, 640, 3), dtype=np.uint8))


def test_fotogramas_reales_misma_camara_con_otra_luz_y_clima() -> None:
    """Agosto soleado (referencia) contra el fixture lluvioso: mismo encuadre, 0,94 medido."""
    referencia = cv2.imread(str(RAIZ / "projects/01-parking-trafico/reference-frame.jpg"))
    captura = cv2.VideoCapture(str(RAIZ / "tests/fixtures/video/calle-1080p-6s.mp4"))
    ok, lluvia = captura.read()
    captura.release()
    assert ok
    guardia = FramingGuardAnalyzer(referencia)
    assert guardia.puntuar(lluvia) > 0.8
    assert guardia.puntuar(mover(lluvia, dx=40)) < guardia.min_similarity


def test_from_config_lee_la_referencia_y_un_fichero_roto_falla_al_construir(
    tmp_path: Path,
) -> None:
    from cvp.pipeline.config import FramingGuardAnalyzer as Config

    cv2.imwrite(str(tmp_path / "ref.png"), escena())
    cfg = Config(type="framing_guard", reference_image=tmp_path / "ref.png", name="encuadre")
    assert FramingGuardAnalyzer.from_config(cfg).name == "encuadre"

    (tmp_path / "roto.png").write_bytes(b"no es una imagen")
    with pytest.raises(ValueError, match="roto.png"):
        FramingGuardAnalyzer.from_config(
            Config(type="framing_guard", reference_image=tmp_path / "roto.png")
        )
