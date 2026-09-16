"""El analizador de aparcamiento, con detecciones sinteticas y timestamps inyectados: sin
video, sin modelo, sin red.

Lo que se juega aqui es la histeresis **en tiempo de video**: un autobus que tapa la fila
durante `to_free_s - 1` segundos no libera nada, un coche que cruza por delante de una
plaza libre durante `to_occupied_s - 1` no la ocupa, y una sola observacion en contra
reinicia la cuenta. Ninguno de estos tests cuenta fotogramas; el de cadencia variable
existe para que no se pueda hacer trampa contandolos.

Convencion: el fotograma es de 1920x1080, las plazas `D01`..`D04` son rectangulos
contiguos en fracciones, `coche(zona)` es una deteccion cuya caja es exactamente el
rectangulo de la plaza (IoU 1,0), y `fotograma(t)` lleva el timestamp `INICIO + t` segundos.
`TO_OCCUPIED_S = 6` y `TO_FREE_S = 10`, como los valores por defecto de `Hysteresis`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cvp.analyzers import ParkingOccupancyAnalyzer
from cvp.analyzers.geometry import Poligono, rasterizar_poligono
from cvp.analyzers.parking import Ocupacion, cargar_zonas
from cvp.core import ANALYZERS, AnalyzerOutput, BBox, Detection, Event, Frame, Metric, Track
from cvp.core.protocols import Analyzer
from cvp.storage import agregar_por_minuto

INICIO = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
ANCHO, ALTO = 1920, 1080
TO_OCCUPIED_S = 6.0
TO_FREE_S = 10.0

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zones"


def cuadrado(id: str, x1: float, y1: float, x2: float, y2: float) -> Poligono:
    return Poligono(id=id, puntos=((x1, y1), (x2, y1), (x2, y2), (x1, y2)))


D01 = cuadrado("d01", 0.10, 0.60, 0.20, 0.80)
D02 = cuadrado("d02", 0.20, 0.60, 0.30, 0.80)
D03 = cuadrado("d03", 0.30, 0.60, 0.40, 0.80)
D04 = cuadrado("d04", 0.40, 0.60, 0.50, 0.80)


def fotograma(t: float, *, indice: int = 0, ancho: int = ANCHO, alto: int = ALTO) -> Frame:
    return Frame(
        image=np.zeros((alto, ancho, 3), dtype=np.uint8),
        index=indice,
        timestamp=INICIO + timedelta(seconds=t),
    )


def caja_de(zona: Poligono, *, escala: float = 1.0, ancho: int = ANCHO, alto: int = ALTO) -> BBox:
    """El rectangulo envolvente de la zona en pixeles, encogido o agrandado desde su centro."""
    x1, y1, x2, y2 = zona.to_pixels(ancho, alto).rectangulo_envolvente
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w, h = (x2 - x1) * escala / 2, (y2 - y1) * escala / 2
    return BBox(cx - w, cy - h, cx + w, cy + h)


def coche(zona: Poligono, *, escala: float = 1.0, confidence: float = 0.6) -> Detection:
    """Una deteccion cuya caja es la de la plaza: IoU 1,0 con `escala=1`."""
    return Detection(
        bbox=caja_de(zona, escala=escala), class_id=2, label="car", confidence=confidence
    )


def analizador(*zonas: Poligono, **kwargs: object) -> ParkingOccupancyAnalyzer:
    argumentos: dict[str, object] = {"to_occupied_s": TO_OCCUPIED_S, "to_free_s": TO_FREE_S}
    argumentos.update(kwargs)
    return ParkingOccupancyAnalyzer(zonas or (D01,), **argumentos)  # type: ignore[arg-type]


Paso = tuple[float, list[Detection]]


def recorrer(a: ParkingOccupancyAnalyzer, pasos: Iterable[Paso]) -> list[AnalyzerOutput]:
    return [a.process(fotograma(t, indice=i), [], dets) for i, (t, dets) in enumerate(pasos)]


def cada(desde: float, hasta: float, paso: float, dets: list[Detection]) -> list[Paso]:
    """Fotogramas a `paso` segundos, de `desde` a `hasta` **inclusive**, todos con `dets`."""
    n = int(round((hasta - desde) / paso))
    return [(desde + i * paso, dets) for i in range(n + 1)]


def eventos(salidas: Iterable[AnalyzerOutput]) -> list[Event]:
    return [e for s in salidas for e in s.events]


def metrica(salida: AnalyzerOutput, nombre: str, **labels: str) -> float:
    (m,) = [m for m in salida.metrics if m.name == nombre and dict(m.labels) == labels]
    return m.value


def arrancar(
    a: ParkingOccupancyAnalyzer, *, ocupadas: Iterable[Poligono] = (), t0: float = 0.0
) -> list[AnalyzerOutput]:
    """Deja determinado el primer estado de todas las plazas: dos fotogramas, en `t0` y
    `t0 + TO_OCCUPIED_S`, con un coche en cada una de `ocupadas`. No sale ningun evento."""
    dets = [coche(z) for z in ocupadas]
    salidas = recorrer(a, [(t0, dets), (t0 + TO_OCCUPIED_S, dets)])
    assert eventos(salidas) == [], "determinar el primer estado no es un cambio"
    return salidas


# --- La histeresis: donde se juega el issue ---------------------------------------------


def test_el_autobus_que_tapa_la_plaza_menos_de_to_free_s_no_la_libera() -> None:
    """Plaza ocupada; la deteccion desaparece durante `to_free_s - 1` segundos y vuelve.
    Cero eventos, y la plaza cuenta como ocupada todo el rato. Es el criterio que da
    nombre al bloque."""
    a = analizador()
    arrancar(a, ocupadas=[D01])
    con = [coche(D01)]
    pasos = cada(7, 10, 1, con) + cada(11, 11 + TO_FREE_S - 1, 1, []) + cada(21, 30, 1, con)
    salidas = recorrer(a, pasos)
    assert eventos(salidas) == []
    assert {metrica(s, "spaces_occupied") for s in salidas} == {1.0}
    assert {metrica(s, "spaces_free") for s in salidas} == {0.0}


def test_el_coche_que_pasa_por_delante_menos_de_to_occupied_s_no_la_ocupa() -> None:
    """Plaza libre; una deteccion aparece durante `to_occupied_s - 1` segundos y se va."""
    a = analizador()
    arrancar(a)
    pasos = (
        cada(7, 9, 1, []) + cada(10, 10 + TO_OCCUPIED_S - 1, 1, [coche(D01)]) + cada(16, 25, 1, [])
    )
    salidas = recorrer(a, pasos)
    assert eventos(salidas) == []
    assert {metrica(s, "spaces_free") for s in salidas} == {1.0}


def test_plaza_libre_con_coche_sigue_libre_hasta_to_occupied_s_y_entonces_un_solo_evento() -> None:
    a = analizador(name="parking")
    arrancar(a)
    salidas = recorrer(a, cada(10, 30, 1, [coche(D01)]))
    # Hasta t = 15 (cinco segundos seguidos) sigue libre; en t = 16 se cruza el umbral.
    assert [metrica(s, "spaces_free") for s in salidas[:6]] == [1.0] * 6
    assert [metrica(s, "spaces_occupied") for s in salidas[6:]] == [1.0] * 15
    (evento,) = eventos(salidas)
    assert evento.name == "space_occupied"
    assert evento.analyzer == "parking"
    assert dict(evento.attributes) == {"zone": "d01"}
    assert evento.timestamp == INICIO + timedelta(seconds=16)


def test_plaza_ocupada_sin_coche_sigue_ocupada_hasta_to_free_s_y_entonces_un_solo_evento() -> None:
    a = analizador()
    arrancar(a, ocupadas=[D01])
    salidas = recorrer(a, cada(10, 30, 1, []))
    assert [metrica(s, "spaces_occupied") for s in salidas[:10]] == [1.0] * 10
    assert [metrica(s, "spaces_free") for s in salidas[10:]] == [1.0] * 11
    (evento,) = eventos(salidas)
    assert evento.name == "space_freed"
    assert dict(evento.attributes) == {"zone": "d01"}
    assert evento.timestamp == INICIO + timedelta(seconds=20)


def test_una_interrupcion_de_un_solo_fotograma_reinicia_la_cuenta() -> None:
    """Plaza libre, coche desde t = 10, **un** fotograma sin el en t = 14, coche otra vez
    desde t = 15. El evento llega en t = 21 (`to_occupied_s` despues de la interrupcion),
    no en t = 16."""
    a = analizador()
    arrancar(a)
    con = [coche(D01)]
    salidas = recorrer(a, cada(10, 13, 1, con) + [(14.0, [])] + cada(15, 25, 1, con))
    (evento,) = eventos(salidas)
    assert evento.name == "space_occupied"
    assert evento.timestamp == INICIO + timedelta(seconds=21)
    # Y la misma interrupcion, al liberar: sin coche desde t = 30, un fotograma con el en
    # t = 35, sin el desde t = 36. Se libera en t = 46, no en t = 40.
    salidas = recorrer(a, cada(30, 34, 1, []) + [(35.0, con)] + cada(36, 50, 1, []))
    (evento,) = eventos(salidas)
    assert evento.name == "space_freed"
    assert evento.timestamp == INICIO + timedelta(seconds=46)


def test_la_histeresis_se_cuenta_en_segundos_de_video_y_no_en_fotogramas() -> None:
    """Coche desde t = 10: diez fotogramas a 0,2 s (1,8 s de video) no ocupan nada, y tres
    mas a 2 s si, en t = 16 exacto. Y al reves para liberar: cinco fotogramas a 2 s (8 s)
    no liberan, y los siguientes a 0,2 s si, en t = 40 exacto. Con cualquier cuenta de
    fotogramas una de las dos mitades saldria mal."""
    a = analizador()
    arrancar(a)
    con = [coche(D01)]
    densos = recorrer(a, cada(10.0, 11.8, 0.2, con))
    assert eventos(densos) == []
    assert len(densos) == 10
    espaciados = recorrer(a, [(12.0, con), (14.0, con), (16.0, con)])
    (evento,) = eventos(espaciados)
    assert evento.name == "space_occupied"
    assert evento.timestamp == INICIO + timedelta(seconds=16)

    espaciados = recorrer(a, cada(30.0, 38.0, 2.0, []))
    assert eventos(espaciados) == []
    assert len(espaciados) == 5
    densos = recorrer(a, cada(38.2, 40.0, 0.2, []))
    (evento,) = eventos(densos)
    assert evento.name == "space_freed"
    assert evento.timestamp == INICIO + timedelta(seconds=40)


def test_ocupar_y_liberar_son_dos_eventos_y_los_umbrales_son_asimetricos() -> None:
    a = analizador()
    arrancar(a)
    salidas = recorrer(a, cada(10, 20, 1, [coche(D01)]) + cada(21, 40, 1, []))
    assert [(e.name, e.timestamp) for e in eventos(salidas)] == [
        ("space_occupied", INICIO + timedelta(seconds=16)),
        ("space_freed", INICIO + timedelta(seconds=31)),
    ]


# --- El primer estado: "todavia no se" ---------------------------------------------------


def test_al_arrancar_una_plaza_ya_ocupada_no_genera_space_occupied_ni_cuenta_como_libre() -> None:
    a = analizador(D01, D02)
    salidas = recorrer(a, cada(0, 8, 1, [coche(D01)]))
    assert eventos(salidas) == [], "lo que ya estaba ocupado al arrancar no es un cambio"
    # Hasta t = 5 no se sabe nada de ninguna de las dos: ni libres ni ocupadas, pero el
    # total ya dice cuantas se vigilan.
    for s in salidas[:6]:
        assert metrica(s, "spaces_free") == 0.0
        assert metrica(s, "spaces_occupied") == 0.0
        assert metrica(s, "spaces_total") == 2.0
        assert [m for m in s.metrics if m.name == "zone_occupied"] == []
    # En t = 6 se determinan las dos a la vez.
    for s in salidas[6:]:
        assert metrica(s, "spaces_occupied") == 1.0
        assert metrica(s, "spaces_free") == 1.0
        assert metrica(s, "zone_occupied", zone="d01") == 1.0
        assert metrica(s, "zone_occupied", zone="d02") == 0.0
    assert a.estado_de("d01") is Ocupacion.OCUPADA
    assert a.estado_de("d02") is Ocupacion.LIBRE


def test_el_primer_estado_se_determina_tras_to_occupied_s_tambien_para_una_plaza_vacia() -> None:
    """`to_free_s` protege una plaza que se dio por ocupada; al arrancar no hay nada que
    proteger, y esperar 10 s en vez de 6 solo retrasa el dato."""
    a = analizador()
    salidas = recorrer(a, cada(0, 10, 1, []))
    assert [metrica(s, "spaces_free") for s in salidas] == [0.0] * 6 + [1.0] * 5
    assert eventos(salidas) == []


def test_una_observacion_en_contra_antes_de_determinar_el_primer_estado_reinicia_la_cuenta() -> (
    None
):
    """Coche desde t = 0, un fotograma sin el en t = 5, coche otra vez desde t = 6: en
    t = 11 todavia no se sabe nada, y en t = 12 se da por ocupada, sin evento."""
    a = analizador()
    con = [coche(D01)]
    salidas = recorrer(a, cada(0, 4, 1, con) + [(5.0, [])] + cada(6, 11, 1, con))
    assert a.estado_de("d01") is Ocupacion.DESCONOCIDA
    assert [metrica(s, "spaces_occupied") for s in salidas] == [0.0] * 12
    salidas = recorrer(a, [(12.0, con)])
    assert a.estado_de("d01") is Ocupacion.OCUPADA
    assert metrica(salidas[0], "spaces_occupied") == 1.0
    assert eventos(salidas) == []


def test_antes_del_primer_fotograma_todas_las_plazas_estan_en_desconocida() -> None:
    a = analizador(D01, D02)
    assert a.estado_de("d01") is Ocupacion.DESCONOCIDA
    assert a.estado_de("d02") is Ocupacion.DESCONOCIDA
    with pytest.raises(KeyError):
        a.estado_de("d99")


# --- La asignacion: una deteccion, una plaza ----------------------------------------------


def test_una_deteccion_con_iou_por_encima_del_umbral_ocupa_la_plaza() -> None:
    """Un coche bien aparcado da 0,5-0,7 con su plaza. Aqui, una caja al 75 % del tamano
    de la plaza, centrada: IoU ~0,56."""
    a = analizador(occupied_iou=0.35)
    arrancar(a)
    mascara = rasterizar_poligono(D01.to_pixels(ANCHO, ALTO), ANCHO, ALTO)
    assert 0.5 < mascara.iou_con(caja_de(D01, escala=0.75)) < 0.6
    salidas = recorrer(a, cada(10, 16, 1, [coche(D01, escala=0.75)]))
    assert [e.name for e in eventos(salidas)] == ["space_occupied"]


def test_una_deteccion_que_no_supera_el_umbral_con_ninguna_plaza_no_ocupa_nada() -> None:
    """Una caja pequena dentro de la plaza (una moto, o un trozo de coche): IoU ~0,16."""
    a = analizador(occupied_iou=0.35)
    arrancar(a)
    mascara = rasterizar_poligono(D01.to_pixels(ANCHO, ALTO), ANCHO, ALTO)
    assert mascara.iou_con(caja_de(D01, escala=0.4)) < 0.35
    salidas = recorrer(a, cada(10, 30, 1, [coche(D01, escala=0.4)]))
    assert eventos(salidas) == []
    assert {metrica(s, "spaces_free") for s in salidas} == {1.0}


def test_una_deteccion_a_caballo_de_dos_plazas_ocupa_solo_la_de_mayor_iou() -> None:
    """Un todoterreno que pasa el umbral con **dos** plazas: la caja cubre `D01` entera y
    el 80 % de `D02`. Las dos superan el umbral y solo se ocupa `D01`."""
    a = analizador(D01, D02, occupied_iou=0.35)
    arrancar(a)
    x1, y1, _, y2 = D01.to_pixels(ANCHO, ALTO).rectangulo_envolvente
    x1_d02, _, x2_d02, _ = D02.to_pixels(ANCHO, ALTO).rectangulo_envolvente
    grande = BBox(x1, y1, x1_d02 + 0.8 * (x2_d02 - x1_d02), y2)
    iou_d01 = rasterizar_poligono(D01.to_pixels(ANCHO, ALTO), ANCHO, ALTO).iou_con(grande)
    iou_d02 = rasterizar_poligono(D02.to_pixels(ANCHO, ALTO), ANCHO, ALTO).iou_con(grande)
    assert iou_d01 > 0.35 and iou_d02 > 0.35, "el test solo vale si las dos superan el umbral"
    assert iou_d01 > iou_d02

    todoterreno = Detection(bbox=grande, class_id=2, label="car", confidence=0.7)
    salidas = recorrer(a, cada(10, 30, 1, [todoterreno]))
    assert [(e.name, e.attributes["zone"]) for e in eventos(salidas)] == [("space_occupied", "d01")]
    assert metrica(salidas[-1], "spaces_occupied") == 1.0
    assert metrica(salidas[-1], "spaces_free") == 1.0


def test_dos_detecciones_en_la_misma_plaza_la_ocupan_una_vez() -> None:
    a = analizador(D01, D02)
    arrancar(a)
    dos = [coche(D01), coche(D01, escala=0.8, confidence=0.4)]
    salidas = recorrer(a, cada(10, 30, 1, dos))
    assert [(e.name, e.attributes["zone"]) for e in eventos(salidas)] == [("space_occupied", "d01")]
    assert metrica(salidas[-1], "spaces_occupied") == 1.0
    assert metrica(salidas[-1], "spaces_free") == 1.0
    assert metrica(salidas[-1], "spaces_total") == 2.0


def test_los_tracks_se_ignoran_y_las_detecciones_deciden() -> None:
    """La decision 1 del issue, fijada: con tracks llenos y detecciones vacias no sale
    nada; con tracks vacios y detecciones llenas, la ocupacion sale."""
    con_tracks = analizador()
    arrancar(con_tracks)
    tracks = [Track.from_detection(coche(D01), track_id=7)]
    salidas = [
        con_tracks.process(fotograma(t, indice=i), tracks, []) for i, t in enumerate(range(10, 31))
    ]
    assert eventos(salidas) == []
    assert {metrica(s, "spaces_free") for s in salidas} == {1.0}

    sin_tracks = analizador()
    arrancar(sin_tracks)
    salidas = [
        sin_tracks.process(fotograma(t, indice=i), [], [coche(D01)])
        for i, t in enumerate(range(10, 31))
    ]
    assert [e.name for e in eventos(salidas)] == ["space_occupied"]
    assert metrica(salidas[-1], "spaces_occupied") == 1.0


def test_cada_plaza_lleva_su_propio_estado_y_no_se_mezclan() -> None:
    a = analizador(D01, D02, D03)
    arrancar(a, ocupadas=[D03])
    pasos = cada(10, 30, 1, [coche(D01)])  # d01 se ocupa, d03 se libera, d02 no cambia
    salidas = recorrer(a, pasos)
    assert sorted((e.name, e.attributes["zone"]) for e in eventos(salidas)) == [
        ("space_freed", "d03"),
        ("space_occupied", "d01"),
    ]
    ultimo = salidas[-1]
    assert (metrica(ultimo, "spaces_free"), metrica(ultimo, "spaces_occupied")) == (2.0, 1.0)
    assert metrica(ultimo, "zone_occupied", zone="d01") == 1.0
    assert metrica(ultimo, "zone_occupied", zone="d02") == 0.0
    assert metrica(ultimo, "zone_occupied", zone="d03") == 0.0


# --- Contrato, registro y from_config -----------------------------------------------------


def test_cumple_el_protocolo_analyzer_y_esta_registrado_como_parking_occupancy() -> None:
    assert isinstance(analizador(), Analyzer)
    assert ANALYZERS.get("parking_occupancy") is ParkingOccupancyAnalyzer


def test_from_config_carga_y_valida_zones_json_y_toma_el_resto_del_yaml() -> None:
    from cvp.pipeline.config import ParkingOccupancyAnalyzer as Cfg

    cfg = Cfg.model_validate(
        {
            "type": "parking_occupancy",
            "name": "plazas",
            "every_n_frames": 10,
            "zones_file": str(FIXTURES / "valido.json"),
            "occupied_iou": 0.4,
            "hysteresis": {"to_occupied_s": 3, "to_free_s": 7},
        }
    )
    construido = ParkingOccupancyAnalyzer.from_config(cfg)
    assert construido.name == "plazas"
    assert construido.every_n_frames == 10
    assert construido.occupied_iou == 0.4
    assert (construido.to_occupied_s, construido.to_free_s) == (3.0, 7.0)
    assert [z.id for z in construido.zones] == ["d01", "d02", "d03"]
    assert construido.zones[0].puntos == ((0.1, 0.6), (0.2, 0.6), (0.2, 0.8), (0.1, 0.8))
    # Sin `name` ni histeresis en el YAML: el `type` y los valores por defecto de `Hysteresis`.
    minimo = Cfg.model_validate(
        {"type": "parking_occupancy", "zones_file": str(FIXTURES / "valido.json")}
    )
    por_defecto = ParkingOccupancyAnalyzer.from_config(minimo)
    assert por_defecto.name == "parking_occupancy"
    assert (por_defecto.occupied_iou, por_defecto.to_occupied_s, por_defecto.to_free_s) == (
        0.35,
        6.0,
        10.0,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": " "},
        {"every_n_frames": 0},
        {"occupied_iou": 0.0},
        {"occupied_iou": 1.5},
        {"to_occupied_s": 0.0},
        {"to_free_s": -1.0},
    ],
    ids=[
        "name-vacio",
        "cadencia-cero",
        "iou-cero",
        "iou-mayor-que-1",
        "ocupar-cero",
        "liberar-negativo",
    ],
)
def test_un_parametro_imposible_falla_al_construir(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        analizador(**kwargs)


def test_sin_zonas_o_con_ids_repetidos_falla_al_construir() -> None:
    with pytest.raises(ValueError, match="plazas"):
        ParkingOccupancyAnalyzer([])
    with pytest.raises(ValueError, match="d01"):
        ParkingOccupancyAnalyzer([D01, D01])


# --- zones.json: se valida al construir, con la zona y el campo -------------------------


def test_un_zones_json_valido_carga_sus_plazas_en_orden() -> None:
    zonas = cargar_zonas(FIXTURES / "valido.json")
    assert [z.id for z in zonas] == ["d01", "d02", "d03"]
    assert all(isinstance(z, Poligono) for z in zonas)
    assert len(zonas[2].puntos) == 4


@pytest.mark.parametrize(
    ("fichero", "esperado"),
    [
        ("roto-sin-zones.json", ["falta la clave 'zones'"]),
        ("roto-vacio.json", ["'zones' esta vacia", "sin plazas"]),
        ("roto-poligono-dos-puntos.json", ["zones[1]", "'d02'", "polygon", "3 puntos", "tiene 2"]),
        (
            "roto-coordenada-fuera.json",
            ["zones[0]", "'d01'", "polygon", "punto 2", "1.2", "fracciones 0-1"],
        ),
        ("roto-id-repetido.json", ["zones[2]", "'d01'", "repetido", "zones[0]"]),
        ("roto-id-vacio.json", ["zones[1]", "'id'", "vacio"]),
        ("roto-json-invalido.json", ["JSON no es valido", "linea 3"]),
    ],
    ids=[
        "sin-zones",
        "lista-vacia",
        "poligono-dos-puntos",
        "coordenada-fuera",
        "id-repetido",
        "id-vacio",
        "json-invalido",
    ],
)
def test_un_zones_json_roto_falla_al_cargar_diciendo_que_zona_y_que_campo(
    fichero: str, esperado: list[str]
) -> None:
    ruta = FIXTURES / fichero
    with pytest.raises(ValueError) as excinfo:
        cargar_zonas(ruta)
    mensaje = str(excinfo.value)
    assert str(ruta) in mensaje, "el mensaje dice que fichero es"
    for trozo in esperado:
        assert trozo in mensaje, f"falta {trozo!r} en: {mensaje}"


def test_un_zones_json_roto_falla_en_from_config_y_no_al_primer_fotograma() -> None:
    from cvp.pipeline.config import ParkingOccupancyAnalyzer as Cfg

    cfg = Cfg.model_validate(
        {"type": "parking_occupancy", "zones_file": str(FIXTURES / "roto-vacio.json")}
    )
    with pytest.raises(ValueError, match="sin plazas"):
        ParkingOccupancyAnalyzer.from_config(cfg)


@pytest.mark.parametrize(
    ("contenido", "esperado"),
    [
        ("[]", "objeto"),
        ('{"zones": {"d01": []}}', "lista"),
        ('{"zones": [42]}', "zones[0]"),
        ('{"zones": [{"polygon": [[0, 0], [1, 0], [1, 1]]}]}', "falta 'id'"),
        ('{"zones": [{"id": "a"}]}', "falta 'polygon'"),
        ('{"zones": [{"id": "a", "polygon": [[0, 0], [1, 0], [1]]}]}', "pares [x, y]"),
        ('{"zones": [{"id": "a", "polygon": [[0, 0], [1, 0], ["x", 1]]}]}', "pares [x, y]"),
        ('{"zones": [{"id": "a", "polygon": [[0, 0], [0.5, 0.5], [1, 1]]}]}', "area cero"),
    ],
    ids=[
        "raiz-lista",
        "zones-objeto",
        "zona-no-objeto",
        "sin-id",
        "sin-polygon",
        "punto-corto",
        "punto-texto",
        "colineal",
    ],
)
def test_otras_formas_de_romper_zones_json_tambien_fallan_con_su_campo(
    tmp_path: Path, contenido: str, esperado: str
) -> None:
    ruta = tmp_path / "zones.json"
    ruta.write_text(contenido, encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(esperado)):
        cargar_zonas(ruta)


def test_un_zones_json_que_no_existe_falla_diciendo_la_ruta(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no se puede leer"):
        cargar_zonas(tmp_path / "no-existe.json")


# --- Lo que sale -----------------------------------------------------------------------------


def test_cada_process_emite_las_tres_metricas_globales_y_una_por_plaza_conocida() -> None:
    a = analizador(D01, D02, D03, name="plazas")
    arrancar(a, ocupadas=[D02])
    (salida,) = recorrer(a, [(10.0, [coche(D02)])])
    nombres = sorted(m.name for m in salida.metrics)
    assert nombres == sorted(
        ["spaces_free", "spaces_occupied", "spaces_total"] + ["zone_occupied"] * 3
    )
    assert metrica(salida, "spaces_total") == 3.0
    assert metrica(salida, "spaces_free") == 2.0
    assert metrica(salida, "spaces_occupied") == 1.0
    por_plaza = {m.labels["zone"]: m.value for m in salida.metrics if m.name == "zone_occupied"}
    assert por_plaza == {"d01": 0.0, "d02": 1.0, "d03": 0.0}
    assert {m.analyzer for m in salida.metrics} == {"plazas"}
    assert {m.timestamp for m in salida.metrics} == {INICIO + timedelta(seconds=10)}
    assert all(dict(m.labels) == {} for m in salida.metrics if m.name.startswith("spaces_"))


def test_todos_los_timestamps_salen_de_frame_timestamp_nunca_del_reloj() -> None:
    marca = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)  # un instante que no es "ahora"
    a = analizador()
    arrancar(a)
    salida = a.process(
        Frame(image=np.zeros((ALTO, ANCHO, 3), np.uint8), index=5, timestamp=marca),
        [],
        [coche(D01)],
    )
    despues = a.process(
        Frame(
            image=np.zeros((ALTO, ANCHO, 3), np.uint8),
            index=6,
            timestamp=marca + timedelta(seconds=TO_OCCUPIED_S),
        ),
        [],
        [coche(D01)],
    )
    assert salida.metrics and despues.events
    for m in salida.metrics:
        assert m.timestamp == marca
    for cosa in (*despues.events, *despues.metrics):
        assert cosa.timestamp == marca + timedelta(seconds=TO_OCCUPIED_S)


def test_el_analyzer_de_eventos_y_metricas_es_el_name_no_el_type() -> None:
    a = analizador(name="plazas-derecha")
    arrancar(a)
    salidas = recorrer(a, cada(10, 16, 1, [coche(D01)]))
    todo = [x for s in salidas for x in (*s.events, *s.metrics)]
    assert todo and {x.analyzer for x in todo} == {"plazas-derecha"}


def test_zone_occupied_agregada_por_minuto_es_la_fraccion_del_minuto_ocupada() -> None:
    """Es lo que permite a #13 pintar la ocupacion plaza a plaza desde `metric_minute`:
    la media ponderada por tiempo de un 0|1 es la fraccion del tiempo en 1."""
    a = analizador(D01, D02, D03)
    arrancar(a, ocupadas=[D02, D03], t0=-30.0)  # determinadas antes del minuto
    # Durante el minuto 10:00: d01 libre todo el rato, d02 ocupada todo el rato, y d03 se
    # queda sin coche en t = 20 y se libera en t = 30.
    pasos = [(float(t), [coche(D02)] + ([coche(D03)] if t < 20 else [])) for t in range(0, 60, 2)]
    salidas = recorrer(a, pasos)
    metricas = [m for s in salidas for m in s.metrics if m.name == "zone_occupied"]
    medias = {
        clave.labels: agregado.mean_value
        for clave, agregado in agregar_por_minuto(metricas).items()
        if clave.minute == INICIO
    }
    assert medias['{"zone":"d01"}'] == 0.0
    assert medias['{"zone":"d02"}'] == 1.0
    assert 0.35 < medias['{"zone":"d03"}'] < 0.65


def test_la_salida_es_un_analyzeroutput_con_tipos_del_core() -> None:
    salida = analizador().process(fotograma(0), [], [coche(D01)])
    assert isinstance(salida, AnalyzerOutput)
    assert all(isinstance(m, Metric) for m in salida.metrics)
    assert all(isinstance(e, Event) for e in salida.events)


def test_el_docstring_deja_escrito_lo_que_se_sabe_de_la_escena_y_por_que_ignora_los_tracks() -> (
    None
):
    documentacion = " ".join((ParkingOccupancyAnalyzer.__doc__ or "").split()).lower()
    assert "fondo" in documentacion and "no se vigilan" in documentacion
    assert "aproximacion" in documentacion
    assert "ignora los tracks" in documentacion
    assert "descarta" in documentacion


# --- La geometria se resuelve en el primer process(), no en la construccion --------------


def test_dos_analizadores_identicos_a_1080p_y_a_720p_dan_los_mismos_eventos() -> None:
    a_1080, a_720 = analizador(), analizador()
    pasos_1080 = cada(0, 12, 1, [coche(D01)])
    salidas_1080 = recorrer(a_1080, pasos_1080)
    detecciones_720 = [
        Detection(bbox=caja_de(D01, ancho=1280, alto=720), class_id=2, label="car", confidence=0.6)
    ]
    salidas_720 = [
        a_720.process(fotograma(t, indice=i, ancho=1280, alto=720), [], detecciones_720)
        for i, t in enumerate(range(0, 13))
    ]
    assert eventos(salidas_1080) == eventos(salidas_720) == []
    assert [metrica(s, "spaces_occupied") for s in salidas_1080] == [
        metrica(s, "spaces_occupied") for s in salidas_720
    ]
    assert a_1080.resolucion == (1920, 1080) and a_720.resolucion == (1280, 720)


# --- annotate() ---------------------------------------------------------------------------


def dominante(pixel: np.ndarray) -> str:
    b, g, r = (int(c) for c in pixel)
    if b == g == r:
        return "gris" if b > 0 else "negro"
    if g >= r and g >= b:
        return "verde"
    if r > g and g > b + 20:
        return "ambar"
    if r > g:
        return "rojo"
    return "otro"


def centroide(zona: Poligono, ancho: int = ANCHO, alto: int = ALTO) -> tuple[int, int]:
    x, y = zona.to_pixels(ancho, alto).centroide
    return (int(round(x)), int(round(y)))


def test_annotate_devuelve_un_array_nuevo_y_deja_el_original_intacto_byte_a_byte() -> None:
    a = analizador(D01, D02)
    arrancar(a, ocupadas=[D02])
    imagen = np.random.default_rng(0).integers(0, 255, (ALTO, ANCHO, 3), dtype=np.uint8)
    antes = imagen.copy()

    anotada = a.annotate(imagen, [])

    assert anotada is not imagen
    assert not np.shares_memory(anotada, imagen)
    assert np.array_equal(imagen, antes)
    assert anotada.shape == imagen.shape and anotada.dtype == np.uint8
    assert not np.array_equal(anotada, imagen), "algo se tiene que haber dibujado"


def test_annotate_pinta_cada_plaza_del_color_de_su_estado() -> None:
    """Verde libre, rojo ocupada, ambar mientras espera la histeresis, gris "todavia no se".
    `D03` alterna en los primeros fotogramas y por eso sigue sin determinarse cuando las
    otras ya lo estan; a `D04` le acaba de llegar un coche."""
    a = analizador(D01, D02, D03, D04)
    recorrer(
        a,
        [
            (0.0, [coche(D02), coche(D03)]),
            (3.0, [coche(D02)]),
            (6.0, [coche(D02), coche(D03)]),
            (7.0, [coche(D02), coche(D03), coche(D04)]),
        ],
    )
    assert a.estado_de("d01") is Ocupacion.LIBRE
    assert a.estado_de("d02") is Ocupacion.OCUPADA
    assert a.estado_de("d03") is Ocupacion.DESCONOCIDA
    assert a.estado_de("d04") is Ocupacion.LIBRE and a.en_espera("d04")

    anotada = a.annotate(np.zeros((ALTO, ANCHO, 3), dtype=np.uint8), [])
    colores = {
        z.id: dominante(anotada[centroide(z)[1], centroide(z)[0]]) for z in (D01, D02, D03, D04)
    }
    assert colores == {"d01": "verde", "d02": "rojo", "d03": "gris", "d04": "ambar"}


def test_annotate_escribe_el_id_de_cada_plaza_y_el_resumen_n_libres_de_m_en_una_esquina() -> None:
    a = analizador(D01, D02, D03)
    arrancar(a, ocupadas=[D02])
    assert a.resumen() == "2 libres de 3"
    anotada = a.annotate(np.zeros((ALTO, ANCHO, 3), dtype=np.uint8), [])
    # El resumen va arriba a la izquierda, fuera de las plazas (que empiezan en y = 0,6).
    assert anotada[:60, :400].any(), "arriba a la izquierda tiene que haber texto"
    # Y algo escrito dentro del rectangulo de cada plaza que no sea solo el relleno: la
    # etiqueta con el id tiene pixeles del color de fondo de la etiqueta, distintos del
    # relleno semitransparente.
    for zona in (D01, D02, D03):
        x1, y1, x2, y2 = (int(v) for v in zona.to_pixels(ANCHO, ALTO).rectangulo_envolvente)
        recorte = anotada[y1 + 6 : y2 - 6, x1 + 6 : x2 - 6]  # sin el borde ni su antialias
        colores = {tuple(int(c) for c in p) for p in recorte.reshape(-1, 3)}
        assert len(colores) > 2, f"en {zona.id} solo hay relleno, falta la etiqueta"


def test_annotate_funciona_antes_del_primer_process_y_con_otra_resolucion() -> None:
    """Antes de ver nada todo es gris, y `annotate` convierte con la forma de la imagen que
    recibe sin tocar la geometria de `process`."""
    a = analizador(D01, D02)
    anotada = a.annotate(np.zeros((360, 640, 3), dtype=np.uint8), [])
    for zona in (D01, D02):
        x, y = centroide(zona, 640, 360)
        assert dominante(anotada[y, x]) == "gris"
    assert a.resolucion is None
    assert a.resumen() == "0 libres de 2"


# --- Con video real ---------------------------------------------------------------------

FIXTURE_1080P = Path(__file__).resolve().parents[1] / "fixtures" / "video" / "calle-1080p-6s.mp4"
# Las tres bahias en bateria del lado derecho, las del primer plano, donde `fuente.md` dice
# que la deteccion es fiable. Colocadas con `scripts/render_parking_frame.py`.
BAHIAS_1080P = FIXTURES / "calle-1080p-bahias.json"


@pytest.mark.slow
def test_sobre_video_real_las_tres_plazas_del_primer_plano_acaban_ocupadas() -> None:
    """Con el detector de verdad y `to_occupied_s` corto para que quepa en 6 s: las tres
    bahias tienen coche todo el rato, asi que acaban **ocupadas**, `spaces_total` es 3 en
    cada fotograma, y —porque estaban ocupadas desde el primer fotograma— **no sale ningun
    evento**: determinar el primer estado no es un `space_occupied`.

    Sin tracker a proposito: este analizador no lo necesita, y con el umbral por defecto
    del tracker se quedaria sin ver casi la mitad de los coches (#7).
    """
    from cvp.detectors import YoloDetector
    from cvp.sources import FileVideoSource

    detector = YoloDetector(
        "yolo11n.pt", imgsz=640, conf=0.35, classes=["car", "motorcycle", "bus", "truck"]
    )
    a = ParkingOccupancyAnalyzer(
        cargar_zonas(BAHIAS_1080P),
        name="plazas",
        occupied_iou=0.35,
        to_occupied_s=2.0,
        to_free_s=4.0,
    )
    origen = FileVideoSource(FIXTURE_1080P, target_fps=5, start_time=INICIO)
    origen.open()
    try:
        salidas: list[tuple[Frame, AnalyzerOutput]] = []
        for fotograma_real in origen.frames():
            detecciones = detector.detect(fotograma_real)
            salidas.append((fotograma_real, a.process(fotograma_real, [], detecciones)))
    finally:
        origen.close()

    assert len(salidas) >= 25, "6 s a 5 fps deberian dar unos 30 fotogramas"
    assert [e for _, s in salidas for e in s.events] == []
    assert {metrica(s, "spaces_total") for _, s in salidas} == {3.0}
    assert {m.analyzer for _, s in salidas for m in s.metrics} == {"plazas"}
    limite = INICIO + timedelta(seconds=2.0)
    for f, s in salidas:
        esperado = 3.0 if f.timestamp >= limite else 0.0
        assert metrica(s, "spaces_occupied") == esperado, f"en {f.timestamp}"
        assert metrica(s, "spaces_free") == 0.0
    assert a.resumen() == "0 libres de 3"
    assert all(a.estado_de(z.id) is Ocupacion.OCUPADA for z in a.zones)
    # Y la anotacion sobre el fotograma real pinta las tres en rojo. La comprobacion visual
    # es `scripts/render_parking_frame.py`; esto solo fija que el color es el del estado.
    ultimo = salidas[-1][0]
    anotada = a.annotate(ultimo.image, [])
    for zona in a.zones:
        x, y = centroide(zona)
        antes, despues = ultimo.image[y, x].astype(int), anotada[y, x].astype(int)
        assert despues[2] - antes[2] > despues[1] - antes[1], f"{zona.id} tendria que ser roja"
