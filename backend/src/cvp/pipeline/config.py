"""El `project.yaml` de un proyecto: esquema, carga y validacion.

Un proyecto es un fichero, no codigo, asi que este modulo es el que decide si ese fichero
esta bien escrito. Falla **en el segundo 0**, con todos los motivos a la vez y diciendo
que campo esta mal, que se esperaba y que habia; no en el minuto 40 con un `KeyError`
dentro del bucle de inferencia.

Tres decisiones que valen para todo el fichero:

- **La geometria va en fracciones 0-1** del ancho y del alto del fotograma, nunca en
  pixeles. La fuente del proyecto 01 sirve el mismo directo en 1080p, 720p, 480p y 360p
  (`projects/01-parking-trafico/fuente.md`): con pixeles, cambiar el `format` invalida en
  silencio todas las lineas y todos los ROI, y las cajas se siguen dibujando, solo que en
  el sitio equivocado. `cvp.core` habla solo en pixeles, y la traduccion la hace el runner
  una vez, despues de `source.open()`, cuando ya sabe `width` y `height`.
- **Las rutas relativas lo son al directorio que contiene el `project.yaml`**, nunca al
  CWD ni a la raiz del repo: un proyecto es una carpeta autocontenida y `cvp run` tiene
  que funcionar igual desde cualquier sitio. Tras la carga no queda ninguna relativa.
- **Una variable de entorno referenciada que no existe es un error de carga.** Nunca se
  sustituye por vacio: una cadena vacia colada en una URL falla lejos de su causa.

El `type:` de cada bloque se valida contra `CONFIG_SCHEMAS`, el registro de esquemas de
este modulo, y **no** contra `cvp.core.registry`. Rellenar `SOURCES`/`DETECTORS`/...
obliga a importar `cvp.sources`, `cvp.detectors`, ..., que importan OpenCV, yt-dlp y
ultralytics: validar dejaria de costar milisegundos y `cvp validate` necesitaria el stack
de vision entero instalado. La comprobacion contra el registro de implementaciones se hace
donde ya toca pagar esos imports, en el runner que construye los objetos.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cvp.core import BBox
from cvp.pipeline.errors import ConfigError

__all__ = [
    "CONFIG_SCHEMAS",
    "AnalyzerConfig",
    "ByteTrackTracker",
    "DetectorConfig",
    "FileSource",
    "Hysteresis",
    "JsonlSink",
    "Line",
    "ParkingOccupancyAnalyzer",
    "Point",
    "PreviewSink",
    "ProjectConfig",
    "Reconnect",
    "Region",
    "RtspSource",
    "SinkConfig",
    "SourceConfig",
    "SqliteSink",
    "TrafficFlowAnalyzer",
    "WebcamSource",
    "YoloDetector",
    "YouTubeLiveSource",
    "load_project_config",
]

# El nombre del fichero dentro del directorio de un proyecto.
NOMBRE_FICHERO = "project.yaml"

# Los dos campos que rellena la carga a partir de la ruta del fichero. Estan declarados en
# `ProjectConfig`, asi que sin esto `extra="forbid"` los dejaria escribir desde el YAML.
_CAMPOS_DERIVADOS = ("config_path", "project_dir")


# --------------------------------------------------------------------------------------
# Como se escribe un valor dentro de un mensaje de error
# --------------------------------------------------------------------------------------


def _valor(dato: object) -> str:
    """El valor ofensivo, tal y como aparece en la linea de error.

    Las cadenas van entrecomilladas (`'rapido'`) para que se vea que lo son, que es medio
    diagnostico cuando alguien ha escrito un numero entre comillas sin querer.
    """
    if isinstance(dato, bool):
        return "true" if dato else "false"
    if dato is None:
        return "null"
    if isinstance(dato, str | int | float):
        return repr(dato)
    if isinstance(dato, Mapping):
        return f"un mapa de {len(dato)} campos"
    if isinstance(dato, Sequence):
        return f"una lista de {len(dato)} elementos"
    return repr(dato)


def _limite(dato: object) -> str:
    """Un limite de un rango, sin decimales inutiles: `0.0` se lee `0`."""
    if isinstance(dato, float) and dato.is_integer():
        return str(int(dato))
    return str(dato)


# --------------------------------------------------------------------------------------
# Geometria: el YAML habla en fracciones, `cvp.core` en pixeles
# --------------------------------------------------------------------------------------


def _validar_fraccion(valor: float) -> float:
    if not 0.0 <= valor <= 1.0:
        raise ValueError(
            "la geometría del project.yaml va en fracciones 0-1 del fotograma, "
            f"y vale {_valor(valor)}"
        )
    return valor


def _validar_confianza(valor: float) -> float:
    if not 0.0 < valor <= 1.0:
        raise ValueError(f"debe estar entre 0 y 1, y vale {_valor(valor)}")
    return valor


def _comprobar_dimensiones(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError(
            f"el fotograma va en píxeles y no puede medir {width}x{height}: "
            "sin dimensiones no hay fracción que convertir"
        )


_Fraccion = Annotated[float, AfterValidator(_validar_fraccion)]
_Confianza = Annotated[float, AfterValidator(_validar_confianza)]


class _ModeloBase(BaseModel):
    """Inmutable y sin campos desconocidos, para todos los modelos de configuracion.

    `frozen=True` porque el runner programa contra esta configuracion durante horas y
    nadie deberia poder cambiarla a mitad de ejecucion. `extra="forbid"` porque un campo
    desconocido casi siempre es una errata (`targt_fps`), y una errata ignorada en
    silencio es exactamente el fallo de las 04:00 que este modulo existe para eliminar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Point(_ModeloBase):
    """Un punto del fotograma, en fracciones 0-1. En el YAML se escribe `[x, y]`.

    `Point(x=0.5, y=0.25).to_pixels(1920, 1080) == (960.0, 270.0)`.
    """

    x: _Fraccion
    y: _Fraccion

    @model_validator(mode="before")
    @classmethod
    def _desde_lista(cls, valor: Any) -> Any:
        if isinstance(valor, Mapping):
            return valor
        if isinstance(valor, list | tuple):
            if len(valor) != 2:
                raise ValueError(
                    f"un punto son 2 fracciones [x, y] entre 0 y 1, y tiene {len(valor)} elementos"
                )
            return {"x": valor[0], "y": valor[1]}
        raise ValueError(f"un punto son 2 fracciones [x, y] entre 0 y 1, y vale {_valor(valor)}")

    def to_pixels(self, width: int, height: int) -> tuple[float, float]:
        """El mismo punto en pixeles del fotograma, sin redondear.

        Lo llama el runner una vez, cuando `SourceInfo` ya dice cuanto mide el fotograma.
        """
        _comprobar_dimensiones(width, height)
        return (self.x * width, self.y * height)


