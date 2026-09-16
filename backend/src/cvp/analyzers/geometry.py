"""Geometria compartida por los analizadores.

Dos cosas que todo analizador con geometria en el YAML necesita y que no deberia
reescribir cada uno (es lo que #10 deja para que #11 lo reutilice, no lo copie):

1. **De fracciones a pixeles, desde el primer fotograma.** El YAML habla en fracciones 0-1
   (decision de #3) y el analizador necesita pixeles, pero la fabrica construye los
   analizadores **antes** de abrir la fuente, asi que en `from_config` no se conoce la
   resolucion. `GeometriaPorFotograma` convierte la primera vez que ve un `Frame`, con
   `frame.image.shape`, y reconvierte —dejandolo en el log— si la forma cambia a mitad de
   ejecucion, que es lo que pasa cuando un directo reconecta a otra calidad. Con eso el
   analizador es autosuficiente y se prueba con un `Frame` sintetico de cualquier tamano.

2. **El lado de un punto respecto a una linea**, con el signo del producto vectorial. Es
   lo que decide un cruce, y su caso de borde —el punto exactamente sobre la linea, que
   con pixeles enteros pasa— tiene contrato propio: `lado_de` devuelve 0 y quien lo llame
   conserva el lado anterior.

3. **Poligonos, y cuanto los cubre una caja** (#11). `Poligono` es la plaza en fracciones
   tal y como la escribe `zones.json`; `PoligonoEnPixeles` la misma ya en pixeles; y
   `rasterizar_poligono` la convierte en una mascara booleana, recortada a su rectangulo
   envolvente, con la que `MascaraDePoligono.iou_con` mide el IoU **de verdad** —
   interseccion partido por union, en pixeles— con la caja de una deteccion. Rasterizar y
   contar es la via mas simple y robusta para un poligono arbitrario: no hace falta
   clipping geometrico, y a ~0,5 fps su coste es irrelevante. La mascara se calcula una
   vez por resolucion, y la caja no se rasteriza nunca: es un rectangulo y se recorta con
   un slice.

Aqui no hay nada de Pydantic, y OpenCV entra **solo** para rellenar un poligono
(`cv2.fillPoly`); el resto son tuplas y aritmetica, para que se pueda probar con numeros
escritos a mano. El convenio de pixeles es el de `fillPoly`: un vertice entero cae
**dentro** del relleno, asi que un rectangulo de `(x1, y1)` a `(x2, y2)` ocupa
`(x2 - x1 + 1) * (y2 - y1 + 1)` pixeles, y `iou_con` trata la caja igual para que una caja
identica a la plaza de exactamente 1,0.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from cvp.core import BBox, Frame

__all__ = [
    "GeometriaPorFotograma",
    "LineaDeConteo",
    "LineaEnPixeles",
    "MascaraDePoligono",
    "Poligono",
    "PoligonoEnPixeles",
    "Punto",
    "lado_de",
    "rasterizar_poligono",
]

_log = logging.getLogger(__name__)

# Un punto como par `(x, y)`: en fracciones 0-1 o en pixeles, segun donde se use.
Punto = tuple[float, float]


def lado_de(p1: Punto, p2: Punto, punto: Punto) -> int:
    """De que lado de la recta `p1 -> p2` cae `punto`: `+1`, `-1` o `0` si esta encima.

    Es el signo del producto vectorial de `(p2 - p1)` y `(punto - p1)`. En coordenadas de
    imagen (origen arriba-izquierda, `y` hacia abajo) el lado `+1` es **el que queda a la
    derecha de `p1 -> p2` visto en pantalla**: con una linea escrita de izquierda a derecha,
    el lado positivo es el de abajo. Es la convencion que `positive_direction` del YAML da
    por hecha, asi que si alguien la cambia aqui, cambia el sentido de todos los conteos.

    El `0` no se redondea hacia ningun lado a proposito: un punto sobre la linea no es
    evidencia de nada, y tratarlo como un cambio de lado es la manera mas facil de contar
    un cruce que no ha ocurrido.
    """
    cruz = (p2[0] - p1[0]) * (punto[1] - p1[1]) - (p2[1] - p1[1]) * (punto[0] - p1[0])
    if cruz > 0:
        return 1
    if cruz < 0:
        return -1
    return 0


def _comprobar_sentidos(positive_direction: str, negative_direction: str) -> None:
    if not positive_direction.strip() or not negative_direction.strip():
        raise ValueError("los dos sentidos de una linea necesitan nombre")
    if positive_direction == negative_direction:
        raise ValueError(
            f"los dos sentidos de una linea no pueden llamarse igual ({positive_direction!r})"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class LineaDeConteo:
    """Una linea de conteo en fracciones 0-1, tal y como la escribe el YAML.

    Es el argumento **plano** con el que se construye un analizador: sin Pydantic y sin
    YAML de por medio, para que un test la escriba con dos tuplas. El puente con la capa
    de configuracion es el `from_config` del analizador, que la saca de `Line`, y es el
    unico.
    """

    id: str
    p1: Punto
    p2: Punto
    positive_direction: str
    negative_direction: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("una linea de conteo necesita id: es lo que etiqueta sus series")
        if tuple(self.p1) == tuple(self.p2):
            raise ValueError(f"la linea {self.id!r} tiene longitud cero y no la cruza nadie")
        for nombre, punto in (("p1", self.p1), ("p2", self.p2)):
            if not all(0.0 <= c <= 1.0 for c in punto):
                raise ValueError(
                    f"{nombre} de la linea {self.id!r} va en fracciones 0-1 del fotograma, "
                    f"no {punto!r}"
                )
        _comprobar_sentidos(self.positive_direction, self.negative_direction)

    def to_pixels(self, width: int, height: int) -> LineaEnPixeles:
        """La misma linea en pixeles de un fotograma de `width` x `height`, sin redondear."""
        if width <= 0 or height <= 0:
            raise ValueError(f"el fotograma va en pixeles y no puede medir {width}x{height}")
        return LineaEnPixeles(
            id=self.id,
            p1=(self.p1[0] * width, self.p1[1] * height),
            p2=(self.p2[0] * width, self.p2[1] * height),
            positive_direction=self.positive_direction,
            negative_direction=self.negative_direction,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class LineaEnPixeles:
    """Una linea de conteo ya en pixeles del fotograma. Es con la que se calcula."""

    id: str
    p1: Punto
    p2: Punto
    positive_direction: str
    negative_direction: str

    def lado_de(self, punto: Punto) -> int:
        """`+1`, `-1` o `0` (sobre la linea). Ver `lado_de`."""
        return lado_de(self.p1, self.p2, punto)

    def sentido_hacia(self, lado: int) -> str:
        """El nombre del sentido de quien acaba de pasar al lado `lado`."""
        if lado > 0:
            return self.positive_direction
        if lado < 0:
            return self.negative_direction
        raise ValueError("el lado 0 es estar sobre la linea, y eso no es un sentido")

    @property
    def punto_medio(self) -> Punto:
        return ((self.p1[0] + self.p2[0]) / 2.0, (self.p1[1] + self.p2[1]) / 2.0)


def _area_con_signo(puntos: Sequence[Punto]) -> float:
    """El area del poligono por la formula del cordon (shoelace), con signo."""
    doble = 0.0
    for i, (x1, y1) in enumerate(puntos):
        x2, y2 = puntos[(i + 1) % len(puntos)]
        doble += x1 * y2 - x2 * y1
    return doble / 2.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Poligono:
    """Una zona en fracciones 0-1, tal y como la escribe `zones.json`.

    Es el argumento **plano** con el que se construye un analizador de zonas, sin JSON ni
    Pydantic de por medio: un test la escribe con cuatro tuplas. Valida lo que un poligono
    necesita para medir algo —id, al menos tres puntos, todos en fracciones y con area—,
    y sus mensajes dicen que punto esta mal, para que quien cargue un fichero de zonas los
    pueda prefijar con la zona.
    """

    id: str
    puntos: tuple[Punto, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("una zona necesita id: es lo que etiqueta sus eventos y metricas")
        puntos = tuple((float(x), float(y)) for x, y in self.puntos)
        object.__setattr__(self, "puntos", puntos)
        if len(puntos) < 3:
            raise ValueError(
                f"el poligono de la zona {self.id!r} necesita al menos 3 puntos y tiene "
                f"{len(puntos)}"
            )
        for i, punto in enumerate(puntos):
            if not all(0.0 <= c <= 1.0 for c in punto):
                raise ValueError(
                    f"el punto {i} de la zona {self.id!r} es {list(punto)!r} y va en "
                    "fracciones 0-1 del fotograma"
                )
        if _area_con_signo(puntos) == 0.0:
            raise ValueError(
                f"el poligono de la zona {self.id!r} tiene area cero y no cabe nada dentro"
            )

    def to_pixels(self, width: int, height: int) -> PoligonoEnPixeles:
        """El mismo poligono en pixeles de un fotograma de `width` x `height`, sin redondear."""
        if width <= 0 or height <= 0:
            raise ValueError(f"el fotograma va en pixeles y no puede medir {width}x{height}")
        return PoligonoEnPixeles(
            id=self.id, puntos=tuple((x * width, y * height) for x, y in self.puntos)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PoligonoEnPixeles:
    """Una zona ya en pixeles del fotograma. Es con la que se rasteriza y se dibuja."""

    id: str
    puntos: tuple[Punto, ...]

    @property
    def centroide(self) -> Punto:
        """La media de los vertices. Vale para colocar una etiqueta, no es el centro de
        masas exacto de un poligono irregular."""
        n = len(self.puntos)
        return (sum(p[0] for p in self.puntos) / n, sum(p[1] for p in self.puntos) / n)

    @property
    def rectangulo_envolvente(self) -> tuple[float, float, float, float]:
        """`(x1, y1, x2, y2)` del rectangulo minimo que lo contiene."""
        xs = [p[0] for p in self.puntos]
        ys = [p[1] for p in self.puntos]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class MascaraDePoligono:
    """Un poligono rasterizado: una mascara booleana recortada a su rectangulo envolvente.

    Recortada a proposito: veinte plazas a 1080p como mascaras del fotograma entero serian
    40 MB, y como recortes, unos pocos KB. `(x1, y1)` es la esquina del recorte dentro del
    fotograma y `mascara[y - y1, x - x1]` dice si el pixel `(x, y)` esta dentro.
    """

    x1: int
    y1: int
    mascara: np.ndarray

    @property
    def area(self) -> int:
        """Pixeles dentro del poligono."""
        return int(np.count_nonzero(self.mascara))

    @property
    def x2(self) -> int:
        return self.x1 + int(self.mascara.shape[1])

    @property
    def y2(self) -> int:
        return self.y1 + int(self.mascara.shape[0])

    def iou_con(self, caja: BBox) -> float:
        """Interseccion partido por union, en pixeles, entre el poligono y `caja`.

        La caja se cuenta con el mismo convenio que `fillPoly` (vertices enteros
        **dentro**, asi que hasta una caja de area cero ocupa un pixel), de modo que una
        caja identica a un rectangulo rasterizado da 1,0. Una que no toque el recorte da 0,0.
        """
        bx1, by1 = int(round(caja.x1)), int(round(caja.y1))
        bx2, by2 = int(round(caja.x2)) + 1, int(round(caja.y2)) + 1
        area_caja = (bx2 - bx1) * (by2 - by1)
        ix1, iy1 = max(bx1, self.x1), max(by1, self.y1)
        ix2, iy2 = min(bx2, self.x2), min(by2, self.y2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        interseccion = int(
            np.count_nonzero(
                self.mascara[iy1 - self.y1 : iy2 - self.y1, ix1 - self.x1 : ix2 - self.x1]
            )
        )
        union = self.area + area_caja - interseccion
        return interseccion / union if union > 0 else 0.0


def rasterizar_poligono(poligono: PoligonoEnPixeles, width: int, height: int) -> MascaraDePoligono:
    """Rellena el poligono con `cv2.fillPoly` sobre un lienzo del tamano de su rectangulo
    envolvente, recortado al fotograma de `width` x `height`.

    Los vertices se redondean a enteros aqui, en un solo sitio. Un poligono que se salga
    del fotograma se recorta; uno que quede entero fuera da una mascara vacia y area 0.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"el fotograma va en pixeles y no puede medir {width}x{height}")
    enteros = np.array([[round(x), round(y)] for x, y in poligono.puntos], dtype=np.int32)
    x1 = int(max(0, enteros[:, 0].min()))
    y1 = int(max(0, enteros[:, 1].min()))
    x2 = int(min(width, enteros[:, 0].max() + 1))
    y2 = int(min(height, enteros[:, 1].max() + 1))
    if x2 <= x1 or y2 <= y1:
        return MascaraDePoligono(x1=0, y1=0, mascara=np.zeros((0, 0), dtype=bool))
    lienzo = np.zeros((y2 - y1, x2 - x1), dtype=np.uint8)
    cv2.fillPoly(lienzo, [enteros - np.array([x1, y1], dtype=np.int32)], 1)
    return MascaraDePoligono(x1=x1, y1=y1, mascara=lienzo.astype(bool))


