"""Las primitivas de `annotate`: pintan sobre una copia y el texto se queda dentro del
fotograma.

Lo que se comprueba es lo que un analizador da por hecho al usarlas, no como dibuja
OpenCV: que el original queda intacto byte a byte, que algo cambia en la copia y que una
etiqueta pegada al borde no se pierde fuera de la imagen.
"""

from __future__ import annotations

import numpy as np

from cvp.analyzers.drawing import (
    copia_para_anotar,
    dibujar_etiqueta,
    dibujar_linea,
    dibujar_punto,
)


def lienzo() -> np.ndarray:
    return np.zeros((120, 200, 3), dtype=np.uint8)


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