class Region(_ModeloBase):
    """Una region rectangular del fotograma, en fracciones 0-1: `[x1, y1, x2, y2]`.

    Se convierte a un `BBox` de `cvp.core`, que va en pixeles y en floats: **no se
    redondea**, porque redondear es cosa de quien recorte la imagen.

    Una region de area cero es invalida, a diferencia de un `BBox` de area cero, que si es
    legitimo: un ROI que no contiene nada no es una configuracion que nadie quisiera.
    """

    x1: _Fraccion
    y1: _Fraccion
    x2: _Fraccion
    y2: _Fraccion

    @model_validator(mode="before")
    @classmethod
    def _desde_lista(cls, valor: Any) -> Any:
        if isinstance(valor, Mapping):
            return valor
        if isinstance(valor, list | tuple):
            if len(valor) != 4:
                raise ValueError(
                    "una región son 4 fracciones [x1, y1, x2, y2] entre 0 y 1, "
                    f"y tiene {len(valor)} elementos"
                )
            return dict(zip(("x1", "y1", "x2", "y2"), valor, strict=True))
        raise ValueError(
            f"una región son 4 fracciones [x1, y1, x2, y2] entre 0 y 1, y vale {_valor(valor)}"
        )

    @model_validator(mode="after")
    def _comprobar_forma(self) -> Region:
        if self.x2 < self.x1:
            raise ValueError(
                f"la región está invertida: x2 ({self.x2}) no puede ser menor que x1 ({self.x1})"
            )
        if self.y2 < self.y1:
            raise ValueError(
                f"la región está invertida: y2 ({self.y2}) no puede ser menor que y1 ({self.y1})"
            )
        if self.x1 == self.x2 or self.y1 == self.y2:
            raise ValueError("una región de área cero no contiene nada; revisa las 4 fracciones")
        return self

    def to_pixels(self, width: int, height: int) -> BBox:
        """La misma region como `BBox` de `cvp.core`, en pixeles y sin redondear."""
        _comprobar_dimensiones(width, height)
        return BBox(
            self.x1 * width,
            self.y1 * height,
            self.x2 * width,
            self.y2 * height,
        )


# --------------------------------------------------------------------------------------
# Rutas: relativas al directorio del project.yaml, absolutas tras la carga
# --------------------------------------------------------------------------------------


def _resolver(valor: Path, info: ValidationInfo) -> Path | None:
    """Absolutiza `valor` contra el directorio del proyecto.

    Devuelve `None` cuando el modelo se construye a mano desde Python (un test) y no hay
    contexto: sin saber donde esta el `project.yaml` no se puede resolver contra el, y
    resolver contra el CWD es justo lo que este modulo promete no hacer.
    """
    base = (info.context or {}).get("project_dir")
    if base is None:
        return None
    ruta = Path(valor).expanduser()
    if not ruta.is_absolute():
        ruta = Path(base) / ruta
    return ruta.resolve()


def _validar_ruta_entrada(valor: Path, info: ValidationInfo) -> Path:
    """Una ruta de la que se lee: tiene que existir ya, en el momento de la carga."""
    ruta = _resolver(valor, info)
    if ruta is None:
        return valor
    if not ruta.exists():
        raise ValueError(f"no existe el fichero de entrada {ruta}")
    return ruta


def _validar_ruta_salida(valor: Path, info: ValidationInfo) -> Path:
    """Una ruta en la que se escribe: el fichero puede no existir, su directorio no.

    La configuracion no crea directorios (eso es del sink al arrancar); lo que hace es
    fallar en el segundo 0 si el sitio donde va a escribir no existe.
    """
    ruta = _resolver(valor, info)
    if ruta is None:
        return valor
    if not ruta.parent.is_dir():
        raise ValueError(
            f"su directorio padre {ruta.parent} no existe; la configuración no crea directorios"
        )
    return ruta


_RutaEntrada = Annotated[Path, AfterValidator(_validar_ruta_entrada)]
_RutaSalida = Annotated[Path, AfterValidator(_validar_ruta_salida)]


# --------------------------------------------------------------------------------------
# `source`: de donde vienen los fotogramas
# --------------------------------------------------------------------------------------


class Reconnect(_ModeloBase):
    """Politica de reconexion de las fuentes en directo.

    Semantica que hereda el connector: si hay mas reintentos que elementos en `backoff_s`,
    **se repite el ultimo**. `max_retries=None` es reintentar indefinidamente, que es lo
    razonable en un directo que se cae un rato; `0` es no reintentar.
    """

    max_retries: int | None = 20
    backoff_s: list[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0, 15.0, 30.0, 60.0])

    @field_validator("max_retries")
    @classmethod
    def _reintentos_no_negativos(cls, valor: int | None) -> int | None:
        if valor is not None and valor < 0:
            raise ValueError(
                "debe ser >= 0 (0 no reintenta) o null (reintentar siempre), "
                f"y vale {_valor(valor)}"
            )
        return valor

    @field_validator("backoff_s")
    @classmethod
    def _espera_creciente(cls, valor: list[float]) -> list[float]:
        if not valor:
            raise ValueError("no puede estar vacía: sin esperas no hay backoff que aplicar")
        for i, espera in enumerate(valor):
            if espera <= 0:
                raise ValueError(f"todas las esperas deben ser > 0, y backoff_s[{i}] vale {espera}")
        for i in range(1, len(valor)):
            if valor[i] < valor[i - 1]:
                raise ValueError(
                    "debe ser no decreciente y un backoff que baja siempre es una errata: "
                    f"backoff_s[{i}] ({valor[i]}) es menor que backoff_s[{i - 1}] ({valor[i - 1]})"
                )
        return valor


