"""Las piezas reutilizables de las fuentes en directo: la ranura y la escalera de backoff.

Viven en `cvp.sources.live` y no dentro del connector de YouTube porque RTSP y webcam
tienen exactamente el mismo problema: leer a la velocidad del directo y analizar a la de
la maquina son dos ritmos distintos, y quien los concilia es esta ranura.

Todo lo de aqui es instantaneo: la ranura se prueba con hilos de verdad pero sin dormir
ni una vez, esperando sobre eventos en vez de sobre el reloj.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from cvp.sources.errors import SourceError
from cvp.sources.live import LatestFrameSlot, espera_de_backoff

INICIO = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
# Tope de seguridad de las esperas sobre eventos: si el codigo es correcto se cumplen en
# microsegundos, y si no lo es el test falla en vez de colgar la suite.
ESPERA_MAXIMA_S = 2.0


def _imagen(valor: int) -> np.ndarray:
    imagen = np.zeros((4, 4, 3), dtype=np.uint8)
    imagen[0, 0, 0] = valor
    return imagen


# --- La ranura del ultimo fotograma ---------------------------------------------------


def test_la_ranura_vacia_no_entrega_nada() -> None:
    assert LatestFrameSlot().esperar(timeout=0) is None


def test_la_ranura_entrega_el_ultimo_fotograma_publicado() -> None:
    ranura = LatestFrameSlot()
    ranura.publicar(_imagen(7), INICIO)
    instantanea = ranura.esperar(timeout=0)
    assert instantanea is not None
    assert instantanea.image[0, 0, 0] == 7
    assert instantanea.read_at == INICIO
    assert instantanea.sequence == 1


def test_la_ranura_pisa_el_fotograma_anterior_en_vez_de_encolarlo() -> None:
    # Es la diferencia entre este connector y uno que encolara: los intermedios se
    # pierden a proposito, que es lo que mantiene fresco lo que sale.
    ranura = LatestFrameSlot()
    for numero in range(1, 51):
        ranura.publicar(_imagen(numero), INICIO + timedelta(milliseconds=numero))
    instantanea = ranura.esperar(timeout=0)
    assert instantanea is not None
    assert instantanea.image[0, 0, 0] == 50
    assert instantanea.sequence == 50


def test_la_ranura_no_devuelve_dos_veces_el_mismo_fotograma() -> None:
    ranura = LatestFrameSlot()
    ranura.publicar(_imagen(1), INICIO)
    primera = ranura.esperar(timeout=0)
    assert primera is not None
    # "Ya te di este" y "todavia no hay uno nuevo" son el mismo caso desde fuera, y los
    # distingue el numero de secuencia: sin el, el consumidor repetiria fotogramas.
    assert ranura.esperar(primera.sequence, timeout=0) is None


def test_esperar_se_desbloquea_en_cuanto_llega_un_fotograma_nuevo() -> None:
    ranura = LatestFrameSlot()
    ranura.publicar(_imagen(1), INICIO)
    recibidos: list[int] = []

    def consumir() -> None:
        instantanea = ranura.esperar(1)
        assert instantanea is not None
        recibidos.append(int(instantanea.image[0, 0, 0]))

    consumidor = threading.Thread(target=consumir, daemon=True)
    consumidor.start()
    ranura.publicar(_imagen(2), INICIO)
    consumidor.join(ESPERA_MAXIMA_S)
    assert recibidos == [2]


def test_cerrar_despierta_a_quien_espera_en_vez_de_dejarlo_colgado() -> None:
    ranura = LatestFrameSlot()
    resultado: list[object] = []
    consumidor = threading.Thread(target=lambda: resultado.append(ranura.esperar()), daemon=True)
    consumidor.start()
    ranura.cerrar()
    consumidor.join(ESPERA_MAXIMA_S)
    assert resultado == [None]


def test_publicar_despues_de_cerrar_no_reabre_la_ranura() -> None:
    ranura = LatestFrameSlot()
    ranura.cerrar()
    ranura.publicar(_imagen(1), INICIO)
    assert ranura.esperar(timeout=0) is None


def test_la_ranura_relanza_el_error_con_el_que_murio_el_hilo_lector() -> None:
    # El hilo lector no puede lanzar hacia el generador del consumidor: son pilas
    # distintas. Sin esto, quedarse sin reintentos seria indistinguible de ir lento.
    ranura = LatestFrameSlot()
    ranura.fallar(SourceError("se acabaron los reintentos"))
    with pytest.raises(SourceError, match="reintentos"):
        ranura.esperar(timeout=0)


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
