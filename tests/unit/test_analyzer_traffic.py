"""El analizador de trafico, con trayectorias sinteticas de `Track`: sin video, sin modelo,
sin red.

Cada caso de cruce es un fallo distinto que este analizador podria tener sin que nadie lo
viera mirando la pantalla, porque cada cruce falso parece legitimo por separado. El mas
probable es el del coche aparcado con la caja temblando sobre la linea, y por eso el test
que fija las dos cifras (cero con `min_frames_on_side = 2`, 19 con `= 1`) va el primero.

Convencion de todos los tests: el fotograma es de 1920x1080 salvo que se diga, la linea
`HORIZONTAL` corta el fotograma por la mitad y esta escrita de izquierda a derecha, asi
que **abajo** (y > 0,5) es el lado positivo, `"sur"`, y **arriba** el negativo, `"norte"`.
`track(x, y)` construye un track cuyo `bbox.bottom_center` es exactamente `(x, y)`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cvp.analyzers import TrafficFlowAnalyzer
from cvp.analyzers.geometry import LineaDeConteo, lado_de
from cvp.core import ANALYZERS, AnalyzerOutput, BBox, Event, Frame, Metric, Track
from cvp.core.protocols import Analyzer
from cvp.storage import agregar_por_minuto

INICIO = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)
ANCHO, ALTO = 1920, 1080

HORIZONTAL = LineaDeConteo(
    id="principal",
    p1=(0.0, 0.5),
    p2=(1.0, 0.5),
    positive_direction="sur",
    negative_direction="norte",
)
VERTICAL = LineaDeConteo(
    id="lateral",
    p1=(0.5, 0.0),
    p2=(0.5, 1.0),
    positive_direction="oeste",
    negative_direction="este",
)
DIAGONAL = LineaDeConteo(
    id="diagonal",
    p1=(0.1, 0.9),
    p2=(0.9, 0.1),
    positive_direction="abajo-derecha",
    negative_direction="arriba-izquierda",
)


def fotograma(
    indice: int,
    *,
    ancho: int = ANCHO,
    alto: int = ALTO,
    timestamp: datetime | None = None,
) -> Frame:
    return Frame(
        image=np.zeros((alto, ancho, 3), dtype=np.uint8),
        index=indice,
        timestamp=timestamp if timestamp is not None else INICIO + timedelta(seconds=indice / 5),
    )


def track(x: float, y: float, *, track_id: int = 1, label: str = "car") -> Track:
    """Un track cuyo punto de apoyo (`bbox.bottom_center`) es exactamente `(x, y)`."""
    return Track(
        bbox=BBox(x - 20, y - 40, x + 20, y),
        class_id=2,
        label=label,
        confidence=0.9,
        track_id=track_id,
    )


def analizador(*lineas: LineaDeConteo, **kwargs: object) -> TrafficFlowAnalyzer:
    return TrafficFlowAnalyzer(lineas or (HORIZONTAL,), **kwargs)  # type: ignore[arg-type]


def recorrer(
    analizador: TrafficFlowAnalyzer,
    pasos: list[list[Track]],
    *,
    desde: int = 0,
    ancho: int = ANCHO,
    alto: int = ALTO,
) -> list[AnalyzerOutput]:
    return [
        analizador.process(fotograma(desde + i, ancho=ancho, alto=alto), tracks)
        for i, tracks in enumerate(pasos)
    ]


def cruces(salidas: list[AnalyzerOutput]) -> list[Event]:
    return [e for salida in salidas for e in salida.events if e.name == "line_crossing"]


def sentidos(salidas: list[AnalyzerOutput]) -> list[str]:
    return [str(e.attributes["direction"]) for e in cruces(salidas)]


def trayectoria(ys: list[float], *, x: float = 0.5, track_id: int = 1) -> list[list[Track]]:
    """Un track solo, bajando por la vertical `x` con las `ys` dadas, todo en fracciones."""
    return [[track(x * ANCHO, y * ALTO, track_id=track_id)] for y in ys]


# --- El caso que decide el issue: el temblor sobre la linea -----------------------------


TEMBLOR = [0.49, 0.51] * 10  # 20 fotogramas alternando de lado


def test_un_coche_que_tiembla_sobre_la_linea_no_produce_ningun_cruce_con_k_2() -> None:
    """Es el fallo mas probable y el mas dificil de ver: un coche aparcado cuya caja tiembla
    cambia de lado en cada fotograma, y cada cruce individual parece legitimo."""
    salidas = recorrer(analizador(min_frames_on_side=2), trayectoria(TEMBLOR))
    assert cruces(salidas) == []


def test_con_k_1_el_mismo_temblor_produce_19_cruces_que_es_lo_que_el_parametro_evita() -> None:
    """La otra cifra: sin confirmacion, 20 observaciones alternas son 19 cambios de lado y
    19 cruces falsos. Es lo que `min_frames_on_side` protege."""
    salidas = recorrer(analizador(min_frames_on_side=1), trayectoria(TEMBLOR))
    assert len(cruces(salidas)) == 19
    assert sentidos(salidas) == ["sur", "norte"] * 9 + ["sur"]


def test_el_valor_por_defecto_de_min_frames_on_side_es_2() -> None:
    assert analizador().min_frames_on_side == 2


# --- Contrato y registro ----------------------------------------------------------------


def test_cumple_el_protocolo_analyzer() -> None:
    assert isinstance(analizador(), Analyzer)


def test_esta_registrado_como_traffic_flow() -> None:
    assert ANALYZERS.get("traffic_flow") is TrafficFlowAnalyzer


def test_from_config_toma_el_nombre_la_cadencia_y_las_lineas_del_yaml() -> None:
    from cvp.pipeline.config import TrafficFlowAnalyzer as Cfg

    cfg = Cfg.model_validate(
        {
            "type": "traffic_flow",
            "name": "entrada",
            "every_n_frames": 3,
            "lines": [
                {
                    "id": "principal",
                    "p1": [0.0, 0.5],
                    "p2": [1.0, 0.5],
                    "positive_direction": "sur",
                    "negative_direction": "norte",
                },
                {
                    "id": "lateral",
                    "p1": [0.5, 0.0],
                    "p2": [0.5, 1.0],
                    "positive_direction": "oeste",
                    "negative_direction": "este",
                },
            ],
        }
    )
    construido = TrafficFlowAnalyzer.from_config(cfg)
    assert construido.name == "entrada"
    assert construido.every_n_frames == 3
    assert construido.lines == (HORIZONTAL, VERTICAL)
    # Sin `name` en el YAML, el nombre efectivo es el `type`.
    sin_nombre = Cfg.model_validate(cfg.model_dump() | {"name": None})
    assert TrafficFlowAnalyzer.from_config(sin_nombre).name == "traffic_flow"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": " "},
        {"every_n_frames": 0},
        {"min_frames_on_side": 0},
        {"forget_after_frames": 0},
    ],
    ids=["name-vacio", "cadencia-cero", "k-cero", "olvido-cero"],
)
def test_un_parametro_imposible_falla_al_construir(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        analizador(**kwargs)


def test_sin_lineas_o_con_ids_repetidos_falla_al_construir() -> None:
    with pytest.raises(ValueError, match="lineas"):
        TrafficFlowAnalyzer([])
    with pytest.raises(ValueError, match="principal"):
        TrafficFlowAnalyzer([HORIZONTAL, HORIZONTAL])


# --- La geometria se resuelve en el primer process(), no en la construccion -------------


def test_dos_analizadores_identicos_a_1080p_y_a_720p_dan_los_mismos_cruces() -> None:
    """La geometria se convierte desde `frame.image.shape`, asi que la misma trayectoria en
    fracciones tiene que dar exactamente los mismos cruces en las dos resoluciones."""
    ys = [0.3, 0.4, 0.45, 0.55, 0.6, 0.7]
    en_1080 = recorrer(analizador(), trayectoria(ys), ancho=1920, alto=1080)
    en_720 = recorrer(
        analizador(),
        [[track(0.5 * 1280, y * 720)] for y in ys],
        ancho=1280,
        alto=720,
    )
    assert cruces(en_1080) == cruces(en_720)
    assert sentidos(en_1080) == ["sur"]


def test_si_la_forma_cambia_a_mitad_se_reconvierte_y_se_registra_un_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = analizador(name="entrada")
    with caplog.at_level(logging.WARNING):
        # Dos fotogramas a 1080p por encima de la linea...
        recorrer(a, [[track(960, 0.4 * 1080)]] * 2, ancho=1920, alto=1080)
        # ...y dos a 720p por debajo: el cruce solo sale bien si la linea se ha reconvertido
        # a la nueva resolucion (a 1080p, y = 0,6 * 720 = 432 seguiria estando *arriba*).
        salidas = recorrer(a, [[track(640, 0.6 * 720)]] * 2, desde=2, ancho=1280, alto=720)
    assert sentidos(salidas) == ["sur"]
    avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1
    assert "entrada" in avisos[0]
    assert "1920x1080" in avisos[0] and "1280x720" in avisos[0]


# --- El punto de referencia y el lado ---------------------------------------------------


def test_el_punto_de_referencia_es_bottom_center_no_el_centro_de_la_caja() -> None:
    """Con la camara elevada del proyecto 01, el centro de la caja cruza la linea varios
    metros antes o despues que las ruedas. Un vehiculo que se aleja (sube por la pantalla)
    con una caja alta cuyo centro ya esta por encima de la linea pero cuya base sigue por
    debajo **no** ha cruzado todavia."""
    linea_y = 0.5 * ALTO
    # Centro en linea_y - 40 (ya "arriba"), base en linea_y + 30 (todavia "abajo").
    alta = Track(
        bbox=BBox(940, linea_y - 110, 980, linea_y + 30),
        class_id=2,
        label="car",
        confidence=0.9,
        track_id=1,
    )
    assert alta.bbox.center[1] < linea_y < alta.bbox.bottom_center[1]
    abajo = [[track(960, 0.7 * ALTO)]] * 2
    salidas = recorrer(analizador(), abajo + [[alta]] * 3)
    assert cruces(salidas) == [], "con el centro de la caja esto seria un cruce hacia el norte"


def test_un_punto_exactamente_sobre_la_linea_conserva_el_lado_anterior() -> None:
    """Producto vectorial cero. Con coordenadas en pixeles enteros pasa, y no cuenta como
    cambio de lado: ni hacia un lado ni hacia el otro."""
    sobre = ALTO // 2  # y = 540, exactamente sobre la linea
    a = analizador(min_frames_on_side=2)
    salidas = recorrer(a, [[track(960, 400)]] * 2 + [[track(960, sobre)]] * 5)
    assert cruces(salidas) == [], "estar sobre la linea no es cruzarla"
    # Y el lado que se conserva es el de antes: al bajar de verdad, el cruce es hacia el sur.
    salidas = recorrer(a, [[track(960, 700)]] * 2, desde=7)
    assert sentidos(salidas) == ["sur"]


def test_un_track_que_aparece_sobre_la_linea_no_tiene_lado_y_no_cruza_al_moverse() -> None:
    sobre = ALTO // 2
    salidas = recorrer(analizador(), [[track(960, sobre)]] * 3 + [[track(960, 700)]] * 3)
    assert cruces(salidas) == []


def test_funciona_con_una_linea_diagonal_en_los_dos_sentidos() -> None:
    a = analizador(DIAGONAL)
    ida = [[track(f * ANCHO, f * ALTO, track_id=1)] for f in (0.2, 0.3, 0.45, 0.55, 0.7, 0.8)]
    vuelta = [[track(f * ANCHO, f * ALTO, track_id=2)] for f in (0.8, 0.7, 0.55, 0.45, 0.3, 0.2)]
    assert sentidos(recorrer(a, ida)) == ["abajo-derecha"]
    assert sentidos(recorrer(a, vuelta, desde=6)) == ["arriba-izquierda"]


def test_funciona_con_una_linea_vertical() -> None:
    xs = [0.3, 0.4, 0.6, 0.7]
    salidas = recorrer(analizador(VERTICAL), [[track(x * ANCHO, 800)] for x in xs])
    # De izquierda a derecha se pasa al lado negativo de una linea escrita hacia abajo.
    assert sentidos(salidas) == ["este"]


# --- Los cruces, uno por fallo posible --------------------------------------------------


def test_del_lado_negativo_al_positivo_produce_exactamente_un_cruce_positivo() -> None:
    salidas = recorrer(analizador(), trayectoria([0.3, 0.4, 0.45, 0.55, 0.6, 0.7, 0.8]))
    (evento,) = cruces(salidas)
    assert evento.name == "line_crossing"
    assert evento.attributes["direction"] == "sur"
    assert evento.attributes["line"] == "principal"
    assert evento.attributes["track_id"] == 1
    assert evento.attributes["label"] == "car"


def test_del_positivo_al_negativo_produce_uno_con_el_sentido_contrario() -> None:
    salidas = recorrer(analizador(), trayectoria([0.8, 0.7, 0.6, 0.45, 0.4, 0.3]))
    assert sentidos(salidas) == ["norte"]


def test_un_track_que_aparece_ya_al_otro_lado_no_cruza() -> None:
    """El coche que entra en escena por delante de la linea: sin observacion previa no hay
    cruce."""
    salidas = recorrer(analizador(), trayectoria([0.6, 0.7, 0.8, 0.9]))
    assert cruces(salidas) == []


def test_cruzar_y_volver_son_dos_cruces_de_sentidos_opuestos() -> None:
    salidas = recorrer(analizador(), trayectoria([0.3, 0.4, 0.6, 0.7, 0.6, 0.4, 0.3]))
    assert sentidos(salidas) == ["sur", "norte"]


def test_un_track_que_desaparece_antes_de_llegar_no_cruza() -> None:
    a = analizador()
    salidas = recorrer(a, trayectoria([0.2, 0.3, 0.4, 0.45]))
    salidas += recorrer(a, [[], [], []], desde=4)
    assert cruces(salidas) == []


def test_dos_tracks_que_cruzan_a_la_vez_en_sentidos_opuestos_no_se_mezclan() -> None:
    bajando = [0.3, 0.4, 0.6, 0.7]
    subiendo = [0.7, 0.6, 0.4, 0.3]
    pasos = [
        [track(700, yb * ALTO, track_id=7), track(1200, ys * ALTO, track_id=9)]
        for yb, ys in zip(bajando, subiendo, strict=True)
    ]
    eventos = cruces(recorrer(analizador(), pasos))
    assert sorted((e.attributes["track_id"], e.attributes["direction"]) for e in eventos) == [
        (7, "sur"),
        (9, "norte"),
    ]


def test_un_track_que_cruza_dos_lineas_produce_un_cruce_por_linea() -> None:
    # Baja por x = 0,3 (a la izquierda de la vertical) y luego se va a la derecha.
    pasos = [
        [track(0.3 * ANCHO, 0.3 * ALTO)],
        [track(0.3 * ANCHO, 0.4 * ALTO)],
        [track(0.3 * ANCHO, 0.6 * ALTO)],
        [track(0.3 * ANCHO, 0.7 * ALTO)],
        [track(0.6 * ANCHO, 0.7 * ALTO)],
        [track(0.7 * ANCHO, 0.7 * ALTO)],
    ]
    eventos = cruces(recorrer(analizador(HORIZONTAL, VERTICAL), pasos))
    assert [(e.attributes["line"], e.attributes["direction"]) for e in eventos] == [
        ("principal", "sur"),
        ("lateral", "este"),
    ]


def test_se_necesitan_k_observaciones_seguidas_en_el_nuevo_lado_y_una_vuelta_reinicia() -> None:
    # Con K = 3: dos fotogramas abajo, uno arriba y dos abajo no confirman nada.
    a = analizador(min_frames_on_side=3)
    salidas = recorrer(a, trayectoria([0.3, 0.3, 0.3, 0.6, 0.6, 0.3, 0.6, 0.6]))
    assert cruces(salidas) == []
    # El tercero seguido si.
    assert sentidos(recorrer(a, trayectoria([0.6]), desde=8)) == ["sur"]


# --- Olvidar el estado de los tracks que ya no estan ------------------------------------


def test_el_estado_de_un_track_que_lleva_mas_de_forget_after_frames_sin_verse_se_olvida() -> None:
    a = analizador(forget_after_frames=60)
    recorrer(a, trayectoria([0.3, 0.3, 0.3]))  # indices 0, 1, 2: confirmado arriba
    assert a.tracks_recordados == 1
    a.process(fotograma(62), [])  # 60 fotogramas sin verse: todavia se recuerda
    assert a.tracks_recordados == 1
    a.process(fotograma(63), [])  # 61: se olvida
    assert a.tracks_recordados == 0


def test_un_track_olvidado_que_reaparece_al_otro_lado_no_cruza() -> None:
    """Sin esto la memoria crece con cada vehiculo que pasa, para siempre. Y olvidar de
    verdad significa que al volver es un track nuevo: aparece ya al otro lado."""
    a = analizador(forget_after_frames=10)
    recorrer(a, trayectoria([0.3, 0.3]))
    a.process(fotograma(50), [])
    salidas = recorrer(a, trayectoria([0.7, 0.7, 0.7]), desde=51)
    assert cruces(salidas) == []

    # Con un olvido mas largo, la misma secuencia si es un cruce: el track sigue recordado.
    b = analizador(forget_after_frames=100)
    recorrer(b, trayectoria([0.3, 0.3]))
    b.process(fotograma(50), [])
    assert sentidos(recorrer(b, trayectoria([0.7, 0.7, 0.7]), desde=51)) == ["sur"]


def test_el_valor_por_defecto_de_forget_after_frames_es_60() -> None:
    assert analizador().forget_after_frames == 60


def test_la_memoria_no_crece_con_los_vehiculos_que_ya_han_pasado() -> None:
    a = analizador(forget_after_frames=5)
    for i in range(200):
        # Cada fotograma trae un track nuevo, que no vuelve a verse.
        a.process(fotograma(i), [track(960, 300, track_id=i)])
    assert a.tracks_recordados <= 6


# --- Lo que sale --------------------------------------------------------------------------


def test_cada_cruce_emite_su_evento_y_su_metrica_crossings_con_las_mismas_etiquetas() -> None:
    a = analizador(name="entrada")
    salidas = recorrer(a, trayectoria([0.3, 0.4, 0.6, 0.7]))
    (evento,) = cruces(salidas)
    (metrica,) = [m for s in salidas for m in s.metrics if m.name == "crossings"]

    assert evento.analyzer == "entrada"
    assert dict(evento.attributes) == {
        "line": "principal",
        "direction": "sur",
        "track_id": 1,
        "label": "car",
    }
    assert metrica.analyzer == "entrada"
    assert metrica.value == 1.0
    assert dict(metrica.labels) == {"line": "principal", "direction": "sur"}
    assert metrica.timestamp == evento.timestamp


def test_cada_process_emite_vehicles_in_scene_con_el_numero_de_tracks() -> None:
    a = analizador()
    con_tres = a.process(fotograma(0), [track(100, 100, track_id=i) for i in range(3)])
    sin_nadie = a.process(fotograma(1), [])
    (m3,) = [m for m in con_tres.metrics if m.name == "vehicles_in_scene"]
    (m0,) = [m for m in sin_nadie.metrics if m.name == "vehicles_in_scene"]
    assert (m3.value, m0.value) == (3.0, 0.0)
    assert dict(m3.labels) == {}


def test_todos_los_timestamps_salen_de_frame_timestamp_nunca_del_reloj() -> None:
    marca = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)  # un instante que no es "ahora"
    a = analizador()
    recorrer(a, trayectoria([0.3, 0.3]))
    salida = a.process(fotograma(2, timestamp=marca), [track(960, 0.7 * ALTO)])
    salida2 = a.process(fotograma(3, timestamp=marca), [track(960, 0.7 * ALTO)])
    assert salida.metrics and salida2.events and salida2.metrics
    for cosa in (*salida.metrics, *salida2.events, *salida2.metrics):
        assert cosa.timestamp == marca


def test_el_analyzer_de_eventos_y_metricas_es_el_name_no_el_type() -> None:
    """Dos analizadores de trafico en el mismo proyecto tienen que distinguirse."""
    a = analizador(name="entrada")
    salidas = recorrer(a, trayectoria([0.3, 0.4, 0.6, 0.7]))
    todo = [x for s in salidas for x in (*s.events, *s.metrics)]
    assert todo
    assert {x.analyzer for x in todo} == {"entrada"}


def test_las_metricas_crossings_agregadas_por_minuto_dan_vehiculos_por_sentido() -> None:
    """Es lo que hace que `metric_minute` de #9 de vehiculos por minuto por sentido con su
    `sum_value`, sin que el dashboard tenga que agrupar eventos crudos."""
    a = analizador()
    pasos = trayectoria([0.3, 0.4, 0.6, 0.7], track_id=1) + trayectoria(
        [0.3, 0.4, 0.6, 0.7], track_id=2
    )
    pasos += trayectoria([0.7, 0.6, 0.4, 0.3], track_id=3)
    salidas = recorrer(a, pasos)
    metricas = [m for s in salidas for m in s.metrics if m.name == "crossings"]
    agregados = agregar_por_minuto(metricas)
    por_sentido = {clave.labels: agregado.sum_value for clave, agregado in agregados.items()}
    assert por_sentido == {
        '{"direction":"sur","line":"principal"}': 2.0,
        '{"direction":"norte","line":"principal"}': 1.0,
    }


def test_el_docstring_avisa_de_que_label_no_sirve_para_desglosar_por_tipo() -> None:
    """Los autobuses del proyecto 01 salen como `car` o `truck` y la clase `bus` no aparece
    nunca (`_docs/rendimiento.md`, #6). El conteo total si vale."""
    documentacion = " ".join((TrafficFlowAnalyzer.__doc__ or "").split())
    assert "label" in documentacion
    assert "no es fiable" in documentacion
    assert "bus" in documentacion


def test_la_salida_es_un_analyzeroutput_con_tipos_del_core() -> None:
    salida = analizador().process(fotograma(0), [track(960, 300)])
    assert isinstance(salida, AnalyzerOutput)
    assert all(isinstance(m, Metric) for m in salida.metrics)
    assert all(isinstance(e, Event) for e in salida.events)


# --- annotate() -------------------------------------------------------------------------


def test_annotate_devuelve_un_array_nuevo_y_deja_el_original_intacto_byte_a_byte() -> None:
    a = analizador()
    recorrer(a, trayectoria([0.3, 0.4, 0.6, 0.7]))
    imagen = np.random.default_rng(0).integers(0, 255, (ALTO, ANCHO, 3), dtype=np.uint8)
    antes = imagen.copy()

    anotada = a.annotate(imagen, [track(960, 700)])

    assert anotada is not imagen
    assert not np.shares_memory(anotada, imagen)
    assert np.array_equal(imagen, antes)
    assert anotada.shape == imagen.shape and anotada.dtype == np.uint8
    assert not np.array_equal(anotada, imagen), "algo se tiene que haber dibujado"


def test_annotate_marca_el_bottom_center_de_cada_track_y_no_su_centro() -> None:
    a = analizador()
    imagen = np.zeros((ALTO, ANCHO, 3), dtype=np.uint8)
    t = track(300, 900)  # base en (300, 900); centro en (300, 880)
    anotada = a.annotate(imagen, [t])
    x, y = t.bbox.bottom_center
    assert anotada[int(y) - 1, int(x)].any(), "el punto de apoyo tiene que estar marcado"


def test_annotate_dibuja_cada_linea_con_su_id_y_los_contadores_de_los_dos_sentidos() -> None:
    a = analizador(HORIZONTAL, VERTICAL)
    recorrer(a, trayectoria([0.3, 0.4, 0.6, 0.7]))
    imagen = np.zeros((ALTO, ANCHO, 3), dtype=np.uint8)
    anotada = a.annotate(imagen, [])
    # La horizontal pasa por (200, 540) y la vertical por (960, 200).
    assert anotada[540, 200].any()
    assert anotada[200, 960].any()
    # Los contadores acumulados son datos, no solo dibujo: es lo que se pinta junto a la linea.
    assert a.contadores == {
        ("principal", "sur"): 1,
        ("principal", "norte"): 0,
        ("lateral", "oeste"): 0,
        ("lateral", "este"): 0,
    }


def test_annotate_funciona_antes_del_primer_process_y_con_otra_resolucion() -> None:
    """`annotate` recibe la imagen, asi que convierte con su forma y no toca el estado de
    `process`: un preview reescalado no puede disparar el WARNING de cambio de forma."""
    a = analizador()
    anotada = a.annotate(np.zeros((360, 640, 3), dtype=np.uint8), [track(320, 300)])
    assert anotada[180, 100].any(), "la linea a mitad de un fotograma de 640x360"
    assert a.resolucion is None


# --- Lo que un cruce no es ----------------------------------------------------------------


def test_para_cada_cruce_el_track_fue_observado_a_ambos_lados_antes() -> None:
    """La misma comprobacion que hace el test lento con video real, aqui con la
    aritmetica de `lado_de` por fuera del analizador."""
    a = analizador()
    linea = HORIZONTAL.to_pixels(ANCHO, ALTO)
    lados: dict[int, set[int]] = {}
    pasos = trayectoria([0.3, 0.4, 0.6, 0.7], track_id=1) + trayectoria([0.6, 0.7], track_id=2)
    for i, tracks in enumerate(pasos):
        salida = a.process(fotograma(i), tracks)
        for e in salida.events:
            vistos = lados.get(int(e.attributes["track_id"]), set())
            assert vistos >= {-1, 1}, f"cruce sin haber visto los dos lados: {e}"
        for t in tracks:
            lados.setdefault(t.track_id, set()).add(
                lado_de(linea.p1, linea.p2, t.bbox.bottom_center)
            )
    assert len(cruces([a.process(fotograma(99), [])])) == 0


# --- Con video real ---------------------------------------------------------------------

FIXTURE_1080P = Path(__file__).resolve().parents[1] / "fixtures" / "video" / "calle-1080p-6s.mp4"

# Cruza la calzada del fixture a media altura, de la fila de aparcados de la izquierda a la
# de la derecha. Escrita de izquierda a derecha, su lado positivo es el de abajo: el de los
# vehiculos que se acercan a la camara.
CALZADA = LineaDeConteo(
    id="calzada",
    p1=(0.25, 0.62),
    p2=(0.62, 0.50),
    positive_direction="acercandose",
    negative_direction="alejandose",
)


@pytest.mark.slow
def test_sobre_video_real_hay_cruces_y_cada_uno_fue_visto_a_ambos_lados_antes() -> None:
    """Lo que distingue un cruce de un artefacto: para cada cruce, el `track_id` fue
    observado a **ambos** lados de la linea en fotogramas anteriores, con la aritmetica
    de `lado_de` por fuera del analizador.

    `track_activation_threshold: 0.5` es lo que #7 midio que hace falta en esta escena:
    con el 0,7 por defecto se siguen 5,4 de los ~14 vehiculos que el detector ve.
    """
    from cvp.detectors import YoloDetector
    from cvp.sources import FileVideoSource
    from cvp.trackers import ByteTrackTracker

    detector = YoloDetector(
        "yolo11n.pt", imgsz=640, conf=0.35, classes=["car", "motorcycle", "bus", "truck"]
    )
    tracker = ByteTrackTracker(frame_rate=5, track_activation_threshold=0.5)
    a = TrafficFlowAnalyzer([CALZADA], name="trafico")
    origen = FileVideoSource(FIXTURE_1080P, target_fps=5, start_time=INICIO)
    origen.open()
    try:
        info = origen.info
        linea = CALZADA.to_pixels(info.width, info.height)
        lados_vistos: dict[int, set[int]] = {}
        eventos: list[Event] = []
        fotogramas = 0
        for fotograma_real in origen.frames():
            tracks = tracker.update(fotograma_real, detector.detect(fotograma_real))
            salida = a.process(fotograma_real, tracks)
            fotogramas += 1
            for e in salida.events:
                track_id = int(e.attributes["track_id"])
                assert lados_vistos.get(track_id, set()) >= {-1, 1}, (
                    f"cruce de {track_id} sin haberlo visto a los dos lados antes: {e}"
                )
                assert e.timestamp == fotograma_real.timestamp
                eventos.append(e)
            for t in tracks:
                lados_vistos.setdefault(t.track_id, set()).add(
                    lado_de(linea.p1, linea.p2, t.bbox.bottom_center)
                )
    finally:
        origen.close()

    assert fotogramas >= 25, "6 s a 5 fps deberian dar unos 30 fotogramas"
    assert eventos, "en 6 s de esta calle tiene que cruzar la linea al menos un vehiculo"
    assert {e.analyzer for e in eventos} == {"trafico"}
    assert sum(a.contadores.values()) == len(eventos)