class GeometriaPorFotograma[T]:
    """Convierte la geometria a pixeles con la forma del primer `Frame`, y la guarda.

    `convertir(width, height)` es lo que cada analizador sabe hacer con su geometria (las
    lineas de #10, los poligonos de #11); esta clase solo decide **cuando** llamarlo:

    - la primera vez que ve un fotograma, con `frame.image.shape`;
    - otra vez si la forma cambia a mitad de ejecucion, con un WARNING en el log, porque
      una reconexion a otra calidad es un hecho que conviene ver junto a los datos.

    Mientras la forma no cambie, devuelve el mismo objeto sin recalcular nada.
    """

    def __init__(self, convertir: Callable[[int, int], T], *, nombre: str) -> None:
        self._convertir = convertir
        self._nombre = nombre
        self._forma: tuple[int, int] | None = None
        self._valor: T | None = None

    @property
    def resolucion(self) -> tuple[int, int] | None:
        """`(width, height)` del ultimo fotograma visto, o `None` si aun no ha visto ninguno."""
        return self._forma

    def para(self, frame: Frame) -> T:
        """La geometria en pixeles del fotograma. Convierte solo si hace falta."""
        alto, ancho = frame.image.shape[:2]
        forma = (int(ancho), int(alto))
        if self._valor is None or forma != self._forma:
            if self._forma is None:
                _log.info(
                    "%s: geometria convertida a pixeles sobre un fotograma de %dx%d",
                    self._nombre,
                    *forma,
                )
            else:
                _log.warning(
                    "%s: el fotograma ha pasado de %dx%d a %dx%d; se reconvierte la geometria "
                    "a la nueva resolucion",
                    self._nombre,
                    *self._forma,
                    *forma,
                )
            self._valor = self._convertir(*forma)
            self._forma = forma
        return self._valor