def _extractor_args_por_defecto() -> dict[str, dict[str, list[str]]]:
    """Los clientes de yt-dlp que **funcionan** con el directo del proyecto 01.

    Sin ellos, yt-dlp devuelve `ERROR: This video is not available`, que parece un video
    caido y no lo es (`projects/01-parking-trafico/fuente.md`). Un default vacio condenaria
    a cada proyecto nuevo de YouTube a redescubrir ese mensaje enganoso.
    """
    return {"youtube": {"player_client": ["web_safari", "tv", "android"]}}


class _SourceBase(_ModeloBase):
    """Lo comun a las cuatro fuentes.

    `target_fps` no tiene valor por defecto a proposito: la tasa de muestreo es una
    decision del proyecto (cuanto se mide y cuanta CPU se gasta), no un descuido.
    """

    type: str
    target_fps: Annotated[float, Field(gt=0)]


class FileSource(_SourceBase):
    """Un fichero de video local. La fuente de los tests y de las demos."""

    type: Literal["file"]
    path: _RutaEntrada
    loop: bool = False


class YouTubeLiveSource(_SourceBase):
    """Un directo de YouTube, resuelto a HLS con yt-dlp.

    `refresh_url_every_s` existe porque la URL HLS **caduca a las 6 h exactas**: es el
    parametro `expire` del manifiesto, medido sobre el directo real, no estimado. Volver a
    resolverla proactivamente antes de que caduque es lo que evita un hueco de datos cada
    seis horas; esperar a que falle la lectura ya es tarde.
    """

    type: Literal["youtube_live"]
    url: Annotated[str, Field(min_length=1)]
    format: str = "best"
    extractor_args: dict[str, dict[str, list[str]]] = Field(
        default_factory=_extractor_args_por_defecto
    )
    refresh_url_every_s: int = 18000
    reconnect: Reconnect = Field(default_factory=Reconnect)

    @field_validator("refresh_url_every_s")
    @classmethod
    def _antes_de_que_caduque(cls, valor: int) -> int:
        if valor > 21600:
            raise ValueError(
                "la URL HLS de YouTube caduca a las 6 h (21600 s) exactas, así que "
                f"re-resolverla cada {valor} s garantiza el hueco de datos que este campo "
                "existe para evitar; usa un valor entre 60 y 21600"
            )
        if valor < 60:
            raise ValueError(
                "re-resolver la URL HLS cuesta una llamada a yt-dlp, así que por debajo de "
                f"60 s no compensa, y vale {_valor(valor)}"
            )
        return valor


class RtspSource(_SourceBase):
    """Una camara IP por RTSP."""

    type: Literal["rtsp"]
    url: Annotated[str, Field(min_length=1)]
    reconnect: Reconnect = Field(default_factory=Reconnect)

    @field_validator("url")
    @classmethod
    def _empieza_por_rtsp(cls, valor: str) -> str:
        if not valor.startswith("rtsp://"):
            raise ValueError(
                f"una URL RTSP tiene que empezar por 'rtsp://', y vale {_valor(valor)}"
            )
        return valor


class WebcamSource(_SourceBase):
    """La webcam de la maquina. `device` es el indice que entiende OpenCV."""

    type: Literal["webcam"]
    device: Annotated[int, Field(ge=0)] = 0


# --------------------------------------------------------------------------------------
# `detector` y `tracker`
# --------------------------------------------------------------------------------------


class YoloDetector(_ModeloBase):
    """El detector YOLO de ultralytics.

    `weights` **no es un campo de ruta**: es una cadena opaca (`yolo11n.pt`) que resuelve
    el propio detector contra `CVP_MODELS_DIR` cuando arranca. Aqui no se resuelve, no se
    comprueba que exista y no se toca; que este escrito asi es deliberado, no un olvido.

    Tampoco se comprueba que las `classes` existan en COCO: eso necesitaria cargar el
    modelo, y validar la configuracion no puede costar los segundos que tarda ultralytics
    en importarse.
    """

    type: Literal["yolo"]
    weights: str = "yolo11n.pt"
    imgsz: int = 640
    conf: _Confianza = 0.35
    classes: list[str]
    roi: Region | None = None

    @field_validator("imgsz")
    @classmethod
    def _multiplo_de_32(cls, valor: int) -> int:
        if not 320 <= valor <= 1280:
            raise ValueError(f"debe estar entre 320 y 1280, y vale {_valor(valor)}")
        if valor % 32:
            raise ValueError(
                "debe ser múltiplo de 32 porque ultralytics lo redondea por su cuenta y el "
                f"YAML dejaría de decir la verdad, y vale {_valor(valor)}"
            )
        return valor

    @field_validator("classes")
    @classmethod
    def _clases_utiles(cls, valor: list[str]) -> list[str]:
        if not valor:
            raise ValueError(
                "no puede estar vacía: un detector que no filtra ninguna clase devuelve "
                "peatones y semáforos junto con los vehículos"
            )
        vistas: set[str] = set()
        for i, clase in enumerate(valor):
            if not clase.strip():
                raise ValueError(f"classes[{i}] está vacía")
            if clase in vistas:
                raise ValueError(f"la clase {clase!r} está repetida en classes[{i}]")
            vistas.add(clase)
        return valor


