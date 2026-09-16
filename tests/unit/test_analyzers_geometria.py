"""La geometria compartida de los analizadores: el lado de un punto, los poligonos y su
IoU con una caja, y la conversion a pixeles desde el primer fotograma.

Todo con numeros escritos a mano. Es lo que fija que el lado `+1` es el que queda a la
derecha de `p1 -> p2` visto en pantalla, que un punto sobre la linea es `0` y no un
redondeo hacia ningun lado, y que el IoU de una caja identica a la plaza es exactamente 1.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import pytest

from cvp.analyzers.geometry import (
    GeometriaPorFotograma,
    LineaDeConteo,
    LineaEnPixeles,
    MascaraDePoligono,
    Poligono,
    PoligonoEnPixeles,
    lado_de,
    rasterizar_poligono,
)
from cvp.core import BBox, Frame

INICIO = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)


def fotograma(ancho: int, alto: int, indice: int = 0) -> Frame:
    return Frame(image=np.zeros((alto, ancho, 3), dtype=np.uint8), index=indice, timestamp=INICIO)


def linea(**cambios: object) -> LineaDeConteo:
    campos: dict[str, object] = {
        "id": "principal",
        "p1": (0.0, 0.5),
        "p2": (1.0, 0.5),
        "positive_direction": "sur",
        "negative_direction": "norte",
    }
    return LineaDeConteo(**(campos | cambios))  # type: ignore[arg-type]


# --- El lado de un punto ----------------------------------------------------------------


def test_el_lado_positivo_es_el_de_la_derecha_de_p1_a_p2_visto_en_pantalla() -> None:
    # Linea horizontal de izquierda a derecha: a su derecha (en pantalla) queda lo de abajo,
    # que en coordenadas de imagen es y mayor.
    assert lado_de((0, 100), (200, 100), (50, 150)) == 1
    assert lado_de((0, 100), (200, 100), (50, 50)) == -1
    # La misma linea escrita al reves invierte los lados: el sentido lo fija p1 -> p2.
    assert lado_de((200, 100), (0, 100), (50, 150)) == -1


def test_con_una_linea_vertical_hacia_abajo_el_lado_positivo_es_la_izquierda() -> None:
    # Caminando hacia abajo por la pantalla, la mano derecha apunta a la izquierda de la imagen.
    assert lado_de((100, 0), (100, 200), (50, 100)) == 1
    assert lado_de((100, 0), (100, 200), (150, 100)) == -1


def test_un_punto_exactamente_sobre_la_linea_es_cero_y_no_un_lado() -> None:
    assert lado_de((0, 100), (200, 100), (50, 100)) == 0
    # Con pixeles enteros sobre una diagonal tambien pasa, y es el caso que importa.
    assert lado_de((0, 0), (100, 100), (37, 37)) == 0
    assert lado_de((0, 0), (100, 100), (37, 38)) != 0


@pytest.mark.parametrize(
    ("punto", "esperado"),
    [((0.2, 0.2), -1), ((0.8, 0.8), 1), ((0.5, 0.5), 0)],
    ids=["arriba-izquierda", "abajo-derecha", "sobre-la-linea"],
)
def test_una_linea_diagonal_parte_el_fotograma_en_dos(
    punto: tuple[float, float], esperado: int
) -> None:
    assert lado_de((0.1, 0.9), (0.9, 0.1), punto) == esperado


# --- LineaDeConteo: el argumento plano del analizador ----------------------------------


def test_una_linea_se_convierte_a_pixeles_sin_redondear() -> None:
    en_pixeles = linea(p1=(0.1, 0.25), p2=(0.9, 0.75)).to_pixels(1281, 721)
    assert isinstance(en_pixeles, LineaEnPixeles)
    assert en_pixeles.p1 == pytest.approx((128.1, 180.25))
    assert en_pixeles.p2 == pytest.approx((1152.9, 540.75))
    assert (en_pixeles.id, en_pixeles.positive_direction, en_pixeles.negative_direction) == (
        "principal",
        "sur",
        "norte",
    )


def test_las_mismas_fracciones_dan_el_mismo_lado_en_cualquier_resolucion() -> None:
    """Es el motivo de que la geometria vaya en fracciones: la fuente sirve el mismo
    directo en 1080p y en 720p, y un vehiculo en la misma fraccion del fotograma tiene que
    caer en el mismo lado."""
    original = linea(p1=(0.1, 0.9), p2=(0.9, 0.1))
    for ancho, alto in ((1920, 1080), (1280, 720), (640, 360)):
        en_pixeles = original.to_pixels(ancho, alto)
        assert en_pixeles.lado_de((0.2 * ancho, 0.2 * alto)) == -1
        assert en_pixeles.lado_de((0.8 * ancho, 0.8 * alto)) == 1


def test_el_sentido_lleva_el_nombre_del_lado_al_que_se_pasa() -> None:
    en_pixeles = linea().to_pixels(100, 100)
    assert en_pixeles.sentido_hacia(1) == "sur"
    assert en_pixeles.sentido_hacia(-1) == "norte"
    with pytest.raises(ValueError, match="sobre la linea"):
        en_pixeles.sentido_hacia(0)


def test_el_punto_medio_es_donde_se_dibuja_la_etiqueta() -> None:
    assert linea(p1=(0.0, 0.0), p2=(1.0, 0.5)).to_pixels(200, 100).punto_medio == (100.0, 25.0)


@pytest.mark.parametrize(
    "cambios",
    [
        {"id": " "},
        {"p2": (0.0, 0.5)},
        {"p1": (1.2, 0.5)},
        {"negative_direction": "sur"},
        {"positive_direction": ""},
    ],
    ids=["sin-id", "longitud-cero", "fuera-de-0-1", "sentidos-iguales", "sentido-vacio"],
)
def test_una_linea_de_conteo_mal_escrita_falla_al_construirla(cambios: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        linea(**cambios)


@pytest.mark.parametrize(("ancho", "alto"), [(0, 100), (100, 0), (-1, -1)])
def test_convertir_sin_dimensiones_falla(ancho: int, alto: int) -> None:
    with pytest.raises(ValueError, match="pixeles"):
        linea().to_pixels(ancho, alto)


# --- Poligono: la plaza en fracciones, y en pixeles ----------------------------------------


def cuadrado(x1: float, y1: float, x2: float, y2: float, *, id: str = "d01") -> Poligono:
    return Poligono(id=id, puntos=((x1, y1), (x2, y1), (x2, y2), (x1, y2)))


def test_un_poligono_se_convierte_a_pixeles_sin_redondear_y_conserva_el_id() -> None:
    en_pixeles = cuadrado(0.1, 0.25, 0.9, 0.75).to_pixels(1281, 721)
    assert isinstance(en_pixeles, PoligonoEnPixeles)
    assert en_pixeles.id == "d01"
    assert en_pixeles.puntos[0] == pytest.approx((128.1, 180.25))
    assert en_pixeles.puntos[2] == pytest.approx((1152.9, 540.75))
    assert en_pixeles.centroide == pytest.approx((640.5, 360.5))
    assert en_pixeles.rectangulo_envolvente == pytest.approx((128.1, 180.25, 1152.9, 540.75))


def test_un_poligono_acepta_listas_y_las_guarda_como_tuplas_de_float() -> None:
    """Es lo que llega de `zones.json`: listas de listas, con enteros donde el JSON los
    escriba (`[0, 1]`)."""
    p = Poligono(id="z", puntos=[[0, 0], [1, 0], [1, 1]])  # type: ignore[arg-type]
    assert p.puntos == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))


@pytest.mark.parametrize(
    ("cambios", "mensaje"),
    [
        ({"id": " "}, "necesita id"),
        ({"puntos": ((0.1, 0.1), (0.5, 0.5))}, "al menos 3 puntos y tiene 2"),
        ({"puntos": ((0.1, 0.1), (1.2, 0.1), (0.5, 0.5))}, "punto 1 de la zona 'd01'"),
        ({"puntos": ((0.1, 0.1), (0.5, 0.5), (0.9, 0.9))}, "area cero"),
    ],
    ids=["sin-id", "dos-puntos", "fuera-de-0-1", "colineal"],
)
def test_un_poligono_mal_escrito_falla_al_construirlo_diciendo_que(
    cambios: dict[str, object], mensaje: str
) -> None:
    campos: dict[str, object] = {"id": "d01", "puntos": ((0.1, 0.1), (0.5, 0.1), (0.5, 0.5))}
    with pytest.raises(ValueError, match=mensaje):
        Poligono(**(campos | cambios))  # type: ignore[arg-type]


@pytest.mark.parametrize(("ancho", "alto"), [(0, 100), (100, 0)])
def test_convertir_un_poligono_sin_dimensiones_falla(ancho: int, alto: int) -> None:
    with pytest.raises(ValueError, match="pixeles"):
        cuadrado(0.1, 0.1, 0.5, 0.5).to_pixels(ancho, alto)


# --- Rasterizar y medir el IoU con una caja -------------------------------------------------


def test_un_rectangulo_rasterizado_ocupa_los_pixeles_de_sus_vertices_incluidos() -> None:
    """El convenio de `fillPoly`: un vertice entero cae dentro, asi que de (10, 10) a
    (20, 30) son 11 x 21 pixeles. Es lo que `iou_con` da por hecho para la caja."""
    mascara = rasterizar_poligono(cuadrado(0.1, 0.1, 0.2, 0.3).to_pixels(100, 100), 100, 100)
    assert isinstance(mascara, MascaraDePoligono)
    assert (mascara.x1, mascara.y1, mascara.x2, mascara.y2) == (10, 10, 21, 31)
    assert mascara.area == 11 * 21
    assert mascara.mascara.dtype == bool and mascara.mascara.all()


def test_la_mascara_va_recortada_al_rectangulo_envolvente_y_no_al_fotograma() -> None:
    triangulo = Poligono(id="t", puntos=((0.5, 0.5), (0.6, 0.5), (0.5, 0.6))).to_pixels(1000, 1000)
    mascara = rasterizar_poligono(triangulo, 1000, 1000)
    assert mascara.mascara.shape == (101, 101)
    # La mitad de un cuadrado de 101 x 101, mas o menos la diagonal.
    assert 101 * 101 / 2 <= mascara.area <= 101 * 101 / 2 + 101
    assert not mascara.mascara[100, 100], "la esquina opuesta a la hipotenusa esta fuera"
    assert mascara.mascara[0, 0]


def test_un_poligono_que_se_sale_del_fotograma_se_recorta_y_uno_de_fuera_queda_vacio() -> None:
    recortado = rasterizar_poligono(cuadrado(0.9, 0.9, 1.0, 1.0).to_pixels(100, 100), 100, 100)
    assert (recortado.x2, recortado.y2) == (100, 100)
    assert recortado.area == 10 * 10
    fuera = rasterizar_poligono(
        PoligonoEnPixeles(id="f", puntos=((200.0, 200.0), (300.0, 200.0), (300.0, 300.0))), 100, 100
    )
    assert fuera.area == 0
    assert fuera.iou_con(BBox(200, 200, 300, 300)) == 0.0


def test_una_caja_identica_a_la_plaza_da_iou_exactamente_1() -> None:
    mascara = rasterizar_poligono(cuadrado(0.1, 0.1, 0.2, 0.3).to_pixels(100, 100), 100, 100)
    assert mascara.iou_con(BBox(10, 10, 20, 30)) == 1.0


def test_el_iou_es_interseccion_partido_por_union_en_pixeles() -> None:
    """Numeros a mano: plaza de 11 x 21 = 231 pixeles; caja de (10, 10) a (20, 20), o sea
    11 x 11 = 121, toda dentro. IoU = 121 / 231."""
    mascara = rasterizar_poligono(cuadrado(0.1, 0.1, 0.2, 0.3).to_pixels(100, 100), 100, 100)
    assert mascara.iou_con(BBox(10, 10, 20, 20)) == pytest.approx(121 / 231)
    # Una caja que la desborda por los cuatro lados: 21 x 31 = 651, y la interseccion es
    # la plaza entera. IoU = 231 / 651.
    assert mascara.iou_con(BBox(5, 5, 25, 35)) == pytest.approx(231 / 651)
    # Una que solo la toca por una esquina: 1 pixel de interseccion.
    assert mascara.iou_con(BBox(20, 30, 40, 50)) == pytest.approx(1 / (231 + 21 * 21 - 1))


def test_una_caja_que_no_toca_la_plaza_da_iou_cero() -> None:
    mascara = rasterizar_poligono(cuadrado(0.1, 0.1, 0.2, 0.3).to_pixels(100, 100), 100, 100)
    assert mascara.iou_con(BBox(50, 50, 60, 60)) == 0.0
    assert mascara.iou_con(BBox(21, 10, 30, 30)) == 0.0, "pegada por la derecha, sin solapar"


def test_el_iou_con_un_poligono_no_rectangular_cuenta_solo_lo_de_dentro() -> None:
    """Un rombo inscrito en un cuadrado ocupa la mitad del cuadrado, asi que la caja que
    lo envuelve tiene IoU ~0,5 con el, no 1,0: es lo que distingue un IoU de verdad de
    un "la caja toca el poligono"."""
    rombo = Poligono(id="r", puntos=((0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)))
    mascara = rasterizar_poligono(rombo.to_pixels(100, 100), 101, 101)
    assert mascara.area == pytest.approx(101 * 101 / 2, rel=0.03)
    assert mascara.iou_con(BBox(0, 0, 100, 100)) == pytest.approx(0.5, abs=0.02)
    # La mitad superior de la caja (101 x 51 pixeles) contiene la mitad del rombo:
    # IoU = (A/2) / (101 * 51 + A/2) con A ~ 5100, o sea ~1/3.
    assert mascara.iou_con(BBox(0, 0, 100, 50)) == pytest.approx(1 / 3, abs=0.02)


# --- GeometriaPorFotograma: desde el primer fotograma, no en la construccion -----------


def test_convierte_la_primera_vez_que_ve_un_fotograma_y_no_antes() -> None:
    llamadas: list[tuple[int, int]] = []

    def convertir(ancho: int, alto: int) -> tuple[int, int]:
        llamadas.append((ancho, alto))
        return (ancho, alto)

    geometria = GeometriaPorFotograma(convertir, nombre="prueba")
    assert geometria.resolucion is None
    assert llamadas == [], "en la construccion no se conoce la resolucion"

    assert geometria.para(fotograma(1920, 1080)) == (1920, 1080)
    assert geometria.resolucion == (1920, 1080)
    assert llamadas == [(1920, 1080)]


def test_mientras_la_forma_no_cambia_devuelve_el_mismo_objeto_sin_reconvertir() -> None:
    llamadas = 0

    def convertir(ancho: int, alto: int) -> object:
        nonlocal llamadas
        llamadas += 1
        return object()

    geometria = GeometriaPorFotograma(convertir, nombre="prueba")
    primero = geometria.para(fotograma(1280, 720, 0))
    for i in range(1, 5):
        assert geometria.para(fotograma(1280, 720, i)) is primero
    assert llamadas == 1


def test_si_la_forma_cambia_a_mitad_reconvierte_y_lo_deja_en_el_log_como_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    geometria = GeometriaPorFotograma(lambda ancho, alto: (ancho, alto), nombre="entrada")
    with caplog.at_level(logging.INFO):
        geometria.para(fotograma(1920, 1080, 0))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert geometria.para(fotograma(1280, 720, 1)) == (1280, 720)

    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1
    assert "entrada" in avisos[0].getMessage()
    assert "1920x1080" in avisos[0].getMessage()
    assert "1280x720" in avisos[0].getMessage()
    assert geometria.resolucion == (1280, 720)
