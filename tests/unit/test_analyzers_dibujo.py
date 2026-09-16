"""Las primitivas de `annotate`: pintan sobre una copia y el texto se queda dentro del
fotograma.

Lo que se comprueba es lo que un analizador da por hecho al usarlas, no como dibuja
OpenCV: que el original queda intacto byte a byte, que algo cambia en la copia y que una
etiqueta pegada al borde no se pierde fuera de la imagen.
"""

from __future__ import annotations

import numpy as np
import pytest

from cvp.analyzers.drawing import (
    ROJO,
    VERDE,
    copia_para_anotar,
    dibujar_etiqueta,
    dibujar_linea,
    dibujar_poligono_relleno,
    dibujar_punto,
)


def lienzo() -> np.ndarray:
    return np.zeros((120, 200, 3), dtype=np.uint8)


# --- Poligonos rellenos: semitransparentes, con borde, y solo dentro de su rectangulo ----

CUADRADO = [(20.0, 20.0), (80.0, 20.0), (80.0, 80.0), (20.0, 80.0)]


def test_el_relleno_es_semitransparente_y_deja_ver_lo_de_debajo() -> None:
    imagen = np.full((120, 200, 3), 200, dtype=np.uint8)  # un fondo gris claro
    dibujar_poligono_relleno(imagen, CUADRADO, color=ROJO, opacidad=0.5, grosor_borde=0)
    dentro = imagen[50, 50]
    # Mezcla al 50 %: (200 + 60) / 2 en B y G, (200 + 255) / 2 en R. Ni el fondo ni el
    # color puro: se ve el coche y se ve el rojo.
    assert tuple(int(c) for c in dentro) == pytest.approx((130, 130, 227), abs=2)
    assert tuple(imagen[10, 10]) == (200, 200, 200), "fuera del poligono no se toca nada"
    assert tuple(imagen[110, 190]) == (200, 200, 200)


def test_el_borde_es_opaco_y_del_mismo_color() -> None:
    imagen = lienzo()
    dibujar_poligono_relleno(imagen, CUADRADO, color=VERDE, opacidad=0.0, grosor_borde=2)
    assert imagen[20, 50].any(), "el borde superior pasa por (50, 20)"
    assert not imagen[50, 50].any(), "con opacidad 0 el interior queda como estaba"
    assert imagen[20, 50][1] >= imagen[20, 50][0], "es verde: el canal G domina"


def test_un_poligono_que_se_sale_del_fotograma_se_recorta_sin_reventar() -> None:
    imagen = lienzo()
    fuera = [(150.0, 100.0), (260.0, 100.0), (260.0, 160.0), (150.0, 160.0)]
    dibujar_poligono_relleno(imagen, fuera, color=ROJO)
    assert imagen[110, 190].any()
    assert not imagen[50, 50].any()


def test_una_opacidad_fuera_de_0_1_es_un_error() -> None:
    with pytest.raises(ValueError, match="opacidad"):
        dibujar_poligono_relleno(lienzo(), CUADRADO, color=ROJO, opacidad=1.5)


def test_la_copia_es_un_array_nuevo_contiguo_e_igual_al_original() -> None:
    original = lienzo()
    copia = copia_para_anotar(original)
    assert copia is not original
    assert not np.shares_memory(copia, original)
    assert copia.flags["C_CONTIGUOUS"]
    assert np.array_equal(copia, original)
    assert copia.dtype == np.uint8


def test_una_vista_no_contigua_tambien_se_copia_a_algo_sobre_lo_que_opencv_puede_pintar() -> None:
    vista = lienzo()[::2, ::2]
    assert not vista.flags["C_CONTIGUOUS"]
    copia = copia_para_anotar(vista)
    assert copia.flags["C_CONTIGUOUS"]
    dibujar_linea(copia, (0, 0), (50, 20))


def test_dibujar_cambia_la_copia_y_solo_la_copia() -> None:
    original = lienzo()
    antes = original.copy()
    copia = copia_para_anotar(original)

    dibujar_linea(copia, (10.4, 60.6), (190.0, 60.0))
    dibujar_punto(copia, (100.0, 30.0))
    dibujar_etiqueta(copia, "principal", (10.0, 10.0))

    assert np.array_equal(original, antes), "el original tiene que quedar intacto byte a byte"
    assert copia.any(), "en la copia se tiene que haber pintado algo"
    assert copia[60, 100].any(), "la linea pasa por (100, 60)"
    assert copia[30, 100].any(), "el punto esta en (100, 30)"


def test_una_etiqueta_pegada_al_borde_se_mete_dentro_del_fotograma() -> None:
    imagen = lienzo()
    # Origen fuera de la imagen por la derecha y por abajo: sin desplazarla, OpenCV la
    # recortaria en silencio y no se veria nada.
    dibujar_etiqueta(imagen, "sur: 12", (195.0, 118.0))
    assert imagen.any()
    # Y todo lo pintado esta dentro: la ultima fila y la ultima columna pueden tener fondo,
    # pero tiene que haber texto legible antes del borde.
    filas_con_algo = np.flatnonzero(imagen.any(axis=(1, 2)))
    columnas_con_algo = np.flatnonzero(imagen.any(axis=(0, 2)))
    assert filas_con_algo.max() <= 119
    assert columnas_con_algo.max() <= 199
    assert filas_con_algo.min() < 100, "la caja se ha desplazado hacia arriba"
    assert columnas_con_algo.min() < 150, "la caja se ha desplazado hacia la izquierda"
