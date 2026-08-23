"""Los tipos que viajan entre bloques.

Son el vocabulario comun de todo el sistema: un analizador no sabe de que fuente vino el
fotograma ni que modelo lo detecto, solo ve estos tipos. Por eso las decisiones que van
aqui (pixeles absolutos, BGR, UTC) se toman una vez y no se reabren en cada bloque.

Todos son dataclasses `frozen=True, slots=True`: inmutables porque un `Frame` o una
`Detection` puede pasar por varios analizadores y ninguno debe poder modificarselo al
siguiente, y con `slots` porque en el bucle del worker se crean miles por minuto.

La validacion se hace en el constructor. Un valor imposible (una caja con `x2 < x1`, una
confianza de 1.4, un timestamp sin zona) falla donde se crea, con el nombre del campo, y
no cuarenta minutos despues en el sink.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

import numpy as np

__all__ = [
    "AnalyzerOutput",
    "BBox",
    "Detection",
    "Event",
    "Frame",
    "Metric",
    "SourceInfo",
    "Track",
]


def _validar_finito(valor: float, campo: str) -> float:
    """Devuelve `valor` como `float` y rechaza `NaN` e `inf`.

    Un `NaN` que entra en una metrica no rompe nada al entrar: rompe la grafica, la
    agregacion por minuto y la comparacion, y para entonces ya no se sabe de donde salio.
    """
    numero = float(valor)
    if not math.isfinite(numero):
        raise ValueError(f"{campo} debe ser un numero finito, no {valor!r}")
    return numero


def _normalizar_timestamp(valor: datetime, campo: str) -> datetime:
    """Exige un `datetime` con zona y lo devuelve en UTC conservando el instante."""
    if not isinstance(valor, datetime):
        raise TypeError(f"{campo} debe ser un datetime, no {type(valor).__name__}")
    if valor.tzinfo is None or valor.tzinfo.utcoffset(valor) is None:
        raise ValueError(
            f"{campo} no puede ser un datetime naive: sin zona horaria no identifica un instante"
        )
    return valor.astimezone(UTC)


def _validar_serializable(datos: Mapping[str, object], campo: str) -> None:
    """Rechaza en el constructor lo que el sink no podria escribir.

    Los eventos son escasos (un cruce de linea, un cambio de ocupacion), asi que este
    coste se paga una vez por evento y evita que el fallo aparezca al volcarlo.
    """
    try:
        json.dumps(datos)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{campo} solo admite valores serializables a JSON: {exc}") from exc


def _mapa_inmutable(
    origen: Mapping[str, object], campo: str, *, valores_str: bool
) -> Mapping[str, object]:
    """Copia el mapping y lo devuelve como `MappingProxyType`.

    Se copia a proposito: quien construye el evento suele reutilizar el `dict` en la
    siguiente iteracion del bucle, y sin copia el evento ya emitido cambiaria solo.
    """
    if not isinstance(origen, Mapping):
        raise TypeError(f"{campo} debe ser un mapping, no {type(origen).__name__}")
    copia = dict(origen)
    for clave, valor in copia.items():
        if not isinstance(clave, str):
            raise TypeError(f"las claves de {campo} deben ser str, no {type(clave).__name__}")
        if valores_str and not isinstance(valor, str):
            raise TypeError(
                f"{campo}[{clave!r}] debe ser str, no {type(valor).__name__}: "
                "las dimensiones de una metrica son etiquetas, no medidas"
            )
    if not valores_str:
        _validar_serializable(copia, campo)
    return MappingProxyType(copia)


@dataclass(frozen=True, slots=True)
class BBox:
    """Caja delimitadora en pixeles absolutos del fotograma original, formato `xyxy`.

    Origen arriba-izquierda, `x` crece a la derecha e `y` hacia abajo (convencion de
    OpenCV). **Nunca normalizada a 0-1**: quien necesite fracciones las convierte en su
    borde (el YAML lo hace en la configuracion), no aqui.

    Una caja de area cero (`x1 == x2`) es valida; una imposible (`x2 < x1`) no lo es.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        for campo in ("x1", "y1", "x2", "y2"):
            object.__setattr__(self, campo, _validar_finito(getattr(self, campo), campo))
        if self.x2 < self.x1:
            raise ValueError(f"x2 ({self.x2}) no puede ser menor que x1 ({self.x1})")
        if self.y2 < self.y1:
            raise ValueError(f"y2 ({self.y2}) no puede ser menor que y1 ({self.y1})")

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def bottom_center(self) -> tuple[float, float]:
        """Punto de apoyo en el suelo. Es el que se usa para decidir si algo esta dentro
        de una zona: el centro de la caja de un camion cae sobre el techo de la cabina."""
        return ((self.x1 + self.x2) / 2.0, self.y2)

    def translated(self, dx: float, dy: float) -> BBox:
        """Devuelve la caja desplazada. Con `clipped`, es lo que necesita un detector con
        ROI para devolver coordenadas del fotograma original."""
        return BBox(self.x1 + dx, self.y1 + dy, self.x2 + dx, self.y2 + dy)

    def clipped(self, width: float, height: float) -> BBox:
        """Devuelve la caja recortada al rectangulo `(0, 0, width, height)`.

        Lanza `ValueError` si la caja no corta el fotograma: pedir una region que no
        existe es un error de configuracion, no una caja vacia que arrastrar hasta el
        detector.
        """
        if width < 0 or height < 0:
            raise ValueError(f"el fotograma no puede medir {width}x{height}")
        if self.x1 > width or self.x2 < 0 or self.y1 > height or self.y2 < 0:
            raise ValueError(
                f"la caja ({self.x1}, {self.y1}, {self.x2}, {self.y2}) no corta el "
                f"fotograma de {width}x{height}"
            )
        return BBox(
            min(max(self.x1, 0.0), width),
            min(max(self.y1, 0.0), height),
            min(max(self.x2, 0.0), width),
            min(max(self.y2, 0.0), height),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceInfo:
    """Lo que una fuente declara de si misma antes de emitir el primer fotograma.

    `native_fps=None` significa "la fuente no lo declara", que es lo normal en RTSP y en
    algunos HLS. No es lo mismo que no saber cuantos fps se van a analizar: eso lo decide
    `target_fps` en la configuracion.
    """

    width: int
    height: int
    native_fps: float | None = None
    is_live: bool

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"width y height van en pixeles y deben ser > 0, no {self.width}x{self.height}"
            )
        if self.native_fps is None:
            return
        fps = _validar_finito(self.native_fps, "native_fps")
        if fps <= 0:
            raise ValueError(f"native_fps debe ser > 0 o None, no {self.native_fps!r}")
        object.__setattr__(self, "native_fps", fps)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Frame:
    """Un fotograma emitido por una fuente.

    - `image`: BGR, `HxWx3`, `uint8`. Es lo que entrega OpenCV y el core no convierte
      espacios de color: quien necesite RGB lo convierte en su adaptador.
    - `index`: empieza en 0 y sube de 1 en 1 **por fotograma emitido**. Los fotogramas
      que el muestreo a `target_fps` descarta no consumen indice, y el contador no se
      reinicia cuando una fuente en directo reconecta, para que `every_n_frames` de un
      analizador siga significando lo mismo despues de una caida.
    - `timestamp`: instante de captura, en UTC y con zona.

    **El buffer no se copia**: `Frame(image=img, ...).image is img`. Es deliberado. En un chip
    de 15 W el pipeline entero se mueve entre 5 y 8 fps (ver `_docs/rendimiento.md`) y
    copiar 6 MB por fotograma y por bloque es un coste que no compra nada. Lo que hace
    que sea seguro es la convencion, no el tipo: **nadie modifica `frame.image` in place**,
    y `Analyzer.annotate` devuelve un array nuevo en vez de pintar sobre el que recibe.

    Compara por identidad (`eq=False`) a proposito: la igualdad de un `ndarray` es
    elemento a elemento y devuelve un array, asi que un `==` generado por la dataclass
    reventaria con "truth value of an array is ambiguous" en cuanto alguien metiera
    fotogramas en una lista y preguntara `in`.
    """

    image: np.ndarray
    index: int
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.image, np.ndarray):
            raise TypeError(f"image debe ser un np.ndarray, no {type(self.image).__name__}")
        if self.image.ndim != 3:
            raise ValueError(f"image debe tener 3 dimensiones (HxWx3), no {self.image.ndim}")
        if self.image.shape[2] != 3:
            raise ValueError(f"image debe tener 3 canales (BGR), no {self.image.shape[2]}")
        if self.image.dtype != np.uint8:
            raise ValueError(f"image debe ser uint8, no {self.image.dtype}")
        if self.index < 0:
            raise ValueError(f"index empieza en 0 y no retrocede, no puede ser {self.index}")
        object.__setattr__(self, "timestamp", _normalizar_timestamp(self.timestamp, "timestamp"))


