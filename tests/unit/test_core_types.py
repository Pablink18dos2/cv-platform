"""Los tipos del core: campos, inmutabilidad y lo que rechazan.

Estos tests son el contrato escrito. Si uno de ellos hay que cambiarlo para que pase
otra tarea, lo que hay que replantear es la otra tarea.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

import numpy as np
import pytest

import cvp.core
from cvp.core import (
    AnalyzerOutput,
    BBox,
    Detection,
    Event,
    Frame,
    Metric,
    SourceInfo,
    Track,
)

# Madrid en enero es UTC+1. Se usa ZoneInfo si la maquina tiene base de datos de zonas
# horarias (Linux, y por tanto CI) y un offset fijo equivalente si no la tiene (Windows
# sin el paquete `tzdata`, que esta tarea no autoriza a anadir). Lo que se prueba es la
# normalizacion de una zona que no es UTC, y para eso las dos sirven igual.
try:
    from zoneinfo import ZoneInfo

    MADRID = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover - depende de la maquina, no del codigo
    MADRID = timezone(timedelta(hours=1))

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

NOMBRES_PUBLICOS = {
    # Los ocho tipos
    "BBox",
    "SourceInfo",
    "Frame",
    "Detection",
    "Track",
    "Event",
    "Metric",
    "AnalyzerOutput",
    # Los cinco Protocol
    "VideoSource",
    "Detector",
    "Tracker",
    "Analyzer",
    "Sink",
    # Los cinco decoradores
    "register_source",
    "register_detector",
    "register_tracker",
    "register_analyzer",
    "register_sink",
    # Los cinco registros
    "SOURCES",
    "DETECTORS",
    "TRACKERS",
    "ANALYZERS",
    "SINKS",
    # Las dos excepciones
    "DuplicateRegistrationError",
    "UnknownTypeError",
}

CAMPOS_ESPERADOS = {
    BBox: ["x1", "y1", "x2", "y2"],
    SourceInfo: ["width", "height", "native_fps", "is_live"],
    Frame: ["image", "index", "timestamp"],
    Detection: ["bbox", "class_id", "label", "confidence"],
    Track: ["bbox", "class_id", "label", "confidence", "track_id"],
    Event: ["analyzer", "name", "timestamp", "attributes"],
    Metric: ["analyzer", "name", "value", "timestamp", "labels"],
    AnalyzerOutput: ["events", "metrics"],
}


def imagen(alto: int = 4, ancho: int = 6) -> np.ndarray:
    return np.zeros((alto, ancho, 3), dtype=np.uint8)


def una_deteccion(**cambios: object) -> Detection:
    campos: dict[str, object] = {
        "bbox": BBox(0, 0, 10, 10),
        "class_id": 2,
        "label": "car",
        "confidence": 0.9,
    }
    campos.update(cambios)
    return Detection(**campos)  # type: ignore[arg-type]


def un_ejemplar(tipo: type) -> object:
    """Un ejemplar valido de cada uno de los ocho tipos, para los tests parametrizados."""
    ejemplares = {
        BBox: lambda: BBox(0, 0, 10, 10),
        SourceInfo: lambda: SourceInfo(width=1920, height=1080, native_fps=25.0, is_live=True),
        Frame: lambda: Frame(image=imagen(), index=0, timestamp=AHORA),
        Detection: una_deteccion,
        Track: lambda: Track.from_detection(una_deteccion(), track_id=1),
        Event: lambda: Event(analyzer="traffic_flow", name="line_crossing", timestamp=AHORA),
        Metric: lambda: Metric(analyzer="traffic_flow", name="vehicles", value=3, timestamp=AHORA),
        AnalyzerOutput: AnalyzerOutput.empty,
    }
    return ejemplares[tipo]()


# --- Superficie publica ---------------------------------------------------------------


def test_los_nombres_publicos_del_core_son_exactamente_los_del_contrato() -> None:
    assert set(cvp.core.__all__) == NOMBRES_PUBLICOS


def test_todo_lo_declarado_en_all_se_puede_importar() -> None:
    for nombre in cvp.core.__all__:
        assert hasattr(cvp.core, nombre), nombre


# --- Campos e inmutabilidad -----------------------------------------------------------


@pytest.mark.parametrize(("tipo", "campos"), list(CAMPOS_ESPERADOS.items()))
def test_los_campos_de_cada_tipo_son_exactamente_los_acordados(
    tipo: type, campos: list[str]
) -> None:
    assert [f.name for f in dataclasses.fields(tipo)] == campos


@pytest.mark.parametrize("tipo", list(CAMPOS_ESPERADOS))
def test_los_ocho_tipos_son_inmutables(tipo: type) -> None:
    ejemplar = un_ejemplar(tipo)
    primer_campo = dataclasses.fields(tipo)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ejemplar, primer_campo, None)


@pytest.mark.parametrize("tipo", list(CAMPOS_ESPERADOS))
def test_los_ocho_tipos_usan_slots(tipo: type) -> None:
    ejemplar = un_ejemplar(tipo)
    with pytest.raises(AttributeError):
        object.__setattr__(ejemplar, "campo_inventado", 1)


def test_solo_bbox_se_construye_por_posicion() -> None:
    assert BBox(0, 0, 10, 10).x2 == 10.0
    with pytest.raises(TypeError):
        Detection(BBox(0, 0, 10, 10), 2, "car", 0.9)  # type: ignore[misc]


# --- Timestamps -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "construir",
    [
        lambda ts: Frame(image=imagen(), index=0, timestamp=ts),
        lambda ts: Event(analyzer="a", name="e", timestamp=ts),
        lambda ts: Metric(analyzer="a", name="m", value=1.0, timestamp=ts),
    ],
    ids=["Frame", "Event", "Metric"],
)
def test_un_timestamp_naive_se_rechaza_diciendo_el_campo(construir) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        construir(datetime(2026, 1, 1, 12, 0))


@pytest.mark.parametrize(
    "construir",
    [
        lambda ts: Frame(image=imagen(), index=0, timestamp=ts),
        lambda ts: Event(analyzer="a", name="e", timestamp=ts),
        lambda ts: Metric(analyzer="a", name="m", value=1.0, timestamp=ts),
    ],
    ids=["Frame", "Event", "Metric"],
)
def test_otra_zona_horaria_se_normaliza_a_utc_conservando_el_instante(construir) -> None:
    objeto = construir(datetime(2026, 1, 1, 13, 0, tzinfo=MADRID))
    assert objeto.timestamp == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert objeto.timestamp.tzinfo is UTC
    assert objeto.timestamp.hour == 12


# --- BBox -----------------------------------------------------------------------------


def test_bbox_deriva_medidas_y_puntos() -> None:
    caja = BBox(0, 0, 10, 20)
    assert caja.width == 10.0
    assert caja.height == 20.0
    assert caja.area == 200.0
    assert caja.center == (5.0, 10.0)
    assert caja.bottom_center == (5.0, 20.0)


def test_bbox_de_area_cero_es_valida() -> None:
    assert BBox(5, 5, 5, 20).area == 0.0


@pytest.mark.parametrize(
    "coordenadas",
    [
        (10, 0, 5, 20),
        (0, 10, 20, 5),
        (0, 0, math.nan, 10),
        (0, 0, 10, math.inf),
        (-math.inf, 0, 10, 10),
    ],
)
def test_bbox_rechaza_lo_imposible(coordenadas: tuple[float, float, float, float]) -> None:
    with pytest.raises(ValueError):
        BBox(*coordenadas)


def test_bbox_se_traslada_al_sistema_de_coordenadas_del_fotograma() -> None:
    assert BBox(0, 0, 10, 10).translated(600, 300) == BBox(600, 300, 610, 310)


def test_bbox_se_recorta_a_los_limites_del_fotograma() -> None:
    assert BBox(-5, -5, 20, 20).clipped(10, 10) == BBox(0, 0, 10, 10)


def test_recortar_una_caja_que_no_corta_el_fotograma_es_un_error() -> None:
    with pytest.raises(ValueError, match="no corta el fotograma"):
        BBox(50, 50, 60, 60).clipped(10, 10)


def test_trasladar_y_recortar_devuelven_cajas_nuevas() -> None:
    original = BBox(0, 0, 10, 10)
    assert original.translated(1, 1) is not original
    assert original.clipped(100, 100) == original


# --- SourceInfo -----------------------------------------------------------------------


def test_source_info_admite_que_la_fuente_no_declare_fps() -> None:
    info = SourceInfo(width=1920, height=1080, native_fps=None, is_live=True)
    assert info.native_fps is None


@pytest.mark.parametrize("fps", [0, -1, math.nan])
def test_source_info_rechaza_un_fps_que_no_es_un_ritmo(fps: float) -> None:
    with pytest.raises(ValueError):
        SourceInfo(width=1920, height=1080, native_fps=fps, is_live=False)


# --- Frame ----------------------------------------------------------------------------


def test_frame_no_copia_el_buffer() -> None:
    img = imagen()
    assert Frame(image=img, index=0, timestamp=AHORA).image is img


@pytest.mark.parametrize(
    ("img", "problema"),
    [
        (np.zeros((4, 6), dtype=np.uint8), "3 dimensiones"),
        (np.zeros((4, 6, 4), dtype=np.uint8), "3 canales"),
        (np.zeros((4, 6, 3), dtype=np.float32), "uint8"),
    ],
)
def test_frame_rechaza_un_buffer_que_no_es_bgr_uint8(img: np.ndarray, problema: str) -> None:
    with pytest.raises(ValueError, match=problema):
        Frame(image=img, index=0, timestamp=AHORA)


def test_frame_rechaza_un_indice_negativo() -> None:
    with pytest.raises(ValueError, match="index"):
        Frame(image=imagen(), index=-1, timestamp=AHORA)


def test_frame_compara_por_identidad() -> None:
    img = imagen()
    uno = Frame(image=img, index=0, timestamp=AHORA)
    otro = Frame(image=img, index=0, timestamp=AHORA)
    assert uno == uno
    assert uno != otro
    assert uno in [uno, otro]


# --- Detection y Track ----------------------------------------------------------------


@pytest.mark.parametrize(
    "cambios",
    [
        {"confidence": 1.4},
        {"confidence": -0.1},
        {"confidence": math.nan},
        {"class_id": -1},
        {"label": ""},
    ],
)
def test_detection_rechaza_valores_imposibles(cambios: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        una_deteccion(**cambios)


def test_track_se_construye_desde_una_deteccion_conservando_sus_campos() -> None:
    deteccion = una_deteccion()
    track = Track.from_detection(deteccion, track_id=7)
    assert track.track_id == 7
    assert (track.bbox, track.class_id, track.label, track.confidence) == (
        deteccion.bbox,
        deteccion.class_id,
        deteccion.label,
        deteccion.confidence,
    )


def test_track_no_hereda_de_detection() -> None:
    assert not issubclass(Track, Detection)


def test_track_rechaza_un_id_negativo() -> None:
    with pytest.raises(ValueError, match="track_id"):
        Track.from_detection(una_deteccion(), track_id=-1)


# --- Event y Metric -------------------------------------------------------------------


def test_los_atributos_de_un_evento_son_inmutables_y_copiados() -> None:
    original = {"space": "7", "vehiculos": 3}
    evento = Event(analyzer="parking", name="space_occupied", timestamp=AHORA, attributes=original)

    assert isinstance(evento.attributes, MappingProxyType)
    with pytest.raises(TypeError):
        evento.attributes["space"] = "8"  # type: ignore[index]

    original["space"] = "8"
    assert evento.attributes["space"] == "7"


def test_los_atributos_de_un_evento_estan_vacios_por_defecto() -> None:
    assert Event(analyzer="a", name="e", timestamp=AHORA).attributes == {}


def test_un_evento_rechaza_atributos_que_no_se_pueden_serializar() -> None:
    with pytest.raises(TypeError):
        Event(analyzer="a", name="e", timestamp=AHORA, attributes={"o": object()})


def test_las_etiquetas_de_una_metrica_van_de_str_a_str() -> None:
    metrica = Metric(
        analyzer="traffic_flow",
        name="vehicles_per_minute",
        value=12,
        timestamp=AHORA,
        labels={"direction": "norte"},
    )
    assert isinstance(metrica.labels, MappingProxyType)
    assert metrica.labels["direction"] == "norte"

    with pytest.raises(TypeError):
        Metric(analyzer="a", name="m", value=1, timestamp=AHORA, labels={"direction": 3})


def test_el_valor_de_una_metrica_se_guarda_como_float() -> None:
    metrica = Metric(analyzer="a", name="m", value=3, timestamp=AHORA)
    assert metrica.value == 3.0
    assert isinstance(metrica.value, float)


@pytest.mark.parametrize("valor", [math.nan, math.inf, -math.inf])
def test_una_metrica_no_puede_valer_nan_ni_infinito(valor: float) -> None:
    with pytest.raises(ValueError):
        Metric(analyzer="a", name="m", value=valor, timestamp=AHORA)


def test_eventos_y_metricas_comparan_por_valor() -> None:
    uno = Event(analyzer="a", name="e", timestamp=AHORA, attributes={"x": 1})
    otro = Event(analyzer="a", name="e", timestamp=AHORA, attributes={"x": 1})
    assert uno == otro


# --- AnalyzerOutput -------------------------------------------------------------------


def test_analyzer_output_acepta_cualquier_iterable_y_guarda_tuplas() -> None:
    evento = Event(analyzer="a", name="e", timestamp=AHORA)
    metrica = Metric(analyzer="a", name="m", value=1, timestamp=AHORA)
    salida = AnalyzerOutput(events=[evento], metrics=(m for m in [metrica]))

    assert isinstance(salida.events, tuple)
    assert isinstance(salida.metrics, tuple)
    assert salida.events == (evento,)
    assert salida.metrics == (metrica,)


def test_analyzer_output_vacio() -> None:
    vacio = AnalyzerOutput.empty()
    assert vacio.events == ()
    assert vacio.metrics == ()
