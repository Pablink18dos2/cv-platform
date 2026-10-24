"""Las piezas reutilizables de las fuentes en directo: la linea de tiempo y el buffer.

Viven en `cvp.sources.live` y no dentro del connector de YouTube porque RTSP y webcam
tienen exactamente el mismo problema: el transporte entrega cuando quiere y el analisis
consume a su ritmo, y quien concilia los dos es esto.

`VideoTimeline` es donde se arregla el fallo de #23. Aqui se prueba **sin hilos y sin
fuente**: es una funcion del numero de fotograma, no del reloj, y eso es justo lo que hay
que fijar. Todo lo de aqui es instantaneo.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from cvp.core import Event
from cvp.sources.errors import SourceError
from cvp.sources.live import (
    AVISO_CADA_S,
    DRENAJE_EN_VENTANAS,
    RecentFrameBuffer,
    VideoTimeline,
    espera_de_backoff,
)

INICIO = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
NATIVO = 30.0
# Tope de seguridad de las esperas sobre eventos: si el codigo es correcto se cumplen en
# microsegundos, y si no lo es el test falla en vez de colgar la suite.
ESPERA_MAXIMA_S = 2.0


class RelojFalso:
    """Reloj inyectable que solo se mueve cuando el test se lo dice."""

    def __init__(self, inicio: datetime = INICIO) -> None:
        self._ahora = inicio
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._ahora

    def avanzar(self, segundos: float) -> None:
        with self._lock:
            self._ahora += timedelta(seconds=segundos)


def _imagen(valor: int) -> np.ndarray:
    imagen = np.zeros((4, 4, 3), dtype=np.uint8)
    imagen[0, 0, 0] = valor
    return imagen


def _linea(
    target_fps: float = 5.0,
    native_fps: float | None = 30.0,
    *,
    reloj: RelojFalso | None = None,
    max_drift_s: float = 30.0,
    eventos: list[Event] | None = None,
) -> tuple[VideoTimeline, RelojFalso]:
    reloj = reloj if reloj is not None else RelojFalso()
    linea = VideoTimeline(
        target_fps,
        native_fps,
        clock=reloj,
        max_drift_s=max_drift_s,
        nombre="fuente",
        al_reanclar=None if eventos is None else eventos.append,
    )
    linea.anclar("apertura")
    return linea, reloj


def _seleccionados(linea: VideoTimeline, cuantos: int) -> list[tuple[int, datetime]]:
    """Pasa `cuantos` fotogramas decodificados y devuelve (indice, instante) de los que salen."""
    salida = []
    for indice in range(cuantos):
        marca = linea.muestrear()
        if marca is not None:
            salida.append((indice, marca))
    return salida


# --- La linea de tiempo del video -----------------------------------------------------


def test_muestrea_uno_de_cada_seis_a_cinco_fps_sobre_treinta() -> None:
    linea, _ = _linea()
    assert [indice for indice, _ in _seleccionados(linea, 30)] == [0, 6, 12, 18, 24]


def test_los_instantes_son_los_del_video_y_no_los_del_reloj() -> None:
    # El reloj no se mueve en todo el test: los 30 fotogramas llegan de golpe, como en una
    # rafaga de HLS. Aun asi salen separados 200 ms, porque 200 ms es lo que ocupan en el
    # video. Ese es el arreglo de #23 en una linea.
    linea, _ = _linea()
    marcas = [marca for _, marca in _seleccionados(linea, 30)]
    assert marcas == [INICIO + timedelta(seconds=n / 30) for n in (0, 6, 12, 18, 24)]


def test_el_muestreo_no_depende_de_como_llegue_el_transporte() -> None:
    # La misma decodificacion entregada de dos maneras: toda de golpe, y repartida en
    # tiempo real. La secuencia tiene que ser identica, instantes incluidos.
    de_golpe, _ = _linea()
    entera = _seleccionados(de_golpe, 300)

    a_cuentagotas, reloj = _linea()
    repartida = []
    for indice in range(300):
        marca = a_cuentagotas.muestrear()
        if marca is not None:
            repartida.append((indice, marca))
        reloj.avanzar(1 / 30)

    assert entera == repartida
    assert len(entera) == 50  # 10 s de video a 5 fps


def test_pedir_mas_fps_de_los_que_trae_el_video_no_duplica_fotogramas() -> None:
    linea, _ = _linea(target_fps=60)
    assert len(_seleccionados(linea, 10)) == 10


def test_el_descarte_se_reparte_de_forma_uniforme() -> None:
    # 7 no divide a 30, asi que este es el caso que obliga a repartir en vez de acumular
    # deriva. Es la misma regla que el connector de fichero de #4.
    linea, _ = _linea(target_fps=7)
    elegidos = [indice for indice, _ in _seleccionados(linea, 300)]
    huecos = [b - a for a, b in zip(elegidos[:-1], elegidos[1:], strict=True)]
    assert max(huecos) - min(huecos) <= 1


def test_los_instantes_son_estrictamente_crecientes_y_en_utc() -> None:
    linea, _ = _linea()
    marcas = [marca for _, marca in _seleccionados(linea, 300)]
    assert marcas == sorted(marcas)
    assert len(set(marcas)) == len(marcas)
    assert all(marca.utcoffset() == timedelta(0) for marca in marcas)


# --- Re-anclaje -----------------------------------------------------------------------


def test_reanclar_no_retrocede_aunque_el_reloj_se_haya_quedado_atras() -> None:
    # El transporte ha entregado 10 s de video mientras el reloj apenas se movia, que es
    # lo que hace una rafaga. Si la reconexion anclara al reloj a secas, los timestamps
    # retrocederian 10 s: como no pueden decrecer, la serie se quedaria congelada hasta
    # que el video volviera a alcanzarlos.
    linea, _ = _linea()
    ultimo = _seleccionados(linea, 300)[-1][1]
    assert ultimo == INICIO + timedelta(seconds=294 / 30)

    linea.anclar("reconexion")
    siguiente = linea.muestrear()
    assert siguiente == ultimo + timedelta(seconds=0.2)


def test_una_reconexion_reancla_al_reloj_y_lo_deja_en_el_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    linea, reloj = _linea()
    linea.muestrear()
    # Una caida de un minuto: el video que venga despues es de ahora, no de hace un minuto.
    reloj.avanzar(60)
    with caplog.at_level(logging.INFO):
        linea.anclar("reconexion")
    assert linea.muestrear() == INICIO + timedelta(seconds=60)
    assert "anclado" in caplog.text
    assert "reconexion" in caplog.text


def test_la_deriva_por_encima_del_umbral_reancla_y_lo_dice_con_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # El caso que describe #23: el decodificador no sostiene el ritmo del directo y el
    # tiempo de video se queda atras. Sin guardarrail, los timestamps dirian las 12:00
    # cuando son las 12:40.
    linea, reloj = _linea(max_drift_s=30)
    assert linea.muestrear() == INICIO
    reloj.avanzar(120)

    with caplog.at_level(logging.WARNING):
        marca = next(m for m in (linea.muestrear() for _ in range(6)) if m is not None)

    assert marca == INICIO + timedelta(seconds=120)
    assert "por detras del reloj" in caplog.text
    assert "120 s" in caplog.text
    # Y a partir de ahi se sigue contando desde el ancla nueva.
    assert _seleccionados(linea, 6)[-1][1] == INICIO + timedelta(seconds=120.2)


def test_una_deriva_por_debajo_del_umbral_no_toca_nada(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Dentro de un ciclo de segmento el tiempo de video adelanta y atrasa unos segundos
    # respecto al reloj. Re-anclar por eso llenaria la serie de saltos artificiales.
    linea, reloj = _linea(max_drift_s=30)
    linea.muestrear()
    reloj.avanzar(10)
    with caplog.at_level(logging.WARNING):
        marca = next(m for m in (linea.muestrear() for _ in range(6)) if m is not None)
    assert marca == INICIO + timedelta(seconds=0.2)
    assert caplog.text == ""


def test_ir_por_delante_del_reloj_no_retrocede_y_avisa_periodicamente(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Pasa al arrancar, cuando el decodificador vacia de golpe el buffer del transporte.
    # Re-anclar hacia atras congelaria la serie, asi que solo se avisa. #26: antes se
    # avisaba una sola vez por epoca, y QA midio 60 minutos de adelanto con un unico
    # WARNING. Ahora se repite cada `AVISO_CADA_S` mientras dure, con el valor de ese momento.
    linea, reloj = _linea(max_drift_s=30)
    with caplog.at_level(logging.WARNING):
        marcas = [marca for _, marca in _seleccionados(linea, 3000)]
        # Mismo instante de reloj: el primer aviso ya salio, el segundo todavia no toca.
        assert caplog.text.count("por delante del reloj") == 1
        reloj.avanzar(AVISO_CADA_S + 1)
        marcas += [marca for _, marca in _seleccionados(linea, 60)]
    assert marcas == sorted(marcas)
    assert len(set(marcas)) == len(marcas)
    assert caplog.text.count("por delante del reloj") == 2
    # Cada aviso lleva el adelanto de ese momento: 30 s al cruzar el umbral y 39 s un
    # minuto despues (el video lleva 100 s y el reloj 61), no repite el primero.
    assert re.findall(r"va (\d+) s por delante", caplog.text) == ["30", "39"]


def test_el_reanclaje_por_deriva_queda_como_evento_con_el_salto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    eventos: list[Event] = []
    linea, reloj = _linea(max_drift_s=30, eventos=eventos)
    linea.muestrear()
    assert eventos == [], "abrir no es un salto: aun no hay serie que partir"
    reloj.avanzar(120)

    with caplog.at_level(logging.WARNING):
        marca = next(m for m in (linea.muestrear() for _ in range(6)) if m is not None)

    (evento,) = eventos
    assert (evento.analyzer, evento.name) == ("source", "reanchor")
    # El evento cae donde se ve el escalon: en el primer instante de la epoca nueva.
    assert evento.timestamp == marca == INICIO + timedelta(seconds=120)
    assert evento.attributes["reason"] == "deriva"
    assert evento.attributes["jump_s"] == pytest.approx(120, abs=1)
    assert evento.attributes["ahead_s"] == 0


def test_una_reconexion_con_el_video_por_delante_no_hereda_el_adelanto_en_silencio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #26: "una reconexion hereda el adelanto intacto (600 s antes, 600 s despues)". No se
    # puede corregir -no se retrocede-, pero tiene que quedar dicho, en evento y en log.
    eventos: list[Event] = []
    linea, _ = _linea(max_drift_s=30, eventos=eventos)
    _seleccionados(linea, 3000)  # 100 s de video con el reloj parado

    with caplog.at_level(logging.WARNING):
        linea.anclar("reconexion")

    (evento,) = eventos
    assert evento.attributes["reason"] == "reconexion"
    assert evento.attributes["ahead_s"] == pytest.approx(100, abs=1)
    assert "se hereda" in caplog.text


# --- La latencia real, no la etiqueta ---------------------------------------------------


def test_el_retraso_se_mide_con_el_pts_del_stream_y_no_con_la_etiqueta() -> None:
    linea, reloj = _linea()
    marca = linea.muestrear()
    assert marca is not None
    assert linea.medir_retraso(5.0, marca) == 0  # la referencia: el primer fotograma
    # Pasan 100 s de reloj pero el stream solo ha avanzado 2: el decodificador va 98 s tarde.
    reloj.avanzar(100)
    assert linea.medir_retraso(7.0, marca) == pytest.approx(98)


def test_re_anclar_no_borra_el_retraso_medido(caplog: pytest.LogCaptureFixture) -> None:
    # El fallo de fondo de #26: el guardarrail re-etiquetaba el fotograma viejo con la hora
    # de ahora, y el unico sintoma desaparecia. La etiqueta vuelve a tener deriva 0, pero el
    # retraso medido contra el stream sigue siendo el que es.
    linea, reloj = _linea(max_drift_s=30)
    marca = linea.muestrear()
    assert marca is not None
    linea.medir_retraso(5.0, marca)
    reloj.avanzar(120)
    marca = next(m for m in (linea.muestrear() for _ in range(6)) if m is not None)
    assert marca == INICIO + timedelta(seconds=120)  # re-anclado: la etiqueta dice "ahora"

    with caplog.at_level(logging.WARNING):
        retraso = linea.medir_retraso(6.0, marca)

    assert retraso == pytest.approx(119)
    assert "por detras del borde en vivo" in caplog.text


def test_el_aviso_de_retraso_se_repite_mientras_dure_y_no_a_cada_fotograma(
    caplog: pytest.LogCaptureFixture,
) -> None:
    linea, reloj = _linea(max_drift_s=30)
    marca = linea.muestrear()
    assert marca is not None
    linea.medir_retraso(5.0, marca)
    reloj.avanzar(100)
    with caplog.at_level(logging.WARNING):
        for pts in (5.1, 5.2, 5.3):
            linea.medir_retraso(pts, marca)
        assert caplog.text.count("borde en vivo") == 1
        reloj.avanzar(AVISO_CADA_S + 1)
        linea.medir_retraso(5.4, marca)
    assert caplog.text.count("borde en vivo") == 2


def test_sin_pts_el_retraso_cae_a_la_deriva_de_la_etiqueta() -> None:
    linea, reloj = _linea()
    marca = linea.muestrear()
    assert marca is not None
    reloj.avanzar(7)
    assert linea.medir_retraso(0.0, marca) == pytest.approx(7)  # OpenCV devuelve 0 sin PTS
    assert linea.medir_retraso(None, marca) == pytest.approx(7)


def test_sin_pts_re_anclar_tampoco_borra_el_retraso() -> None:
    linea, reloj = _linea(max_drift_s=30)
    linea.muestrear()
    reloj.avanzar(120)
    marca = next(m for m in (linea.muestrear() for _ in range(6)) if m is not None)
    assert marca == INICIO + timedelta(seconds=120)  # la etiqueta vuelve a decir "ahora"
    assert linea.medir_retraso(None, marca) == pytest.approx(120, abs=1)


# --- El muestreo se ajusta a lo que el consumidor sostiene -----------------------------


def test_el_muestreo_baja_a_lo_que_el_consumidor_sostiene(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # El fallo que encontro QA: pidiendo 5 fps a una maquina que sostiene 3,75, se
    # seleccionaban 5, el buffer se llenaba y el descarte caia en una tirada contigua de la
    # cabeza. Huecos de 200 ms y de pronto uno de 1,2 s, con la mediana impecable.
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    _seleccionados(linea, 120)  # 4 s de video: 20 ofrecidos
    reloj.avanzar(4)
    with caplog.at_level(logging.INFO):
        linea.realimentar(tomados=15, pendientes=0)
    # No salta de 5 a 3,75 de una vez: baja como mucho un 15 % por ventana, para no
    # perseguir el ruido del detector, cuyo p95 es cuatro veces su mediana.
    assert linea.tasa_efectiva == pytest.approx(5.0 * 0.85)
    assert "sostiene 3.75" in caplog.text

    # Una ventana mas y ya esta en lo que el consumidor sostiene.
    _seleccionados(linea, 120)
    reloj.avanzar(4)
    linea.realimentar(tomados=30, pendientes=0)
    assert linea.tasa_efectiva == pytest.approx(3.75)


def test_con_la_tasa_ajustada_los_huecos_salen_repartidos() -> None:
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    _seleccionados(linea, 120)
    reloj.avanzar(4)
    linea.realimentar(tomados=15, pendientes=0)

    marcas = [marca for _, marca in _seleccionados(linea, 300)]
    huecos = [(b - a).total_seconds() for a, b in zip(marcas[:-1], marcas[1:], strict=True)]
    # Los huecos salen del periodo de la tasa nueva, redondeados al fotograma nativo mas
    # cercano, y lo peor que hace el cambio de tasa es estirar uno un fotograma. Nada que
    # se parezca al 1,2 s de tirar un bloque entero.
    cada = round(NATIVO / linea.tasa_efectiva)
    assert sorted(huecos)[len(huecos) // 2] == pytest.approx(cada / NATIVO, abs=1e-5)
    assert max(huecos) <= (cada + 1) / NATIVO + 1e-5


def test_el_retraso_que_no_se_va_solo_se_descuenta_del_muestreo() -> None:
    # Sin restar el valle del buffer, cada tanteo hacia arriba deja fotogramas de mas
    # esperando y el retraso crece hasta desbordar: el bloque descartado de golpe vuelve
    # por otro camino.
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    _seleccionados(linea, 120)
    reloj.avanzar(4)
    # Dos fuentes identicas salvo por los cinco fotogramas atascados en el valle del
    # buffer: la que los tiene muestrea mas bajo, para drenarlos.
    con_retraso, sin_retraso = [], []
    for pendientes, donde in ((5, con_retraso), (0, sin_retraso)):
        linea, reloj = _linea(target_fps=5.0)
        tomados = 0
        for _ in range(4):
            linea.realimentar(tomados=tomados, pendientes=pendientes)
            tomados += 15
            _seleccionados(linea, 120)
            reloj.avanzar(4)
        linea.realimentar(tomados=tomados, pendientes=pendientes)
        donde.append(linea.tasa_efectiva)

    assert con_retraso[0] < sin_retraso[0]
    # El drenaje ajusta, no hunde: drenarlo de golpe mandaba el muestreo al suelo en
    # cuanto el arranque dejaba el buffer lleno, y costaba un tercio de los fps.
    assert con_retraso[0] > sin_retraso[0] * 0.7


def test_el_muestreo_vuelve_a_subir_cuando_el_consumidor_da_abasto() -> None:
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    _seleccionados(linea, 120)
    reloj.avanzar(4)
    linea.realimentar(tomados=15, pendientes=0)
    tasa_baja = linea.tasa_efectiva
    assert tasa_baja < 5.0

    # Ahora se lleva todo lo que se le ofrece y el buffer se queda vacio.
    _seleccionados(linea, 120)
    reloj.avanzar(4)
    linea.realimentar(tomados=99, pendientes=0)
    assert linea.tasa_efectiva == pytest.approx(min(tasa_baja * 1.25, 5.0))
    assert linea.tasa_efectiva > tasa_baja


def test_el_muestreo_no_pasa_del_target_por_mucho_margen_que_haya() -> None:
    linea, reloj = _linea(target_fps=5.0)
    tomados = 0
    for _ in range(6):
        linea.realimentar(tomados=tomados, pendientes=0)
        tomados += len(_seleccionados(linea, 120))
        reloj.avanzar(4)
    linea.realimentar(tomados=tomados, pendientes=0)
    assert linea.tasa_efectiva == 5.0


def test_una_ventana_sin_consumo_no_cuenta() -> None:
    # Es la del arranque, mientras el detector carga los pesos y calienta. Ajustar ahi
    # dejaria el muestreo en el suelo y tardaria un minuto en recuperarse.
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    _seleccionados(linea, 300)
    reloj.avanzar(10)
    linea.realimentar(tomados=0, pendientes=0)
    assert linea.tasa_efectiva == 5.0


def test_el_muestreo_no_baja_del_suelo_aunque_el_consumidor_se_atasque() -> None:
    linea, reloj = _linea(target_fps=5.0)
    tomados = 0
    for _ in range(30):
        linea.realimentar(tomados=tomados, pendientes=20)
        _seleccionados(linea, 120)
        reloj.avanzar(4)
        tomados += 1  # un fotograma cada cuatro segundos: la maquina esta atascada
    linea.realimentar(tomados=tomados, pendientes=20)
    # Por mucho que se baje ventana tras ventana, no se deja de muestrear del todo: sin
    # suelo, un consumidor atascado dejaria la fuente muda y no habria con que volver.
    assert linea.tasa_efectiva >= 0.5
    assert linea.tasa_efectiva < 1.0


def test_antes_de_que_pase_la_ventana_no_se_ajusta_nada() -> None:
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    _seleccionados(linea, 60)
    reloj.avanzar(2)
    linea.realimentar(tomados=1, pendientes=9)
    assert linea.tasa_efectiva == 5.0


def _ventana(
    linea: VideoTimeline,
    reloj: RelojFalso,
    *,
    tomados: int,
    pendientes: int,
    decodificados: int = 120,
) -> None:
    """Pasa una ventana entera: se decodifica, pasa el tiempo y se realimenta."""
    _seleccionados(linea, decodificados)
    reloj.avanzar(4)
    linea.realimentar(tomados=tomados, pendientes=pendientes)


def test_el_valle_se_vuelve_a_medir_en_cada_ventana() -> None:
    """El valle es de la ventana, no de toda la ejecucion.

    Si solo pudiera decrecer, en cuanto el buffer se vaciara una vez el drenaje quedaria
    muerto para el resto de la ejecucion y nada lo diria. Es un fallo de larga duracion:
    ningun test corto lo ve por casualidad, porque hace falta que el buffer se vacie
    **antes** de que aparezca el retraso.

    Por eso se comparan tres ejecuciones y no dos: que las dos con retraso coincidan no
    demuestra nada si en ninguna se drena, asi que la tercera -sin retraso- es la que
    obliga a que el drenaje exista de verdad.
    """
    tasas = {}
    for nombre, se_vacia_primero, pendientes in (
        ("tras vaciarse", True, 5),
        ("sin vaciarse", False, 5),
        ("sin retraso", True, 0),
    ):
        linea, reloj = _linea(target_fps=10.0)
        linea.realimentar(tomados=0, pendientes=0)
        tomados = 0
        if se_vacia_primero:
            # Una ventana en la que el consumidor va al dia y el buffer llega a vaciarse.
            tomados += 40
            _ventana(linea, reloj, tomados=tomados, pendientes=0)
        # Y ahora dos en las que se queda corto y deja siempre cinco esperando.
        for _ in range(2):
            tomados += 15
            _ventana(linea, reloj, tomados=tomados, pendientes=pendientes)
        tomados += 500
        _ventana(linea, reloj, tomados=tomados, pendientes=pendientes)
        tasas[nombre] = linea.tasa_efectiva

    assert tasas["tras vaciarse"] == pytest.approx(tasas["sin vaciarse"]), (
        "haberse vaciado antes ha dejado el drenaje muerto para el resto de la ejecucion"
    )
    assert tasas["tras vaciarse"] < tasas["sin retraso"], "no se esta drenando nada"


def test_el_valle_se_descuenta_repartido_en_varias_ventanas() -> None:
    # No basta con que se descuente: **cuanto** se descuenta es la diferencia entre ajustar
    # y hundir. Drenarlo de una vez mandaba el muestreo al suelo en el arranque y costaba
    # un tercio de los fps contra el directo (2,47 en vez de 3,7).
    #
    # Las dos ejecuciones son identicas salvo por los cinco fotogramas que se quedan
    # esperando, asi que la diferencia entre sus tasas es exactamente el drenaje.
    tasas = {}
    for pendientes in (0, 5):
        linea, reloj = _linea(target_fps=10.0)
        linea.realimentar(tomados=0, pendientes=0)
        tomados = 0
        for _ in range(3):
            tomados += 15
            _ventana(linea, reloj, tomados=tomados, pendientes=0)
        # La penultima deja el valle puesto para la siguiente, sin drenar todavia.
        tomados += 15
        _ventana(linea, reloj, tomados=tomados, pendientes=pendientes)
        # Y en la ultima el consumidor se lleva todo lo que se le ofrece, asi que lo unico
        # que puede separar a las dos es el drenaje.
        tomados += 500
        _ventana(linea, reloj, tomados=tomados, pendientes=pendientes)
        tasas[pendientes] = linea.tasa_efectiva

    assert tasas[0] - tasas[5] == pytest.approx(5 / (4 * DRENAJE_EN_VENTANAS))


def test_un_consumidor_parado_del_todo_baja_el_muestreo_hasta_el_suelo() -> None:
    # Antes no bajaba: una ventana sin consumo se saltaba entera, asi que un analisis
    # atascado se quedaba muestreando a tope y el suelo era inalcanzable por ese camino.
    # El buffer seguia acotando el retraso, pero se gastaba CPU en fotogramas que se iban
    # a tirar y el comentario del suelo era mentira.
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    # Arranca consumiendo, para distinguir "todavia no ha empezado" de "se ha parado".
    _ventana(linea, reloj, tomados=20, pendientes=0)
    assert linea.tasa_efectiva == 5.0

    for _ in range(40):
        _ventana(linea, reloj, tomados=20, pendientes=20)
    # Hasta el suelo, salvo la banda muerta con la que se evita reajustar por nada.
    assert linea.tasa_efectiva == pytest.approx(0.5, abs=0.05)


def test_una_ventana_sin_consumo_antes_de_arrancar_no_cuenta() -> None:
    # La del arranque, mientras el detector carga los pesos: ahi nadie ha consumido nada
    # todavia y bajar el muestreo seria castigar a la maquina por estar encendiendose.
    linea, reloj = _linea(target_fps=5.0)
    linea.realimentar(tomados=0, pendientes=0)
    for _ in range(3):
        _ventana(linea, reloj, tomados=0, pendientes=20)
    assert linea.tasa_efectiva == 5.0


# --- `native_fps` desconocido ---------------------------------------------------------


def test_sin_frame_rate_declarado_se_avisa_al_construir(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Estimarlo de lo que tarda en llegar seria peor que no hacerlo: sobre un transporte a
    # rafagas se mediria la velocidad de la rafaga (135 fps), no la del video.
    with caplog.at_level(logging.WARNING):
        VideoTimeline(5.0, None, clock=RelojFalso(), nombre="hls sin fps")
    assert "no declara frame rate" in caplog.text
    assert "reloj de pared" in caplog.text


def test_sin_frame_rate_declarado_se_muestrea_contra_el_reloj() -> None:
    linea, reloj = _linea(native_fps=None)
    assert linea.muestrear() == INICIO
    # Con el reloj parado no sale ninguno mas: sin fps nativos no hay tiempo de video que
    # derivar, asi que se vuelve al comportamiento de #5.
    assert [linea.muestrear() for _ in range(5)] == [None] * 5
    reloj.avanzar(0.2)
    assert linea.muestrear() == INICIO + timedelta(seconds=0.2)


def test_sin_frame_rate_declarado_los_instantes_siguen_creciendo() -> None:
    linea, reloj = _linea(native_fps=None)
    marcas = []
    for _ in range(20):
        marca = linea.muestrear()
        if marca is not None:
            marcas.append(marca)
        reloj.avanzar(0.1)
    assert marcas == [INICIO + timedelta(seconds=0.2 * n) for n in range(10)]


@pytest.mark.parametrize(
    ("kwargs", "mensaje"),
    [
        ({"target_fps": 0}, "target_fps"),
        ({"target_fps": 5, "native_fps": 0}, "native_fps"),
        ({"target_fps": 5, "max_drift_s": 0}, "max_drift_s"),
    ],
)
def test_una_linea_de_tiempo_imposible_falla_al_construir(
    kwargs: dict[str, float], mensaje: str
) -> None:
    parametros: dict[str, object] = {"native_fps": 30.0, **kwargs}
    target_fps = parametros.pop("target_fps")
    with pytest.raises(ValueError, match=mensaje):
        VideoTimeline(target_fps, clock=RelojFalso(), **parametros)  # type: ignore[arg-type]


# --- El buffer acotado ----------------------------------------------------------------


def test_el_buffer_entrega_en_orden_y_no_repite() -> None:
    buffer = RecentFrameBuffer(4.0)
    for numero in range(1, 4):
        buffer.publicar(_imagen(numero), INICIO + timedelta(seconds=numero))
    assert [buffer.tomar(timeout=0).image[0, 0, 0] for _ in range(3)] == [1, 2, 3]  # type: ignore[union-attr]
    assert buffer.tomar(timeout=0) is None


def test_el_buffer_descarta_lo_mas_viejo_para_no_pasar_de_su_ventana() -> None:
    # Es lo que mantiene el retraso acotado: con una ventana de 0,6 s, el analisis nunca
    # puede ir mas de 0,6 s de video por detras del directo, por lento que vaya.
    buffer = RecentFrameBuffer(0.6)
    descartes = [
        buffer.publicar(_imagen(numero), INICIO + timedelta(seconds=0.2 * numero))
        for numero in range(6)
    ]
    assert descartes == [False, False, False, False, True, True]
    assert buffer.descartados == 2
    assert len(buffer) == 4
    assert [buffer.tomar(timeout=0).image[0, 0, 0] for _ in range(4)] == [2, 3, 4, 5]  # type: ignore[union-attr]


def test_la_ventana_del_buffer_no_depende_de_cuantos_fotogramas_quepan() -> None:
    # El tope se declaraba en fotogramas (`max_backlog_s * target_fps`) y se cobraba en
    # tiempo, que solo coincide mientras la separacion sea la que se supone: con una fuente
    # que declara 1 fps y `target_fps: 5`, un tope que se creia de 4 s era de 19 s. Es el
    # caso de RTSP y webcam, donde 1-5 fps nativos son normales.
    buffer = RecentFrameBuffer(4.0)
    for numero in range(40):
        buffer.publicar(_imagen(numero), INICIO + timedelta(seconds=numero))
    primero = buffer.tomar(timeout=0)
    assert primero is not None
    assert INICIO + timedelta(seconds=39) - primero.timestamp <= timedelta(seconds=4)


def test_el_buffer_cuenta_lo_que_se_lleva_el_analisis() -> None:
    # Es la medida con la que se decide si el muestreo va por delante del consumidor.
    buffer = RecentFrameBuffer(4.0)
    for numero in range(3):
        buffer.publicar(_imagen(numero), INICIO + timedelta(seconds=0.2 * numero))
    assert buffer.tomados == 0
    buffer.tomar(timeout=0)
    buffer.tomar(timeout=0)
    assert buffer.tomados == 2


def test_el_buffer_dice_cuantos_quedan_esperando() -> None:
    # Es lo que se le pasa a la linea de tiempo para que descuente el retraso que no se va
    # solo. El valle lo mide ella, que es quien sabe donde empieza y acaba una ventana.
    buffer = RecentFrameBuffer(4.0)
    for numero in range(10):
        buffer.publicar(_imagen(numero), INICIO + timedelta(seconds=0.2 * numero))
    for _ in range(8):
        buffer.tomar(timeout=0)
    assert len(buffer) == 2


def test_el_buffer_vacio_con_timeout_devuelve_none() -> None:
    assert RecentFrameBuffer(2.0).tomar(timeout=0) is None


def test_tomar_se_desbloquea_en_cuanto_llega_un_fotograma() -> None:
    buffer = RecentFrameBuffer(2.0)
    recibidos: list[int] = []

    def consumir() -> None:
        instantanea = buffer.tomar()
        assert instantanea is not None
        recibidos.append(int(instantanea.image[0, 0, 0]))

    consumidor = threading.Thread(target=consumir, daemon=True)
    consumidor.start()
    buffer.publicar(_imagen(7), INICIO)
    consumidor.join(ESPERA_MAXIMA_S)
    assert recibidos == [7]


def test_cerrar_despierta_a_quien_espera_en_vez_de_dejarlo_colgado() -> None:
    buffer = RecentFrameBuffer(2.0)
    resultado: list[object] = []
    consumidor = threading.Thread(target=lambda: resultado.append(buffer.tomar()), daemon=True)
    consumidor.start()
    buffer.cerrar()
    consumidor.join(ESPERA_MAXIMA_S)
    assert resultado == [None]


def test_cerrar_suelta_los_fotogramas_que_quedaban() -> None:
    # A 1080p, veinte fotogramas son ~124 MB: no tienen por que sobrevivir a `close()`.
    buffer = RecentFrameBuffer(4.0)
    buffer.publicar(_imagen(1), INICIO)
    buffer.cerrar()
    assert len(buffer) == 0
    assert buffer.tomar(timeout=0) is None


def test_publicar_despues_de_cerrar_no_reabre_el_buffer() -> None:
    buffer = RecentFrameBuffer(2.0)
    buffer.cerrar()
    buffer.publicar(_imagen(1), INICIO)
    assert buffer.tomar(timeout=0) is None


def test_el_buffer_relanza_el_error_con_el_que_murio_el_hilo_lector() -> None:
    # El hilo lector no puede lanzar hacia el generador del consumidor: son pilas
    # distintas. Sin esto, quedarse sin reintentos seria indistinguible de ir lento.
    buffer = RecentFrameBuffer(2.0)
    buffer.fallar(SourceError("se acabaron los reintentos"))
    with pytest.raises(SourceError, match="reintentos"):
        buffer.tomar(timeout=0)


def test_un_buffer_sin_ventana_no_tiene_sentido() -> None:
    with pytest.raises(ValueError, match="ventana"):
        RecentFrameBuffer(0)


# --- La escalera de esperas -----------------------------------------------------------


@pytest.mark.parametrize(
    ("intento", "esperado"),
    [(1, 1.0), (2, 2.0), (3, 5.0), (4, 5.0), (5, 5.0), (40, 5.0)],
)
def test_el_backoff_repite_el_ultimo_valor_cuando_se_acaba_la_escalera(
    intento: int, esperado: float
) -> None:
    # Semantica del modelo `Reconnect` de #3: mas reintentos que elementos no vuelve a
    # empezar por el principio ni deja de esperar.
    assert espera_de_backoff([1, 2, 5], intento) == esperado


def test_el_primer_intento_es_el_uno_y_no_el_cero() -> None:
    with pytest.raises(ValueError, match="empieza en 1"):
        espera_de_backoff([1, 2, 5], 0)