class ByteTrackTracker(_ModeloBase):
    """ByteTrack.

    `frame_rate` **no esta aqui a proposito**: no es una decision del bloque `tracker`, es
    el `target_fps` de la fuente. Lo pasa el runner, que conoce los dos. Escribirlo dos
    veces en el YAML seria una forma segura de que acabaran discrepando.
    """

    type: Literal["bytetrack"]
    lost_track_buffer: Annotated[int, Field(ge=1, le=300)] = 30
    track_activation_threshold: _Confianza = 0.7
    minimum_consecutive_frames: Annotated[int, Field(ge=1, le=10)] = 2
    minimum_iou_threshold: _Confianza = 0.1


# --------------------------------------------------------------------------------------
# `analyzers`
# --------------------------------------------------------------------------------------


class _AnalyzerBase(_ModeloBase):
    """Lo comun a los analizadores.

    `name` acaba en la columna `analyzer` de cada `Event` y de cada `Metric`, asi que los
    nombres efectivos tienen que ser unicos dentro del proyecto: dos analizadores con el
    mismo nombre hacen la serie temporal indistinguible.
    """

    type: str
    name: str | None = None
    every_n_frames: Annotated[int, Field(ge=1)] = 1

    @property
    def effective_name(self) -> str:
        """El `name` escrito o, si no lo hay, el propio `type`."""
        return self.name or self.type

    @field_validator("name")
    @classmethod
    def _nombre_no_vacio(cls, valor: str | None) -> str | None:
        if valor is not None and not valor.strip():
            raise ValueError("no puede estar vacío: es lo que identifica la serie temporal")
        return valor


class Line(_ModeloBase):
    """Una linea de conteo, en fracciones del fotograma.

    `positive_direction` es la etiqueta del sentido que cuenta como positivo; sale tal cual
    en las metricas, asi que se escribe como se quiera leer en el dashboard.
    """

    id: Annotated[str, Field(min_length=1)]
    p1: Point
    p2: Point
    positive_direction: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _linea_con_longitud(self) -> Line:
        if (self.p1.x, self.p1.y) == (self.p2.x, self.p2.y):
            raise ValueError(
                "p1 y p2 son el mismo punto, y una línea de longitud cero no la cruza nadie"
            )
        return self


class TrafficFlowAnalyzer(_AnalyzerBase):
    """Cruces de linea con sentido, conteo por minuto y densidad en escena."""

    type: Literal["traffic_flow"]
    lines: list[Line]

    @field_validator("lines")
    @classmethod
    def _lineas_utiles(cls, valor: list[Line]) -> list[Line]:
        if not valor:
            raise ValueError("no puede estar vacía: sin líneas no hay nada que contar")
        return valor

    @model_validator(mode="after")
    def _ids_unicos(self) -> TrafficFlowAnalyzer:
        vistos: dict[str, int] = {}
        for i, linea in enumerate(self.lines):
            if linea.id in vistos:
                raise ValueError(
                    f"el id de línea {linea.id!r} está repetido en lines[{vistos[linea.id]}] "
                    f"y lines[{i}], y es lo que distingue una serie temporal de otra"
                )
            vistos[linea.id] = i
        return self


class Hysteresis(_ModeloBase):
    """Cuanto tiene que aguantar un cambio antes de darlo por bueno.

    Sin esto la ocupacion parpadea con cada deteccion perdida (un autobus tapando media
    fila) y la grafica no vale para nada.

    Los dos umbrales son **independientes** y cada uno trae su valor por defecto: el
    bloque entero se puede omitir, y escribir solo uno de los dos es legal y deja el otro
    en el suyo. Son asimetricos a proposito (6 s para ocupar, 10 s para liberar): liberar
    una plaza que en realidad sigue ocupada es peor error que tardar de mas en marcarla
    libre, asi que quien ajuste uno de los dos no tiene por que tocar el otro.
    """

    to_occupied_s: Annotated[float, Field(gt=0)] = 6.0
    to_free_s: Annotated[float, Field(gt=0)] = 10.0


class ParkingOccupancyAnalyzer(_AnalyzerBase):
    """Ocupacion por poligono, con histeresis temporal.

    `zones_file` apunta a un `zones.json` cuyos poligonos van tambien **en fracciones**,
    por el mismo motivo que el resto de la geometria. Aqui solo se comprueba que el fichero
    existe: leerlo y validar su esquema es de quien implemente el analizador.
    """

    type: Literal["parking_occupancy"]
    zones_file: _RutaEntrada
    occupied_iou: _Confianza = 0.35
    hysteresis: Hysteresis = Field(default_factory=Hysteresis)


# --------------------------------------------------------------------------------------
# `sinks`
# --------------------------------------------------------------------------------------


class SqliteSink(_ModeloBase):
    """Persistencia. `url` es una URL de SQLAlchemy, no una ruta: no se resuelve.

    `batch_size` y `max_interval_s` son la ventana de escritura: el sink agrupa y escribe
    cuando junta `batch_size` registros o cuando pasan `max_interval_s` segundos desde el
    ultimo volcado, lo que ocurra antes. Son configurables porque son un intercambio que
    decide quien opera el sistema: **lo que se agrupa es lo que se pierde si el proceso
    muere de golpe** (`cvp.sinks.sqlite` lo detalla en su docstring).
    """

    type: Literal["sqlite"]
    url: str = "sqlite:///data/cvp.sqlite3"
    batch_size: Annotated[int, Field(ge=1)] = 200
    max_interval_s: Annotated[float, Field(gt=0)] = 5.0