def _validar_deteccion(objeto: Detection | Track) -> None:
    """Valida los cuatro campos que `Detection` y `Track` comparten.

    `Track` no hereda de `Detection` (composicion, no herencia: un track no es una
    deteccion, tiene una), asi que la validacion comun vive aqui y no en una clase base.
    """
    if not isinstance(objeto.bbox, BBox):
        raise TypeError(f"bbox debe ser un BBox, no {type(objeto.bbox).__name__}")
    if objeto.class_id < 0:
        raise ValueError(f"class_id es el id del modelo y debe ser >= 0, no {objeto.class_id}")
    if not objeto.label.strip():
        raise ValueError("label no puede estar vacio")
    confidence = _validar_finito(objeto.confidence, "confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence debe estar en [0, 1], no {objeto.confidence!r}")
    object.__setattr__(objeto, "confidence", confidence)


@dataclass(frozen=True, slots=True, kw_only=True)
class Detection:
    """Algo que el detector ha visto en un fotograma.

    `class_id` es el id del modelo (COCO en el MVP) y `label` su nombre ya traducido:
    nada del formato de ultralytics llega hasta aqui.
    """

    bbox: BBox
    class_id: int
    label: str
    confidence: float

    def __post_init__(self) -> None:
        _validar_deteccion(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class Track:
    """Una deteccion con identidad estable entre fotogramas.

    Sin `track_id` no hay conteo: contariamos el mismo coche una vez por fotograma. Se
    construye con `Track.from_detection(deteccion, track_id=7)`.
    """

    bbox: BBox
    class_id: int
    label: str
    confidence: float
    track_id: int

    def __post_init__(self) -> None:
        _validar_deteccion(self)
        if self.track_id < 0:
            raise ValueError(f"track_id debe ser >= 0, no {self.track_id}")

    @classmethod
    def from_detection(cls, detection: Detection, track_id: int) -> Track:
        """Copia los cuatro campos de la deteccion y le pone identidad."""
        return cls(
            bbox=detection.bbox,
            class_id=detection.class_id,
            label=detection.label,
            confidence=detection.confidence,
            track_id=track_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Algo que paso en un instante: un coche cruzo la linea, la plaza 7 se libero.

    `analyzer` es el `name` del analizador que lo emite y `name` el tipo de evento
    (`line_crossing`, `space_occupied`). Lo particular de cada evento va en `attributes`,
    que se guarda como `MappingProxyType` sobre una copia.

    No es hashable (`MappingProxyType` no lo es) pero compara por valor: para comparar
    colecciones de eventos en un test, listas ordenadas, nunca `set`.
    """

    analyzer: str
    name: str
    timestamp: datetime
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _normalizar_timestamp(self.timestamp, "timestamp"))
        object.__setattr__(
            self, "attributes", _mapa_inmutable(self.attributes, "attributes", valores_str=False)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Metric:
    """Un valor medido en un instante: 12 vehiculos en escena, 8 plazas libres.

    `labels` son las dimensiones de la medida (`{"direction": "norte"}`). Estan aparte
    del `name` a proposito: es lo que permite preguntar "vehiculos por minuto por
    sentido" sin inventarse nombres como `vehicles_per_minute_norte`.

    No es hashable, por lo mismo que `Event`.
    """

    analyzer: str
    name: str
    value: float
    timestamp: datetime
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validar_finito(self.value, "value"))
        object.__setattr__(self, "timestamp", _normalizar_timestamp(self.timestamp, "timestamp"))
        object.__setattr__(self, "labels", _mapa_inmutable(self.labels, "labels", valores_str=True))


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalyzerOutput:
    """Lo que un analizador devuelve por fotograma procesado.

    Acepta cualquier iterable y guarda tuplas: el analizador suele construir listas y no
    tiene por que acordarse de congelarlas antes de devolverlas.
    """

    events: tuple[Event, ...] = ()
    metrics: tuple[Metric, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "metrics", tuple(self.metrics))

    @classmethod
    def empty(cls) -> AnalyzerOutput:
        """Lo que devuelve un analizador que en este fotograma no tiene nada que decir."""
        return cls()
