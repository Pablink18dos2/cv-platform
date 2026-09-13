"""La agregacion por minuto, que es donde esta el riesgo de #9.

Desde #23 la tasa efectiva se adapta a lo que el analisis sostiene, **dentro** del minuto y
con sesgo: el analisis va mas lento cuando hay mas objetos en escena, asi que los minutos
con mas trafico son los que se muestrean menos. Una media por muestra los aplana; QA lo
midio (4,9 %, 24,2 % y 78,6 % de error segun como se mueva la cadencia) y esos tres
escenarios estan aqui, contra la media temporal real calculada aparte.

Cada test de la media comprueba dos cosas: que la media temporal acierta, y que la media
por muestra de las mismas muestras **falla** por lo que QA midio. Lo segundo es lo que hace
que el test no sea vacio: si un dia alguien vuelve a dividir por `samples`, el rojo dice
cuanto se equivoca.

Son funciones puras: ni base de datos, ni fotogramas, ni reloj.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from cvp.core import Metric
from cvp.storage.aggregation import (
    HUECO_MAXIMO_S,
    Agregado,
    AgregadorPorMinuto,
    ClaveMinuto,
    agregar_por_minuto,
    minuto_de,
)

INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)
MINUTO = timedelta(minutes=1)

# La cadencia que QA midio en una sola ejecucion de #23, y el motivo de este fichero.
FPS_ALTO = 5.0
FPS_BAJO = 2.74


def metrica(marca: datetime, valor: float, **labels: str) -> Metric:
    return Metric(
        analyzer="traffic_flow",
        name="vehicles_in_scene",
        value=valor,
        timestamp=marca,
        labels=labels,
    )


def en(segundos: float, valor: float) -> Metric:
    return metrica(INICIO + timedelta(seconds=segundos), valor)


def serie(cadencias: Sequence[float], valor_del_minuto: Sequence[float]) -> Iterator[Metric]:
    """Una muestra cada `1/fps` segundos, con la cadencia de cada minuto y su valor."""
    for i, (fps, valor) in enumerate(zip(cadencias, valor_del_minuto, strict=True)):
        paso = timedelta(seconds=1.0 / fps)
        marca = INICIO + MINUTO * i
        fin = INICIO + MINUTO * (i + 1)
        while marca < fin:
            yield metrica(marca, valor)
            marca += paso


def muestrear_un_minuto(
    fps_en: Callable[[float], float], valor_en: Callable[[float], float]
) -> list[Metric]:
    """Un minuto muestreado con la cadencia y el valor que digan las dos funciones de `t`.

    Termina con una muestra ya en el minuto siguiente, para que la ultima del minuto tenga
    con que cerrar su tramo: es lo que pasa en un worker que sigue corriendo.
    """
    t = 0.0
    muestras = []
    while t < 60.0:
        muestras.append(en(t, valor_en(t)))
        t += 1.0 / fps_en(t)
    muestras.append(en(t, valor_en(59.999)))
    return muestras


def por_minuto(agregados: dict[ClaveMinuto, Agregado]) -> dict[datetime, Agregado]:
    return {clave.minute: agregado for clave, agregado in agregados.items()}


def el_minuto(agregados: dict[ClaveMinuto, Agregado], minuto: datetime = INICIO) -> Agregado:
    return por_minuto(agregados)[minuto]


def media_por_muestra(agregado: Agregado) -> float:
    """La media que estaba mal. Se calcula aqui para medir cuanto se equivocaba."""
    return agregado.sum_value / agregado.samples


def error(estimado: float, real: float) -> float:
    return abs(estimado - real) / real


# --- El minuto sale del timestamp de cada muestra -------------------------------------


def test_el_minuto_se_trunca_no_se_redondea() -> None:
    assert minuto_de(INICIO + timedelta(seconds=59, microseconds=999999)) == INICIO


def test_el_minuto_se_calcula_en_utc_aunque_la_marca_venga_en_otra_zona() -> None:
    """Con hora local, la noche del cambio de hora tendria un minuto repetido y otro
    inexistente, y la serie del dashboard mentiria dos veces al ano."""
    en_madrid = (INICIO + timedelta(seconds=30)).astimezone(timezone(timedelta(hours=2)))
    assert minuto_de(en_madrid) == INICIO


def test_el_minuto_de_una_muestra_sale_de_su_propia_marca_y_no_del_lote() -> None:
    """Ni del momento en que se escribe (llega despues y en lotes) ni del minuto del lote."""
    muestras = [en(10, 1.0), en(7 * 60 + 10, 2.0)]
    assert sorted(por_minuto(dict(agregar_por_minuto(muestras)))) == [INICIO, INICIO + 7 * MINUTO]


def test_un_lote_que_cruza_una_frontera_de_minuto_se_reparte_entre_los_dos() -> None:
    """Es el caso que un `INSERT ... ON CONFLICT` mal escrito resuelve mal y nadie mira.

    Cuatro muestras, dos a cada lado de las 14:21:00, en el mismo lote y en la misma
    llamada. El lote no tiene minuto: lo tiene cada muestra. Y el tiempo tampoco cruza: la
    muestra de las :59 cubre hasta la frontera y la de las :60 empieza en ella.
    """
    minutos = por_minuto(
        dict(agregar_por_minuto([en(58, 10.0), en(59, 20.0), en(60, 30.0), en(61, 40.0)]))
    )

    assert sorted(minutos) == [INICIO, INICIO + MINUTO]
    primero, segundo = minutos[INICIO], minutos[INICIO + MINUTO]
    assert (primero.samples, primero.sum_value) == (2, 30.0)
    assert (segundo.samples, segundo.sum_value) == (2, 70.0)
    # :58 cubre [58, 58,5) y :59 cubre [58,5, 60): 2 s, y la integral 10x0,5 + 20x1,5.
    assert primero.seconds == pytest.approx(2.0)
    assert primero.mean_value == pytest.approx(17.5)
    # :60 cubre [60, 60,5); :61 es la ultima y se queda pendiente.
    assert segundo.seconds == pytest.approx(0.5)
    assert segundo.mean_value == pytest.approx(30.0)


# --- La cadencia variable, que es el criterio de este issue ---------------------------


def test_los_agregados_no_dependen_de_la_cadencia_entre_minutos() -> None:
    """La misma serie a cadencia variable da los mismos agregados que a cadencia constante.

    Cuatro minutos, valor constante dentro de cada uno. En una version se mide siempre a
    5 fps; en la otra, la mitad de los minutos a 5 fps y la mitad a 2,74 fps. El numero de
    muestras **no** coincide (300 frente a 165) y los agregados si. Y se comparan con su
    valor absoluto, no solo entre si: un factor constante de mas —`sum x target_fps /
    samples`, la forma exacta que nombra el issue— dejaria las dos columnas iguales entre si
    e igual de mal.
    """
    valores = [12.0, 8.0, 15.0, 9.0]
    constante = por_minuto(dict(agregar_por_minuto(serie([FPS_ALTO] * 4, valores))))
    variable = por_minuto(dict(agregar_por_minuto(serie([FPS_ALTO, FPS_BAJO] * 2, valores))))

    assert sorted(constante) == sorted(variable) == [INICIO + MINUTO * i for i in range(4)]
    for i, valor in enumerate(valores):
        uno, otro = constante[INICIO + MINUTO * i], variable[INICIO + MINUTO * i]
        assert uno.mean_value == pytest.approx(valor)
        assert otro.mean_value == pytest.approx(valor)
        assert (uno.min_value, uno.max_value) == (otro.min_value, otro.max_value) == (valor, valor)

    lento, rapido = variable[INICIO + MINUTO], constante[INICIO + MINUTO]
    assert (rapido.samples, lento.samples) == (300, 165)
    assert lento.seconds == pytest.approx(60.0)
    assert rapido.seconds == pytest.approx(60.0)


def test_la_maquina_se_carga_a_mitad_de_minuto() -> None:
    """El primer escenario de QA: rampa de 10 a 20 y la cadencia cae de 5,0 a 2,74 fps en
    el segundo 30. Media real 15,0; la media por muestra da 14,26 (4,9 %)."""
    minuto = el_minuto(
        dict(
            agregar_por_minuto(
                muestrear_un_minuto(
                    lambda t: FPS_ALTO if t < 30 else FPS_BAJO,
                    lambda t: 10.0 + 10.0 * t / 60.0,
                )
            )
        )
    )
    assert minuto.mean_value == pytest.approx(15.0, rel=0.005)
    assert error(media_por_muestra(minuto), 15.0) > 0.045


def test_la_escena_se_llena_y_por_eso_cae_la_cadencia() -> None:
    """El segundo escenario de QA, y el que lleva el sesgo: 2 vehiculos a 5 fps la primera
    mitad, 20 vehiculos a 2,7 fps la segunda, porque con 20 el analisis va mas lento.
    Media real 11,0; la media por muestra da 8,3 (24 %), y siempre hacia abajo."""
    minuto = el_minuto(
        dict(
            agregar_por_minuto(
                muestrear_un_minuto(
                    lambda t: FPS_ALTO if t < 30 else 2.7,
                    lambda t: 2.0 if t < 30 else 20.0,
                )
            )
        )
    )
    assert minuto.mean_value == pytest.approx(11.0, rel=0.01)
    assert media_por_muestra(minuto) < 11.0
    assert error(media_por_muestra(minuto), 11.0) > 0.2


def test_un_pico_de_cinco_segundos_a_baja_cadencia() -> None:
    """El tercer escenario de QA: 55 s a 0 a 5 fps y 5 s de pico a 60 a 1 fps. Media real
    5,0; la media por muestra da 1,07 (78,6 %): el pico casi desaparece."""
    minuto = el_minuto(
        dict(
            agregar_por_minuto(
                muestrear_un_minuto(
                    lambda t: FPS_ALTO if t < 55 else 1.0,
                    lambda t: 0.0 if t < 55 else 60.0,
                )
            )
        )
    )
    assert minuto.mean_value == pytest.approx(5.0, rel=0.03)
    assert error(media_por_muestra(minuto), 5.0) > 0.7


def test_la_media_temporal_es_la_integral_entre_los_segundos_cubiertos() -> None:
    """Con tramos conocidos a mano, la media es exactamente `integral / seconds`.

    Tres muestras: 10 que dura 4 s (hasta el punto medio con la siguiente, en el 8), 30 que
    dura 8 s (del 4 al 12) y 50 pendiente. `(10x4 + 30x8) / 12 = 23,33`; la media por
    muestra seria 30.
    """
    minuto = el_minuto(dict(agregar_por_minuto([en(0, 10.0), en(8, 30.0), en(16, 50.0)])))
    assert minuto.seconds == pytest.approx(12.0)
    assert minuto.integral == pytest.approx(280.0)
    assert minuto.mean_value == pytest.approx(280.0 / 12.0)
    assert (minuto.samples, minuto.sum_value) == (3, 90.0)


# --- El borde: la ultima muestra del lote y el estado entre lotes ---------------------


def test_la_ultima_muestra_de_un_lote_cierra_su_tramo_en_el_lote_siguiente() -> None:
    agregador = AgregadorPorMinuto()
    primero = el_minuto(dict(agregador.agregar([en(0, 10.0), en(2, 10.0)])))
    # La de las :00 cubre [0, 1); la de las :02 esta pendiente.
    assert primero.seconds == pytest.approx(1.0)
    assert agregador.pendientes == 1

    segundo = el_minuto(dict(agregador.agregar([en(4, 30.0)])))
    # Ahora :02 cubre [1, 3): dos segundos que llegan **sin muestra**, solo tiempo.
    assert segundo.samples == 1  # la de las :04
    assert segundo.seconds == pytest.approx(2.0)
    assert segundo.integral == pytest.approx(20.0)


def test_la_composicion_de_los_lotes_no_cambia_el_resultado() -> None:
    """Las mismas muestras en lotes de 1, de 7 y de 37 suman exactamente lo mismo que en
    uno solo. Es la propiedad que exige el estado entre lotes: sin el, `batch_size: 1`
    no cubriria ni un segundo."""
    muestras = list(serie([FPS_ALTO, FPS_BAJO, FPS_ALTO], [3.0, 7.0, 5.0]))
    entero = dict(AgregadorPorMinuto().agregar(muestras))

    for tamano in (1, 7, 37):
        agregador = AgregadorPorMinuto()
        sumado: dict[ClaveMinuto, Agregado] = {}
        for corte in range(0, len(muestras), tamano):
            for clave, delta in agregador.agregar(muestras[corte : corte + tamano]).items():
                sumado[clave] = sumado.get(clave, Agregado()).unido_con(delta)
        assert sumado.keys() == entero.keys()
        for clave in entero:
            uno, otro = entero[clave], sumado[clave]
            assert (uno.samples, uno.sum_value) == (otro.samples, otro.sum_value)
            assert (uno.min_value, uno.max_value) == (otro.min_value, otro.max_value)
            assert uno.seconds == pytest.approx(otro.seconds)
            assert uno.integral == pytest.approx(otro.integral)


def test_el_orden_dentro_del_lote_da_igual() -> None:
    muestras = [en(0, 10.0), en(8, 30.0), en(16, 50.0), en(20, 70.0)]
    ordenado = el_minuto(dict(agregar_por_minuto(muestras)))
    al_reves = el_minuto(dict(agregar_por_minuto(list(reversed(muestras)))))
    assert (ordenado.seconds, ordenado.integral) == (al_reves.seconds, al_reves.integral)
    assert (ordenado.samples, ordenado.sum_value) == (al_reves.samples, al_reves.sum_value)


def test_una_muestra_que_llega_tarde_cuenta_como_muestra_y_no_como_tiempo() -> None:
    """Reabrir un tramo ya escrito lo contaria dos veces."""
    agregador = AgregadorPorMinuto()
    agregador.agregar([en(0, 10.0), en(4, 10.0)])
    tarde = el_minuto(dict(agregador.agregar([en(2, 99.0)])))
    assert (tarde.samples, tarde.max_value) == (1, 99.0)
    assert tarde.seconds == 0.0


def test_la_ultima_muestra_de_la_ejecucion_no_tiene_tramo() -> None:
    """No se sabe cuanto duro: cuenta como muestra, no como tiempo, y la media del minuto
    con una sola muestra es su valor."""
    minuto = el_minuto(dict(agregar_por_minuto([en(30, 7.0)])))
    assert (minuto.samples, minuto.seconds) == (1, 0.0)
    assert minuto.mean_value == 7.0


def test_la_primera_muestra_de_una_serie_empieza_en_su_propio_timestamp() -> None:
    """Antes de ella no se estaba mirando: un worker que arranca a las :30 cubre 30 s."""
    muestras = [en(30 + i * 0.2, 4.0) for i in range(150)] + [en(60.0, 4.0)]
    minuto = el_minuto(dict(agregar_por_minuto(muestras)))
    assert minuto.seconds == pytest.approx(30.0)


def test_un_minuto_entero_muestreado_cubre_sesenta_segundos_y_ni_uno_mas() -> None:
    minutos = por_minuto(dict(agregar_por_minuto(serie([FPS_ALTO] * 3, [1.0, 2.0, 3.0]))))
    assert minutos[INICIO].seconds == pytest.approx(60.0)
    assert minutos[INICIO + MINUTO].seconds == pytest.approx(60.0)
    # El ultimo: su ultima muestra (:59,8) queda pendiente sin tramo, y la anterior cubre
    # solo hasta el punto medio con ella (:59,7). Faltan 0,3 s, que llegaran con la
    # siguiente muestra o no llegaran nunca.
    assert minutos[INICIO + 2 * MINUTO].seconds == pytest.approx(59.7)


# --- Los huecos -----------------------------------------------------------------------


def test_un_hueco_no_se_rellena_con_el_ultimo_valor() -> None:
    """La fuente se cae diez minutos. La ultima medida de antes representa como mucho
    `HUECO_MAXIMO_S`, la primera de despues igual, y los minutos de en medio no existen:
    la grafica ensena un agujero, que es lo que hubo."""
    muestras = [en(30, 8.0), en(10 * 60 + 30, 9.0), en(10 * 60 + 31, 9.0)]
    minutos = por_minuto(dict(agregar_por_minuto(muestras)))

    assert sorted(minutos) == [INICIO, INICIO + 10 * MINUTO]
    assert minutos[INICIO].seconds == pytest.approx(HUECO_MAXIMO_S)
    assert minutos[INICIO].mean_value == 8.0
    despues = minutos[INICIO + 10 * MINUTO]
    assert despues.seconds == pytest.approx(HUECO_MAXIMO_S + 0.5)


def test_el_tope_del_hueco_se_recorta_ademas_al_minuto() -> None:
    """Una muestra a 5 s de la frontera y un hueco detras: cubre esos 5 s, no 15."""
    muestras = [en(55, 8.0), en(5 * 60, 9.0), en(5 * 60 + 1, 9.0)]
    minutos = por_minuto(dict(agregar_por_minuto(muestras)))
    assert minutos[INICIO].seconds == pytest.approx(5.0)
    assert sorted(minutos) == [INICIO, INICIO + 5 * MINUTO]


def test_una_cadencia_lenta_pero_dentro_del_tope_cubre_el_minuto_entero() -> None:
    """El analizador de aparcamiento a 0,5 fps, con la maquina cargada: una muestra cada
    10 s sigue cubriendo los 60 s."""
    muestras = [en(i * 10, 3.0) for i in range(7)]
    assert el_minuto(dict(agregar_por_minuto(muestras))).seconds == pytest.approx(60.0)


def test_un_tope_de_hueco_imposible_se_rechaza() -> None:
    with pytest.raises(ValueError, match="hueco_maximo_s"):
        AgregadorPorMinuto(hueco_maximo_s=0)


# --- Las claves y la union ------------------------------------------------------------


def test_dos_etiquetas_distintas_son_dos_series_distintas() -> None:
    """Es lo que permite "vehiculos por minuto por sentido" sin inventar nombres, y cada
    serie lleva su propio tramo pendiente."""
    muestras = [
        metrica(INICIO, 3.0, direction="norte"),
        metrica(INICIO, 5.0, direction="sur"),
        metrica(INICIO + timedelta(seconds=2), 3.0, direction="norte"),
    ]
    agregados = agregar_por_minuto(muestras)
    assert len(agregados) == 2
    norte = next(a for k, a in agregados.items() if "norte" in k.labels)
    sur = next(a for k, a in agregados.items() if "sur" in k.labels)
    assert (norte.samples, norte.seconds) == (2, pytest.approx(1.0))
    assert (sur.samples, sur.seconds) == (1, 0.0)


def test_el_orden_en_que_se_escriben_las_etiquetas_no_crea_una_serie_nueva() -> None:
    """`{"a": ..., "b": ...}` y `{"b": ..., "a": ...}` son el mismo minuto, no dos."""
    uno = metrica(INICIO, 1.0, line="principal", direction="norte")
    otro = metrica(INICIO + timedelta(seconds=1), 3.0, direction="norte", line="principal")
    agregados = agregar_por_minuto([uno, otro])
    assert len(agregados) == 1
    assert next(iter(agregados.values())).samples == 2


def test_unir_dos_trozos_del_mismo_minuto_suma_las_dos_familias() -> None:
    """Es lo que hace que reescribir un minuto acumule en vez de reemplazar."""
    uno = Agregado(
        samples=2, sum_value=30.0, min_value=10.0, max_value=20.0, seconds=3.0, integral=45.0
    )
    otro = Agregado(
        samples=3, sum_value=15.0, min_value=2.0, max_value=8.0, seconds=1.0, integral=5.0
    )
    unido = uno.unido_con(otro)
    assert (unido.samples, unido.sum_value) == (5, 45.0)
    assert (unido.min_value, unido.max_value) == (2.0, 20.0)
    assert (unido.seconds, unido.integral) == (4.0, 50.0)
    assert unido.mean_value == 12.5


def test_la_media_acumulada_no_es_la_media_de_las_medias() -> None:
    """Un trozo con media 15 sobre 3 s y otro con media 5 sobre 1 s: la media es 12,5, no
    10. Es el motivo por el que la tabla guarda la integral y no solo la media."""
    uno = Agregado(
        samples=1, sum_value=15.0, min_value=15.0, max_value=15.0, seconds=3.0, integral=45.0
    )
    otro = Agregado(
        samples=1, sum_value=5.0, min_value=5.0, max_value=5.0, seconds=1.0, integral=5.0
    )
    assert uno.unido_con(otro).mean_value == 12.5


def test_unir_con_un_trozo_sin_muestras_no_toca_el_minimo_ni_el_maximo() -> None:
    """El tramo de una muestra que llego en el lote anterior trae tiempo y no muestras: no
    puede arrastrar un cero al minimo."""
    solo_tiempo = Agregado(seconds=2.0, integral=14.0)
    lleno = Agregado(samples=1, sum_value=7.0, min_value=7.0, max_value=7.0)
    for unido in (solo_tiempo.unido_con(lleno), lleno.unido_con(solo_tiempo)):
        assert (unido.min_value, unido.max_value) == (7.0, 7.0)
        assert unido.mean_value == 7.0


def test_agregar_nada_no_da_ningun_minuto() -> None:
    assert agregar_por_minuto([]) == {}