class PreviewSink(_ModeloBase):
    """El ultimo fotograma en disco como JPEG; es lo que servira el MJPEG de #12.

    **No es un ring buffer en memoria**, aunque lo fuera en el diseno original: worker y API
    son procesos distintos y la memoria de uno no la lee el otro (decision 2 de #9). El
    fichero va en `data/preview/<id del proyecto>.jpg`, al lado de la base de datos y por el
    mismo motivo que ella: es una ruta conocida que los dos procesos derivan del proyecto
    sin ponerse de acuerdo.

    No tiene campos: en disco solo existe el ultimo fotograma y no hay nada que ajustar. El
    `max_frames` del ring buffer original se retiro en #9 en vez de dejarlo inerte, porque
    un campo que `cvp validate` bendice y que no hace nada es una trampa para quien lo
    escriba creyendo que configura algo.
    """

    type: Literal["preview"]


class JsonlSink(_ModeloBase):
    """Volcado a un fichero de lineas JSON, para depurar."""

    type: Literal["jsonl"]
    path: _RutaSalida


# --------------------------------------------------------------------------------------
# El registro de esquemas y las uniones discriminadas
# --------------------------------------------------------------------------------------

SourceConfig = Annotated[
    FileSource | YouTubeLiveSource | RtspSource | WebcamSource,
    Field(discriminator="type"),
]
DetectorConfig = Annotated[YoloDetector, Field(discriminator="type")]
TrackerConfig = Annotated[ByteTrackTracker, Field(discriminator="type")]
AnalyzerConfig = Annotated[
    TrafficFlowAnalyzer | ParkingOccupancyAnalyzer,
    Field(discriminator="type"),
]
SinkConfig = Annotated[SqliteSink | PreviewSink | JsonlSink, Field(discriminator="type")]

# Bloque -> nombre -> esquema. Es dato publico a proposito: el test de coherencia que
# comprueba que estos nombres y los del registro de implementaciones no se separan lo
# recorre desde fuera. El mundo es **cerrado**: un `type:` sin esquema es un error aunque
# alguien fuese a registrar la clase mas tarde, porque un fallo claro en el segundo 0 vale
# mas que un YAML que valida y revienta al construir el bloque.
CONFIG_SCHEMAS: Mapping[str, Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "source": MappingProxyType(
            {
                "file": FileSource,
                "rtsp": RtspSource,
                "webcam": WebcamSource,
                "youtube_live": YouTubeLiveSource,
            }
        ),
        "detector": MappingProxyType({"yolo": YoloDetector}),
        "tracker": MappingProxyType({"bytetrack": ByteTrackTracker}),
        "analyzer": MappingProxyType(
            {
                "parking_occupancy": ParkingOccupancyAnalyzer,
                "traffic_flow": TrafficFlowAnalyzer,
            }
        ),
        "sink": MappingProxyType(
            {
                "jsonl": JsonlSink,
                "preview": PreviewSink,
                "sqlite": SqliteSink,
            }
        ),
    }
)

# El campo del YAML en el que vive cada bloque. Sirve para dos cosas: dar la lista de
# nombres disponibles cuando el `type:` no existe, y quitar de la ruta del error el
# segmento que Pydantic mete con el nombre de la variante elegida.
_BLOQUE_POR_CAMPO = MappingProxyType(
    {
        "source": "source",
        "detector": "detector",
        "tracker": "tracker",
        "analyzers": "analyzer",
        "sinks": "sink",
    }
)


