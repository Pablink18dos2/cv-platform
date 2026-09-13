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

Aqui no hay nada de OpenCV ni de Pydantic: solo tuplas y aritmetica, para que se pueda
probar con numeros escritos a mano.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from cvp.core import Frame

__all__ = [
    "GeometriaPorFotograma",
    "LineaDeConteo",
    "LineaEnPixeles",
    "Punto",
    "lado_de",
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
