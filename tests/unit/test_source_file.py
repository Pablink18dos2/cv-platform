"""El connector de fichero de video: muestreo, indices, timestamps y ciclo de vida.

El fixture `calle-10s.mp4` son 10 s reales de la fuente del proyecto 01 a 640x360, con
**exactamente 300 fotogramas**. El numero redondo es deliberado: permite afirmar las cuentas
de muestreo exactas en vez de con tolerancias.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cvp.core import Frame, SourceInfo
from cvp.core.protocols import VideoSource
from cvp.core.registry import SOURCES
from cvp.sources import FileVideoSource, SourceError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "video" / "calle-10s.mp4"
FOTOGRAMAS_DEL_FIXTURE = 300
FPS_DEL_FIXTURE = 30.0
INICIO = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def fuente() -> FileVideoSource:
    origen = FileVideoSource(FIXTURE, target_fps=FPS_DEL_FIXTURE, start_time=INICIO)
    origen.open()
    yield origen
    origen.close()


def _emitidos(target_fps: float, **kwargs: object) -> list[Frame]:
    origen = FileVideoSource(FIXTURE, target_fps=target_fps, start_time=INICIO, **kwargs)
    origen.open()
    try:
        return list(origen.frames())
    finally:
        origen.close()


# --- El fixture -----------------------------------------------------------------------


def test_el_fixture_de_video_existe() -> None:
    # Se comprueba antes de usarlo para que el fallo diga "falta el fixture" en vez de
    # reventar dentro de OpenCV con un mensaje que no explica nada.
    assert FIXTURE.is_file(), f"falta el fixture de video en {FIXTURE}"


def test_el_fixture_tiene_las_dimensiones_y_la_duracion_esperadas(
    fuente: FileVideoSource,
) -> None:
    assert fuente.info == SourceInfo(width=640, height=360, native_fps=30.0, is_live=False)


# --- Contrato y registro --------------------------------------------------------------


def test_cumple_el_protocolo_video_source() -> None:
    assert isinstance(FileVideoSource(FIXTURE, target_fps=5), VideoSource)


def test_esta_registrada_como_file() -> None:
    assert SOURCES.get("file") is FileVideoSource


def test_un_target_fps_no_positivo_falla_al_construir() -> None:
    with pytest.raises(ValueError, match="target_fps"):
        FileVideoSource(FIXTURE, target_fps=0)


# --- Ciclo de vida --------------------------------------------------------------------


def test_pedir_info_antes_de_abrir_falla() -> None:
    with pytest.raises(SourceError, match="open()"):
        _ = FileVideoSource(FIXTURE, target_fps=5).info


def test_leer_fotogramas_antes_de_abrir_falla() -> None:
    with pytest.raises(SourceError, match="open()"):
        FileVideoSource(FIXTURE, target_fps=5).frames()


def test_cerrar_es_idempotente_y_no_exige_haber_abierto() -> None:
    origen = FileVideoSource(FIXTURE, target_fps=5)
    origen.close()
    origen.open()
    origen.close()
    origen.close()


def test_abrir_dos_veces_no_reabre_nada(fuente: FileVideoSource) -> None:
    antes = fuente.info
    fuente.open()
    assert fuente.info == antes


def test_un_iterador_agotado_no_reabre_el_video(fuente: FileVideoSource) -> None:
    assert len(list(fuente.frames())) == FOTOGRAMAS_DEL_FIXTURE
    assert list(fuente.frames()) == []


# --- Errores de apertura --------------------------------------------------------------


def test_una_ruta_que_no_existe_falla_con_la_ruta_en_el_mensaje(tmp_path: Path) -> None:
    ausente = tmp_path / "no-esta.mp4"
    with pytest.raises(SourceError, match="no existe"):
        FileVideoSource(ausente, target_fps=5).open()


def test_un_fichero_que_no_es_video_falla(tmp_path: Path) -> None:
    impostor = tmp_path / "no-soy-un-video.txt"
    impostor.write_text("esto no es un mp4", encoding="utf-8")
    with pytest.raises(SourceError, match="no ha podido abrir"):
        FileVideoSource(impostor, target_fps=5).open()


@pytest.mark.parametrize("fps_leido", [0.0, -1.0, float("nan")])
def test_un_video_sin_frame_rate_falla_en_vez_de_emitirlo_todo(
    monkeypatch: pytest.MonkeyPatch, fps_leido: float
) -> None:
    # Emitirlo todo en silencio convertiria el contrato de muestreo en una mentira:
    # target_fps diria 5 y entregaria 30.
    import cv2

    original = cv2.VideoCapture.get

    def get_trucado(self: object, prop: int) -> float:
        if prop == cv2.CAP_PROP_FPS:
            return fps_leido
        return original(self, prop)

    monkeypatch.setattr(cv2.VideoCapture, "get", get_trucado)
    with pytest.raises(SourceError, match="frame rate"):
        FileVideoSource(FIXTURE, target_fps=5).open()


# --- Muestreo -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target_fps", "esperados"),
    [(30, 300), (15, 150), (10, 100), (7, 70), (5, 50), (1, 10)],
)
def test_el_muestreo_emite_la_cuenta_exacta(target_fps: float, esperados: int) -> None:
    assert len(_emitidos(target_fps)) == esperados


def test_pedir_mas_fps_de_los_que_hay_no_duplica_fotogramas() -> None:
    assert len(_emitidos(60)) == FOTOGRAMAS_DEL_FIXTURE


def test_el_descarte_se_reparte_de_forma_uniforme() -> None:
    # 7 no divide a 30, asi que este es el caso que obliga a repartir en vez de acumular
    # deriva. Un muestreo que emitiera 70 seguidos y descartara 230 cumpliria la cuenta
    # total y seria inservible para el tracker.
    fotogramas = _emitidos(7)
    origen_de_cada_uno = [
        round((f.timestamp - INICIO).total_seconds() * FPS_DEL_FIXTURE) for f in fotogramas
    ]
    huecos = [b - a for a, b in zip(origen_de_cada_uno[:-1], origen_de_cada_uno[1:], strict=True)]
    assert max(huecos) - min(huecos) <= 1


# --- Lo que sale ----------------------------------------------------------------------


def test_los_indices_son_contiguos_y_empiezan_en_cero() -> None:
    # Contrato de #2: el indice cuenta fotogramas *emitidos*, no leidos. Los que el
    # muestreo descarta no consumen indice.
    assert [f.index for f in _emitidos(5)] == list(range(50))


def test_los_timestamps_crecen_y_van_en_utc() -> None:
    fotogramas = _emitidos(5)
    marcas = [f.timestamp for f in fotogramas]
    assert all(m.tzinfo is not None for m in marcas)
    assert all(m.utcoffset() == timedelta(0) for m in marcas)
    assert marcas == sorted(marcas)
    assert len(set(marcas)) == len(marcas)


def test_los_timestamps_van_separados_por_el_periodo_de_muestreo() -> None:
    marcas = [f.timestamp for f in _emitidos(5)]
    huecos = [(b - a).total_seconds() for a, b in zip(marcas[:-1], marcas[1:], strict=True)]
    assert all(abs(h - 0.2) < 0.001 for h in huecos)


def test_dos_recorridos_dan_exactamente_la_misma_secuencia_de_timestamps() -> None:
    # Se derivan de la posicion en el medio, no del reloj de pared. Es lo que permite
    # afirmar sobre ellos en un test.
    assert [f.timestamp for f in _emitidos(5)] == [f.timestamp for f in _emitidos(5)]


def test_sin_start_time_se_usa_la_hora_actual_en_utc() -> None:
    antes = datetime.now(UTC)
    origen = FileVideoSource(FIXTURE, target_fps=1)
    despues = datetime.now(UTC)
    origen.open()
    try:
        primero = next(iter(origen.frames()))
    finally:
        origen.close()
    assert antes <= primero.timestamp <= despues


def test_las_imagenes_son_bgr_uint8_con_las_dimensiones_del_video() -> None:
    imagen = _emitidos(1)[0].image
    assert imagen.shape == (360, 640, 3)
    assert imagen.dtype == np.uint8


# --- Pereza y liberacion --------------------------------------------------------------


class _CapturaFalsa:
    """Doble de `cv2.VideoCapture` que cuenta lecturas y liberaciones."""

    def __init__(self, _ruta: str, total: int = 300) -> None:
        self.lecturas = 0
        self.liberaciones = 0
        self.total = total

    def isOpened(self) -> bool:  # noqa: N802 - la firma la impone OpenCV
        return True

    def get(self, prop: int) -> float:
        import cv2

        return {
            cv2.CAP_PROP_FPS: 30.0,
            cv2.CAP_PROP_FRAME_WIDTH: 640.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 360.0,
        }[prop]

    def set(self, _prop: int, _valor: float) -> bool:
        self.lecturas = 0
        return True

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.lecturas >= self.total:
            return False, None
        self.lecturas += 1
        return True, np.zeros((360, 640, 3), dtype=np.uint8)

    def release(self) -> None:
        self.liberaciones += 1


@pytest.fixture
def captura_falsa(monkeypatch: pytest.MonkeyPatch) -> list[_CapturaFalsa]:
    creadas: list[_CapturaFalsa] = []

    def fabricar(ruta: str) -> _CapturaFalsa:
        captura = _CapturaFalsa(ruta)
        creadas.append(captura)
        return captura

    import cv2

    monkeypatch.setattr(cv2, "VideoCapture", fabricar)
    return creadas


def test_leer_tres_fotogramas_no_decodifica_el_video_entero(
    captura_falsa: list[_CapturaFalsa],
) -> None:
    # `frames()` es un generador: si construyera la lista de 300, un pipeline sobre un
    # directo se comeria la memoria y el retraso.
    origen = FileVideoSource(FIXTURE, target_fps=30, start_time=INICIO)
    origen.open()
    iterador = origen.frames()
    for _ in range(3):
        next(iterador)
    assert captura_falsa[0].lecturas == 3
    origen.close()


def test_close_libera_la_captura_aunque_se_abandone_el_generador_a_medias(
    captura_falsa: list[_CapturaFalsa],
) -> None:
    origen = FileVideoSource(FIXTURE, target_fps=30, start_time=INICIO)
    origen.open()
    for _ in origen.frames():
        break
    origen.close()
    origen.close()
    assert captura_falsa[0].liberaciones == 1


def test_loop_vuelve_a_empezar_sin_reiniciar_indices_ni_timestamps(
    captura_falsa: list[_CapturaFalsa],
) -> None:
    # El indice no se reinicia al dar la vuelta: es el mismo contrato que hace que
    # `every_n_frames` siga significando lo mismo cuando un directo reconecta.
    origen = FileVideoSource(FIXTURE, target_fps=30, loop=True, start_time=INICIO)
    origen.open()
    try:
        fotogramas = [f for f, _ in zip(origen.frames(), range(320), strict=False)]
    finally:
        origen.close()
    assert [f.index for f in fotogramas] == list(range(320))
    marcas = [f.timestamp for f in fotogramas]
    assert marcas == sorted(marcas)
    assert len(set(marcas)) == len(marcas)


# --- Puente con la configuracion ------------------------------------------------------


def test_from_config_construye_desde_el_modelo_de_configuracion() -> None:
    from cvp.pipeline.config import FileSource

    cfg = FileSource(type="file", path=FIXTURE, target_fps=5, loop=True)
    origen = FileVideoSource.from_config(cfg, start_time=INICIO)
    assert origen.path == FIXTURE
    assert origen.target_fps == 5
    assert origen.loop is True