class ProjectConfig(_ModeloBase):
    """Un proyecto entero, ya validado, inmutable y con todas las rutas absolutas.

    Es el objeto contra el que programa el runner: si esta construido, ya no hay nada que
    volver a comprobar. `config_path` y `project_dir` no se escriben en el YAML, los
    rellena la carga a partir de la ruta del fichero.
    """

    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    name: Annotated[str, Field(min_length=1)]
    source: SourceConfig
    detector: DetectorConfig
    tracker: TrackerConfig
    # `analyzers` y `sinks` **pueden ser listas vacias**, y los dos siguen siendo campos
    # obligatorios: omitirlos es un error, escribir `[]` no. Hasta #8 una lista vacia se
    # rechazaba ("un proyecto sin analizadores no mide nada"); el criterio de #8 lo cambia,
    # porque `analyzers: []` con `sinks: []` es el modo con el que se mide el rendimiento
    # del encadenado completo antes de que existan #9, #10 y #11, y sin el no hay manera de
    # correr `cvp run` mientras no haya ni un analizador implementado. Lo que se pierde —un
    # `sinks: []` colado por descuido, que tira los datos a la basura en silencio— lo cubre
    # el runner, que avisa con un WARNING al arrancar en vez de callarse.
    analyzers: list[AnalyzerConfig]
    sinks: list[SinkConfig]
    config_path: Path
    project_dir: Path

    @field_validator("name")
    @classmethod
    def _nombre_no_vacio(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("no puede estar vacío: es lo que se ve en el dashboard")
        return valor


# --------------------------------------------------------------------------------------
# Fase 1: leer el fichero y parsear el YAML
# --------------------------------------------------------------------------------------


class _CargadorSinDuplicados(yaml.SafeLoader):
    """`SafeLoader` que ademas caza las claves repetidas y sabe donde estaba cada valor.

    Dos motivos para no usar `yaml.safe_load` a secas:

    - Con dos claves iguales en el mismo mapa se queda calladamente con la ultima. Un
      `analyzers:` escrito dos veces perdiendo el primero es el mismo fallo silencioso que
      un campo con una errata, con otra cara.
    - Construyendo el documento a mano se sabe la ruta (`analyzers[1].hysteresis`) de cada
      valor, que es lo que hace que el error se pueda leer sin abrir el fichero.

    Sigue derivando de `SafeLoader`: un fichero de configuracion no puede construir objetos
    arbitrarios de Python.
    """

    def construir(self, nodo: yaml.Node, problemas: list[str]) -> Any:
        """El documento como datos de Python, apuntando los duplicados en `problemas`."""
        return self._desde_nodo(nodo, "", problemas)

    def _desde_nodo(self, nodo: yaml.Node, ruta: str, problemas: list[str]) -> Any:
        if isinstance(nodo, yaml.MappingNode):
            return self._desde_mapa(nodo, ruta, problemas)
        if isinstance(nodo, yaml.SequenceNode):
            return [
                self._desde_nodo(hijo, f"{ruta}[{i}]", problemas)
                for i, hijo in enumerate(nodo.value)
            ]
        return self.construct_object(nodo, deep=True)

    def _desde_mapa(self, nodo: yaml.MappingNode, ruta: str, problemas: list[str]) -> Any:
        resultado: dict[str, Any] = {}
        lineas: dict[str, int] = {}
        for nodo_clave, nodo_valor in nodo.value:
            clave = self.construct_object(nodo_clave, deep=True)
            if not isinstance(clave, str):
                problemas.append(
                    f"{ruta or 'la raíz del documento'}: las claves de un project.yaml son "
                    f"nombres de campo y deben ser cadenas, y hay una que vale {_valor(clave)}"
                )
                continue
            hija = f"{ruta}.{clave}" if ruta else clave
            linea = nodo_clave.start_mark.line + 1
            if clave in resultado:
                problemas.append(
                    f"{hija}: clave duplicada (líneas {lineas[clave]} y {linea}); "
                    "en YAML gana la última en silencio y la primera se pierde"
                )
            else:
                lineas[clave] = linea
            resultado[clave] = self._desde_nodo(nodo_valor, hija, problemas)
        return resultado


def _motivo_yaml(exc: yaml.YAMLError) -> str:
    """El error de sintaxis con linea y columna, que `pyyaml` ya da gratis."""
    problema = getattr(exc, "problem", None) or str(exc)
    marca = getattr(exc, "problem_mark", None)
    if marca is None:
        return f"el YAML no es válido: {problema}"
    return f"el YAML no es válido (línea {marca.line + 1}, columna {marca.column + 1}): {problema}"


def _leer_documento(texto: str, pedido: str) -> dict[str, Any]:
    cargador = _CargadorSinDuplicados(texto)
    try:
        try:
            nodo = cargador.get_single_node()
        except yaml.YAMLError as exc:
            raise ConfigError(pedido, [_motivo_yaml(exc)]) from exc
        if nodo is None:
            raise ConfigError(
                pedido,
                [
                    "el fichero está vacío y un project.yaml necesita al menos "
                    "id, name, source, detector, tracker, analyzers y sinks"
                ],
            )
        if not isinstance(nodo, yaml.MappingNode):
            que_es = "una lista" if isinstance(nodo, yaml.SequenceNode) else "un valor suelto"
            raise ConfigError(
                pedido,
                [
                    f"la raíz de un project.yaml es un mapa de campos (id, name, source, ...) "
                    f"y aquí es {que_es}"
                ],
            )
        problemas: list[str] = []
        try:
            datos = cargador.construir(nodo, problemas)
        except yaml.YAMLError as exc:
            raise ConfigError(pedido, [_motivo_yaml(exc)]) from exc
        if problemas:
            raise ConfigError(pedido, _ordenados(problemas))
        return datos
    finally:
        cargador.dispose()


# --------------------------------------------------------------------------------------
# Fase 2: expandir las variables de entorno
# --------------------------------------------------------------------------------------

_NOMBRE_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _expandir_cadena(
    texto: str, entorno: Mapping[str, str], ruta: str, problemas: list[str]
) -> str:
    """Sustituye `${VAR}` y `${VAR:-por defecto}` dentro de `texto`.

    `$VAR` sin llaves **no** se expande: un `$` suelto es demasiado comun en contrasenas y
    en URLs como para tratarlo como sintaxis. `$${VAR}` produce el literal `${VAR}`.
    """
    if "$" not in texto:
        return texto
    partes: list[str] = []
    i = 0
    while i < len(texto):
        if texto.startswith("$${", i):
            partes.append("${")
            i += 3
            continue
        if not texto.startswith("${", i):
            partes.append(texto[i])
            i += 1
            continue
        cierre = texto.find("}", i + 2)
        if cierre == -1:
            problemas.append(
                f"{ruta}: hay un '${{' sin cerrar; una variable de entorno se escribe "
                "${VAR} o ${VAR:-valor por defecto}"
            )
            return texto
        cuerpo = texto[i + 2 : cierre]
        i = cierre + 1
        nombre, _, defecto = cuerpo.partition(":-")
        tiene_defecto = ":-" in cuerpo
        if not _NOMBRE_VARIABLE.match(nombre):
            problemas.append(
                f"{ruta}: {nombre!r} no es un nombre de variable de entorno válido "
                "(letras, dígitos y guion bajo, sin empezar por dígito)"
            )
            continue
        valor = entorno.get(nombre)
        if valor:
            partes.append(valor)
            continue
        if tiene_defecto:
            partes.append(defecto)
            continue
        if valor is None:
            problemas.append(f"{ruta}: la variable de entorno {nombre} no está definida")
        else:
            problemas.append(
                f"{ruta}: la variable de entorno {nombre} está definida pero vacía, que se "
                "trata igual que ausente; escribe ${" + nombre + ":-valor} si el vacío es "
                "intencionado"
            )
    return "".join(partes)


def _expandir_valor(dato: Any, entorno: Mapping[str, str], ruta: str, problemas: list[str]) -> Any:
    """Expande en cualquier profundidad, listas incluidas. En las claves **no**.

    Se expande sobre los valores de cadena **ya parseados**, nunca sobre el texto crudo del
    fichero: preprocesar el texto dejaria que el valor de una variable de entorno inyecte
    sintaxis YAML y cambie la estructura del documento.
    """
    if isinstance(dato, str):
        return _expandir_cadena(dato, entorno, ruta, problemas)
    if isinstance(dato, Mapping):
        return {
            clave: _expandir_valor(
                valor, entorno, f"{ruta}.{clave}" if ruta else str(clave), problemas
            )
            for clave, valor in dato.items()
        }
    if isinstance(dato, list):
        return [
            _expandir_valor(valor, entorno, f"{ruta}[{i}]", problemas)
            for i, valor in enumerate(dato)
        ]
    return dato


def _expandir_entorno(
    datos: dict[str, Any], entorno: Mapping[str, str], pedido: str
) -> dict[str, Any]:
    problemas: list[str] = []
    expandido = _expandir_valor(datos, entorno, "", problemas)
    if problemas:
        raise ConfigError(pedido, _ordenados(problemas))
    return expandido


# --------------------------------------------------------------------------------------
# Fase 3: esquema, rutas y coherencia
# --------------------------------------------------------------------------------------

# Los campos de `Point` y de `Region` se escriben en el YAML como una lista, asi que el
# error se senala con el indice (`detector.roi[2]`) y no con el nombre interno del campo.
_INDICE_GEOMETRIA = MappingProxyType({"x": 0, "y": 1, "x1": 0, "y1": 1, "x2": 2, "y2": 3})


def _ruta_de_loc(loc: Sequence[str | int]) -> str:
    """La ruta del campo tal y como se escribe en el YAML.

    Puntos para los mapas, `[i]` para las listas, empezando en la raiz del documento. Se
    quita el segmento que Pydantic mete con el nombre de la variante de la union
    discriminada (`source.youtube_live.target_fps` -> `source.target_fps`): es un detalle
    interno de la validacion, no un campo del fichero.
    """
    segmentos = list(loc)
    bloque = _BLOQUE_POR_CAMPO.get(str(segmentos[0])) if segmentos else None
    if bloque is not None:
        posicion = 2 if len(segmentos) > 1 and isinstance(segmentos[1], int) else 1
        if (
            len(segmentos) > posicion
            and isinstance(segmentos[posicion], str)
            and segmentos[posicion] in CONFIG_SCHEMAS[bloque]
        ):
            del segmentos[posicion]
    partes: list[str] = []
    for segmento in segmentos:
        if isinstance(segmento, int):
            partes.append(f"[{segmento}]")
        elif segmento in _INDICE_GEOMETRIA:
            partes.append(f"[{_INDICE_GEOMETRIA[segmento]}]")
        elif partes:
            partes.append(f".{segmento}")
        else:
            partes.append(str(segmento))
    return "".join(partes)


def _motivo(error: Mapping[str, Any]) -> str:
    """Traduce un error de Pydantic a una linea que se entiende sin saber que es Pydantic."""
    tipo = error["type"]
    ctx = error.get("ctx") or {}
    entrada = error.get("input")
    match tipo:
        case "missing":
            return "campo obligatorio que falta"
        case "extra_forbidden":
            return "campo desconocido; en un project.yaml casi siempre es una errata"
        case "value_error" | "assertion_error":
            mensaje = str(error["msg"])
            return mensaje.removeprefix("Value error, ").removeprefix("Assertion failed, ")
        case "greater_than":
            return f"debe ser > {_limite(ctx.get('gt'))}, y vale {_valor(entrada)}"
        case "greater_than_equal":
            return f"debe ser >= {_limite(ctx.get('ge'))}, y vale {_valor(entrada)}"
        case "less_than":
            return f"debe ser < {_limite(ctx.get('lt'))}, y vale {_valor(entrada)}"
        case "less_than_equal":
            return f"debe ser <= {_limite(ctx.get('le'))}, y vale {_valor(entrada)}"
        case "string_too_short" | "string_too_long":
            return f"no puede estar vacío, y vale {_valor(entrada)}"
        case "string_pattern_mismatch":
            return f"debe encajar en {ctx.get('pattern')}, y vale {_valor(entrada)}"
        case "too_short":
            return (
                f"debe tener al menos {ctx.get('min_length')} elementos, "
                f"y tiene {ctx.get('actual_length')}"
            )
        case "too_long":
            return (
                f"debe tener como mucho {ctx.get('max_length')} elementos, "
                f"y tiene {ctx.get('actual_length')}"
            )
        case "int_parsing" | "int_type" | "int_from_float":
            return f"debe ser un número entero, y vale {_valor(entrada)}"
        case "float_parsing" | "float_type":
            return f"debe ser un número, y vale {_valor(entrada)}"
        case "bool_parsing" | "bool_type":
            return f"debe ser true o false, y vale {_valor(entrada)}"
        case "string_type":
            return f"debe ser una cadena, y vale {_valor(entrada)}"
        case "list_type":
            return f"debe ser una lista, y vale {_valor(entrada)}"
        case "dict_type" | "model_type" | "model_attributes_type":
            return f"debe ser un mapa de campos, y vale {_valor(entrada)}"
        case "path_type":
            return f"debe ser una ruta, y vale {_valor(entrada)}"
        case _:
            return f"{error['msg']}, y vale {_valor(entrada)}"


def _linea_de_error(error: Mapping[str, Any]) -> str:
    """Una linea del bloque: ruta del campo, motivo y, cuando lo hay, valor ofensivo."""
    loc: tuple[str | int, ...] = tuple(error["loc"])
    tipo = error["type"]
    if tipo in ("union_tag_invalid", "union_tag_not_found"):
        campo = str(loc[0]) if loc else ""
        bloque = _BLOQUE_POR_CAMPO.get(campo, campo)
        disponibles = ", ".join(sorted(CONFIG_SCHEMAS.get(bloque, {})))
        ruta = _ruta_de_loc((*loc, "type"))
        if tipo == "union_tag_invalid":
            pedido = (error.get("ctx") or {}).get("tag")
            return f"{ruta}: no hay ningún {bloque} llamado {pedido!r}. Disponibles: {disponibles}"
        return (
            f"{ruta}: campo obligatorio que falta; es lo que dice qué {bloque} es. "
            f"Disponibles: {disponibles}"
        )
    ruta = _ruta_de_loc(loc) or "el documento"
    return f"{ruta}: {_motivo(error)}"


def _problemas_de_coherencia(config: ProjectConfig, nombre_directorio: str | None) -> list[str]:
    """Lo que no se puede comprobar campo a campo, sino mirando el proyecto entero."""
    problemas: list[str] = []
    if nombre_directorio is not None and config.id != nombre_directorio:
        problemas.append(
            f"id: el proyecto está en el directorio {nombre_directorio!r} pero su id es "
            f"{config.id!r}; en el dashboard se identifica por el id y en el disco por la "
            "carpeta, y que discrepen es un bug garantizado"
        )
    vistos: dict[str, int] = {}
    for i, analizador in enumerate(config.analyzers):
        nombre = analizador.effective_name
        if nombre in vistos:
            problemas.append(
                f"analyzers[{i}].name: el nombre efectivo {nombre!r} ya lo usa "
                f"analyzers[{vistos[nombre]}]; es la columna `analyzer` de cada Event y de "
                "cada Metric, y repetido hace la serie temporal indistinguible"
            )
        else:
            vistos[nombre] = i
    tipos: dict[str, int] = {}
    for i, sink in enumerate(config.sinks):
        if sink.type in tipos:
            problemas.append(
                f"sinks[{i}].type: ya hay un sink {sink.type!r} en sinks[{tipos[sink.type]}]; "
                "dos sinks iguales escribirían la misma ejecución dos veces"
            )
        else:
            tipos[sink.type] = i
    return problemas


def _validar(
    datos: dict[str, Any],
    *,
    pedido: str,
    config_path: Path,
    project_dir: Path,
    nombre_directorio: str | None,
) -> ProjectConfig:
    problemas: list[str] = []
    documento = dict(datos)
    for campo in _CAMPOS_DERIVADOS:
        if campo in documento:
            del documento[campo]
            problemas.append(
                f"{campo}: campo desconocido; lo rellena la carga a partir de la ruta del "
                f"{NOMBRE_FICHERO}"
            )
    documento["config_path"] = config_path
    documento["project_dir"] = project_dir
    try:
        config = ProjectConfig.model_validate(documento, context={"project_dir": project_dir})
    except ValidationError as exc:
        problemas.extend(_linea_de_error(error) for error in exc.errors(include_url=False))
        raise ConfigError(pedido, _ordenados(problemas)) from exc
    problemas.extend(_problemas_de_coherencia(config, nombre_directorio))
    if problemas:
        raise ConfigError(pedido, _ordenados(problemas))
    return config


# --------------------------------------------------------------------------------------
# La carga
# --------------------------------------------------------------------------------------

_TROZOS = re.compile(r"(\d+)")


def _clave_orden(problema: str) -> tuple[tuple[int, int | str], ...]:
    """Ordena las lineas por ruta de campo, con los indices en orden numerico.

    Sin esto `analyzers[10]` saldria antes que `analyzers[2]`, que es justo el tipo de
    detalle que hace dudar de si la lista esta completa.
    """
    ruta = problema.split(":", 1)[0]
    return tuple(
        (1, int(trozo)) if trozo.isdigit() else (0, trozo) for trozo in _TROZOS.split(ruta) if trozo
    )


def _ordenados(problemas: Iterable[str]) -> list[str]:
    return sorted(problemas, key=_clave_orden)


def _localizar(path: str | Path, pedido: str) -> tuple[Path, str | None]:
    """Encuentra el `project.yaml` y dice si se pidio por directorio.

    Aceptar las dos formas es lo que permite `cvp validate projects/01-parking-trafico` y
    `cvp validate ruta/al/fichero.yaml` sin que quien llama tenga que saber cual es cual.
    """
    ruta = Path(path).expanduser()
    if ruta.is_dir():
        fichero = ruta / NOMBRE_FICHERO
        if not fichero.is_file():
            raise ConfigError(
                pedido,
                [
                    f"no existe {fichero.resolve()}: un directorio de proyecto tiene que "
                    f"llevar su {NOMBRE_FICHERO}"
                ],
            )
        return fichero.resolve(), fichero.resolve().parent.name
    if not ruta.is_file():
        raise ConfigError(
            pedido,
            [f"no existe el fichero de configuración {ruta.resolve()}"],
        )
    return ruta.resolve(), None


def load_project_config(path: str | Path, *, env: Mapping[str, str] | None = None) -> ProjectConfig:
    """Lee, valida y devuelve la configuracion de un proyecto.

    `path` puede ser el `project.yaml` o el directorio del proyecto, en cuyo caso se carga
    el `project.yaml` de dentro y se exige que el `id` coincida con el nombre de la
    carpeta. `env` es el mapping de variables de entorno (`None` = `os.environ`); existe
    para que los tests sean deterministas sin tocar el entorno del proceso, igual que los
    timestamps se inyectan en vez de llamar a `datetime.now()`. **No lee `.env`** ni
    modifica `os.environ`.

    Lo unico que lanza es `ConfigError`, con **todos** los errores de la primera fase que
    falle: (1) lectura y sintaxis del YAML, (2) expansion de variables de entorno,
    (3) esquema, rutas y coherencia. Validar el esquema de un documento con huecos sin
    resolver no dice nada util, asi que las fases no se mezclan.

    No importa nada pesado: ni torch, ni OpenCV, ni ultralytics, ni yt-dlp. Validar cuesta
    milisegundos y ese es justamente el punto.
    """
    pedido = path if isinstance(path, str) else str(path)
    entorno = os.environ if env is None else env
    config_path, nombre_directorio = _localizar(path, pedido)
    try:
        texto = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(pedido, [f"no se puede leer {config_path}: {exc.strerror}"]) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(pedido, [f"{config_path} no está en UTF-8: {exc}"]) from exc
    datos = _leer_documento(texto, pedido)
    datos = _expandir_entorno(datos, entorno, pedido)
    return _validar(
        datos,
        pedido=pedido,
        config_path=config_path,
        project_dir=config_path.parent,
        nombre_directorio=nombre_directorio,
    )
