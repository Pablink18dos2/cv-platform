"""La agregacion por minuto, que es donde esta el riesgo de #9.

El criterio que separa una agregacion correcta de una que **parece** correcta mientras la
maquina va sobrada esta en `test_los_agregados_no_dependen_de_la_cadencia`: desde #23 la
tasa efectiva se adapta a lo que el analisis sostiene (QA midio 5,0 -> 2,74 -> 5,0 fps en
una misma ejecucion), asi que el numero de muestras de un minuto **no es constante**.
Cualquier calculo que divida por muestras para obtener tiempo sale mal en cuanto la tasa se
mueve, y se mueve.

Son funciones puras: ni base de datos, ni fotogramas, ni reloj.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from cvp.core import Metric
from cvp.storage.aggregation import Agregado, ClaveMinuto, agregar_por_minuto, minuto_de

INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)

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


def serie(cadencias: Sequence[float], valor_del_minuto: Sequence[float]) -> Iterator[Metric]:
    """Una muestra cada `1/fps` segundos, con la cadencia de cada minuto y su valor.

    Es la misma serie medida de dos maneras: lo que cambia entre una llamada y otra es
    cuantas veces se mira, no lo que se esta midiendo.
    """
    for i, (fps, valor) in enumerate(zip(cadencias, valor_del_minuto, strict=True)):
        paso = timedelta(seconds=1.0 / fps)
        marca = INICIO + timedelta(minutes=i)
        fin = INICIO + timedelta(minutes=i + 1)
        while marca < fin:
            yield metrica(marca, valor)
            marca += paso


def por_minuto(agregados: dict[ClaveMinuto, Agregado]) -> dict[datetime, Agregado]:
    return {clave.minute: agregado for clave, agregado in agregados.items()}


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
    muestras = [
        metrica(INICIO + timedelta(seconds=10), 1.0),
        metrica(INICIO + timedelta(minutes=7, seconds=10), 2.0),
    ]
    assert sorted(por_minuto(dict(agregar_por_minuto(muestras)))) == [
        INICIO,
        INICIO + timedelta(minutes=7),
    ]


def test_un_lote_que_cruza_una_frontera_de_minuto_se_reparte_entre_los_dos() -> None:
    """Es el caso que un `INSERT ... ON CONFLICT` mal escrito resuelve mal y nadie mira.

    Cuatro muestras, dos a cada lado de las 14:21:00, en el mismo lote y en la misma
    llamada. El lote no tiene minuto: lo tiene cada muestra.
    """
    lote = [
        metrica(INICIO + timedelta(seconds=58), 10.0),
        metrica(INICIO + timedelta(seconds=59), 20.0),
        metrica(INICIO + timedelta(seconds=60), 30.0),
        metrica(INICIO + timedelta(seconds=61), 40.0),
    ]
    minutos = por_minuto(dict(agregar_por_minuto(lote)))

    assert sorted(minutos) == [INICIO, INICIO + timedelta(minutes=1)]
    assert (minutos[INICIO].samples, minutos[INICIO].mean_value) == (2, 15.0)
    siguiente = minutos[INICIO + timedelta(minutes=1)]
    assert (siguiente.samples, siguiente.mean_value) == (2, 35.0)


def test_la_composicion_del_lote_no_cambia_el_resultado() -> None:
    """Las mismas muestras repartidas en otros lotes dan exactamente lo mismo.

    Es la propiedad que hace que agrupar escrituras sea seguro: si el resultado dependiera
    de donde cae la frontera del lote, el sink estaria inventando datos al agrupar.
    """
    muestras = list(serie([FPS_ALTO, FPS_ALTO], [3.0, 7.0]))
    entero = dict(agregar_por_minuto(muestras))
    troceado: dict[ClaveMinuto, Agregado] = {}
    for corte in range(0, len(muestras), 37):
        for clave, agregado in agregar_por_minuto(muestras[corte : corte + 37]).items():
            troceado[clave] = troceado.get(clave, Agregado()).unido_con(agregado)

    assert {k: (v.samples, v.sum_value) for k, v in entero.items()} == {
        k: (v.samples, v.sum_value) for k, v in troceado.items()
    }


# --- La cadencia variable, que es el criterio de este issue ---------------------------


def test_los_agregados_no_dependen_de_la_cadencia() -> None:
    """La misma serie a cadencia variable da los mismos agregados que a cadencia constante.

    Cuatro minutos. En una version se mide siempre a 5 fps; en la otra, la mitad de los
    minutos a 5 fps y la mitad a 2,74 fps, que es la secuencia que QA midio en #23 segun se
    cargaba la maquina. Lo que se esta midiendo es lo mismo en los dos casos.

    Y el numero de muestras **no** coincide: 300 frente a 165 en los minutos lentos. Ese es
    justamente el punto. Un agregado que dividiera por muestras para obtener tiempo daria un
    82 % de diferencia entre las dos columnas; el que normaliza por timestamps da cero.
    """
    valores = [12.0, 8.0, 15.0, 9.0]
    constante = por_minuto(dict(agregar_por_minuto(serie([FPS_ALTO] * 4, valores))))
    variable = por_minuto(dict(agregar_por_minuto(serie([FPS_ALTO, FPS_BAJO] * 2, valores))))

    assert sorted(constante) == sorted(variable)
    for minuto in constante:
        uno, otro = constante[minuto], variable[minuto]
        assert (uno.mean_value, uno.min_value, uno.max_value) == (
            otro.mean_value,
            otro.min_value,
            otro.max_value,
        )

    # Y que el test no es vacio: los minutos lentos tienen casi la mitad de muestras.
    lento = variable[INICIO + timedelta(minutes=1)]
    rapido = constante[INICIO + timedelta(minutes=1)]
    assert rapido.samples == 300
    assert lento.samples == 165
    assert rapido.samples / lento.samples == pytest.approx(1.82, abs=0.01)


def test_la_media_de_una_senal_que_varia_tampoco_depende_de_la_cadencia() -> None:
    """Con una rampa dentro del minuto, la media apenas se mueve al cambiar la cadencia.

    El error de muestreo es de decimas de por ciento; el que tendria un calculo hecho a base
    de contar fotogramas seria del 82 %. Los dos numeros estan en el mismo test a proposito:
    es la diferencia entre "distinto por como se muestrea" y "distinto por estar mal".
    """

    def rampa(cadencia: float) -> Agregado:
        paso = timedelta(seconds=1.0 / cadencia)
        muestras = []
        marca = INICIO
        while marca < INICIO + timedelta(minutes=1):
            segundo = (marca - INICIO).total_seconds()
            muestras.append(metrica(marca, 10.0 + segundo / 6.0))
            marca += paso
        return next(iter(agregar_por_minuto(muestras).values()))

    rapido, lento = rampa(FPS_ALTO), rampa(FPS_BAJO)
    assert lento.mean_value == pytest.approx(rapido.mean_value, rel=0.01)
    assert lento.min_value == pytest.approx(rapido.min_value, abs=0.1)
    assert lento.max_value == pytest.approx(rapido.max_value, rel=0.01)
    assert rapido.samples != lento.samples


# --- Las claves y la union ------------------------------------------------------------


def test_dos_etiquetas_distintas_son_dos_celdas_distintas() -> None:
    """Es lo que permite "vehiculos por minuto por sentido" sin inventar nombres."""
    muestras = [
        metrica(INICIO, 3.0, direction="norte"),
        metrica(INICIO, 5.0, direction="sur"),
    ]
    agregados = agregar_por_minuto(muestras)
    assert len(agregados) == 2
    assert {a.sum_value for a in agregados.values()} == {3.0, 5.0}


def test_el_orden_en_que_se_escriben_las_etiquetas_no_crea_una_celda_nueva() -> None:
    """`{"a": ..., "b": ...}` y `{"b": ..., "a": ...}` son el mismo minuto, no dos."""
    uno = metrica(INICIO, 1.0, line="principal", direction="norte")
    otro = metrica(INICIO, 3.0, direction="norte", line="principal")
    agregados = agregar_por_minuto([uno, otro])
    assert len(agregados) == 1
    assert next(iter(agregados.values())).samples == 2


def test_unir_dos_trozos_del_mismo_minuto_suma_y_ensancha() -> None:
    """Es lo que hace que reescribir un minuto acumule en vez de reemplazar."""
    uno = Agregado(samples=2, sum_value=30.0, min_value=10.0, max_value=20.0)
    otro = Agregado(samples=3, sum_value=15.0, min_value=2.0, max_value=8.0)
    unido = uno.unido_con(otro)
    assert (unido.samples, unido.sum_value) == (5, 45.0)
    assert (unido.min_value, unido.max_value) == (2.0, 20.0)
    assert unido.mean_value == 9.0


def test_unir_con_un_agregado_vacio_no_arrastra_un_cero_al_minimo() -> None:
    """Un `Agregado()` recien creado no ha visto ningun valor: su minimo no es 0, no hay."""
    vacio = Agregado()
    lleno = Agregado(samples=1, sum_value=7.0, min_value=7.0, max_value=7.0)
    assert vacio.unido_con(lleno).min_value == 7.0
    assert lleno.unido_con(vacio).min_value == 7.0


def test_agregar_nada_no_da_ningun_minuto() -> None:
    assert agregar_por_minuto([]) == {}
