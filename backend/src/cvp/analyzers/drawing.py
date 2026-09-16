"""Primitivas de dibujo para `Analyzer.annotate`, compartidas por los analizadores.

Tres reglas que valen para todo lo que se pinta aqui, y que son el motivo de que exista
el modulo en vez de llamar a `cv2` desde cada analizador:

- **Nunca se pinta sobre el array que se recibe.** `Frame.image` no se copia entre bloques
  (contrato de #2) y un analizador que dibujara sobre el pasaria las lineas al siguiente
  analizador y al `preview`. `copia_para_anotar` es el unico punto de entrada, y devuelve
  un array nuevo y contiguo.
- **Todas las coordenadas llegan en pixeles y en float**, y el redondeo a entero que exige
  OpenCV se hace aqui, en un solo sitio.
- **El texto lleva fondo.** Sobre asfalto claro o sobre un capo blanco, un texto sin fondo
  no se lee, y una anotacion que no se lee no sirve para la comprobacion visual.

Los colores son BGR, que es lo que entrega OpenCV y lo que el core no convierte.
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

__all__ = [
    "AMARILLO",
    "AMBAR",
    "BLANCO",
    "CIAN",
    "GRIS",
    "NEGRO",
    "ROJO",
    "VERDE",
    "Color",
    "copia_para_anotar",
    "dibujar_etiqueta",
    "dibujar_linea",
    "dibujar_poligono_relleno",
    "dibujar_punto",
]

Color = tuple[int, int, int]

# BGR. Saturados a proposito: se ven sobre asfalto, sobre chapa y sobre un JPEG a 85.
AMARILLO: Color = (0, 220, 255)
# Ambar y gris son los dos estados intermedios de una plaza (#11): "esperando la
# histeresis" y "todavia no se". El ambar se distingue del amarillo de las lineas a proposito.
AMBAR: Color = (0, 150, 255)
BLANCO: Color = (255, 255, 255)
CIAN: Color = (255, 220, 0)
GRIS: Color = (160, 160, 160)
NEGRO: Color = (0, 0, 0)
ROJO: Color = (60, 60, 255)
VERDE: Color = (80, 230, 80)

_FUENTE = cv2.FONT_HERSHEY_SIMPLEX


def _entero(punto: tuple[float, float]) -> tuple[int, int]:
    return (int(round(punto[0])), int(round(punto[1])))


def copia_para_anotar(image: np.ndarray) -> np.ndarray:
    """Un array nuevo sobre el que pintar. El original queda intacto byte a byte."""
    return np.ascontiguousarray(image, dtype=np.uint8).copy()


def dibujar_linea(
    imagen: np.ndarray,
    p1: tuple[float, float],
    p2: tuple[float, float],
    *,
    color: Color = AMARILLO,
    grosor: int = 3,
) -> None:
    cv2.line(imagen, _entero(p1), _entero(p2), color, grosor, lineType=cv2.LINE_AA)


def dibujar_poligono_relleno(
    imagen: np.ndarray,
    puntos: Sequence[tuple[float, float]],
    *,
    color: Color,
    opacidad: float = 0.35,
    grosor_borde: int = 2,
) -> None:
    """Un poligono relleno semitransparente con borde opaco del mismo color.

    Semitransparente para que el coche siga viendose debajo: una plaza pintada en rojo
    solido tapa justo lo que hace falta mirar para saber si el rojo esta bien. La mezcla
    se hace solo dentro del rectangulo envolvente del poligono, no sobre el fotograma
    entero, para que veinte plazas no cuesten veinte copias de 6 MB.
    """
    if not 0.0 <= opacidad <= 1.0:
        raise ValueError(f"opacidad es una fraccion 0-1, no {opacidad!r}")
    enteros = np.array([_entero(p) for p in puntos], dtype=np.int32)
    alto, ancho = imagen.shape[:2]
    x1, y1 = max(0, int(enteros[:, 0].min())), max(0, int(enteros[:, 1].min()))
    x2, y2 = min(ancho, int(enteros[:, 0].max()) + 1), min(alto, int(enteros[:, 1].max()) + 1)
    if x2 > x1 and y2 > y1 and opacidad > 0.0:
        recorte = imagen[y1:y2, x1:x2]
        capa = recorte.copy()
        cv2.fillPoly(capa, [enteros - np.array([x1, y1], dtype=np.int32)], color)
        cv2.addWeighted(capa, opacidad, recorte, 1.0 - opacidad, 0.0, dst=recorte)
    if grosor_borde > 0:
        cv2.polylines(
            imagen,
            [enteros],
            isClosed=True,
            color=color,
            thickness=grosor_borde,
            lineType=cv2.LINE_AA,
        )


def dibujar_punto(
    imagen: np.ndarray,
    punto: tuple[float, float],
    *,
    color: Color = ROJO,
    radio: int = 6,
) -> None:
    """Un circulo relleno con borde blanco, para que se vea sobre cualquier fondo."""
    centro = _entero(punto)
    cv2.circle(imagen, centro, radio + 2, BLANCO, -1, lineType=cv2.LINE_AA)
    cv2.circle(imagen, centro, radio, color, -1, lineType=cv2.LINE_AA)


def dibujar_etiqueta(
    imagen: np.ndarray,
    texto: str,
    origen: tuple[float, float],
    *,
    color_texto: Color = NEGRO,
    color_fondo: Color = AMARILLO,
    escala: float = 0.7,
    grosor: int = 2,
) -> None:
    """Texto con una caja de fondo detras. `origen` es la esquina superior izquierda.

    Si la caja se saliera del fotograma por la derecha o por abajo, se desplaza hacia
    dentro: una etiqueta que se dibuja fuera de la imagen no se ve, y OpenCV la recorta en
    silencio.
    """
    (ancho, alto), base = cv2.getTextSize(texto, _FUENTE, escala, grosor)
    margen = 6
    alto_caja = alto + base + 2 * margen
    ancho_caja = ancho + 2 * margen
    alto_imagen, ancho_imagen = imagen.shape[:2]
    x, y = _entero(origen)
    x = max(0, min(x, ancho_imagen - ancho_caja))
    y = max(0, min(y, alto_imagen - alto_caja))
    cv2.rectangle(imagen, (x, y), (x + ancho_caja, y + alto_caja), color_fondo, -1)
    cv2.putText(
        imagen,
        texto,
        (x + margen, y + margen + alto),
        _FUENTE,
        escala,
        color_texto,
        grosor,
        lineType=cv2.LINE_AA,
    )
