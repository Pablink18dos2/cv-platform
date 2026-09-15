"""El analizador de trafico: cruces de linea con sentido y vehiculos en escena.

Es el primer analizador y el molde de los siguientes, asi que las tres decisiones que lo
forman van escritas aqui arriba (el detalle esta en #10):

1. **La geometria se resuelve en el primer fotograma, no en la construccion.** La fabrica
   construye los analizadores antes de abrir la fuente, asi que `from_config` no conoce la
   resolucion. `GeometriaPorFotograma` (compartida, en `cvp.analyzers.geometry`) convierte
   las lineas a pixeles con `frame.image.shape` la primera vez, y reconvierte con un
   WARNING si la forma cambia.

2. **El punto de referencia de un vehiculo es `bbox.bottom_center`**, donde toca el suelo.
   Con la camara elevada del proyecto 01, el centro de la caja cruza la linea varios metros
   antes que las ruedas.

3. **Un cruce es un cambio de lado confirmado.** El lado de un track respecto a una linea
   solo se da por cambiado cuando lleva `min_frames_on_side` observaciones consecutivas en
   el nuevo lado, y se emite un cruce cuando cambia el lado **confirmado**. Sin esto, un
   coche aparcado con la caja temblando sobre la linea daria decenas de cruces por minuto,
   y cada uno pareceria legitimo por separado. Un punto exactamente sobre la linea no es
   evidencia de nada y conserva el lado anterior.

Los estados son **por track y por linea**, y el de un track que lleva mas de
`forget_after_frames` fotogramas sin verse se olvida: sin eso la memoria creceria con cada
vehiculo que pasa, para siempre.

Todos los timestamps salen de `frame.timestamp`. Aqui no hay reloj.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import numpy as np

from cvp.analyzers.drawing import (
    AMARILLO,
    CIAN,
    NEGRO,
    ROJO,
    copia_para_anotar,
    dibujar_etiqueta,
    dibujar_linea,
    dibujar_punto,
)
from cvp.analyzers.geometry import GeometriaPorFotograma, LineaDeConteo, LineaEnPixeles
from cvp.core import AnalyzerOutput, Detection, Event, Frame, Metric, Track
from cvp.core.registry import ANALYZERS

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import TrafficFlowAnalyzer as TrafficFlowConfig

__all__ = ["TrafficFlowAnalyzer"]

# Los nombres que salen en la base de datos y en el dashboard. Son contrato de #10.
EVENTO_CRUCE = "line_crossing"
METRICA_CRUCES = "crossings"
METRICA_EN_ESCENA = "vehicles_in_scene"

# Los mismos valores por defecto que el docstring del issue, para que construir el
# analizador a mano y construirlo desde el YAML den lo mismo.
MIN_FRAMES_ON_SIDE_POR_DEFECTO = 2
FORGET_AFTER_FRAMES_POR_DEFECTO = 60


@dataclass(slots=True)
class _LadoDeLinea:
    """El lado de **un** track respecto a **una** linea, con la confirmacion a medias.

    `confirmado` es el lado que se da por bueno (0 = todavia ninguno: el track acaba de
    aparecer). `candidato` y `racha` son el lado que se esta viendo ultimamente y cuantas
    veces seguidas.
    """

    confirmado: int = 0
    candidato: int = 0
    racha: int = 0

    def observar(self, lado: int, minimo: int) -> tuple[int, int] | None:
        """Registra una observacion y devuelve `(desde, hacia)` si hay cruce confirmado.

        Un lado se confirma cuando se ve `minimo` veces **seguidas**; una observacion en
        el lado ya confirmado rompe la racha del otro. Y solo hay cruce cuando el lado
        confirmado cambia de un lado a otro: el primer lado que se confirma no viene de
        ninguna parte, es el coche entrando en escena.

        El `0` (sobre la linea) no cuenta ni a favor ni en contra: ni confirma, ni rompe
        la racha, ni cambia nada. Es lo que hace que un punto exactamente sobre la linea
        conserve el lado que tenia.
        """
        if lado == 0:
            return None
        if lado == self.confirmado:
            self.candidato, self.racha = 0, 0
            return None
        if lado == self.candidato:
            self.racha += 1
        else:
            self.candidato, self.racha = lado, 1
        if self.racha < minimo:
            return None
        anterior, self.confirmado = self.confirmado, lado
        self.candidato, self.racha = 0, 0
        if anterior == 0:
            return None
        return (anterior, lado)


@dataclass(slots=True)
class _EstadoDeTrack:
    ultimo_visto: int
    lados: dict[str, _LadoDeLinea] = field(default_factory=dict)


@ANALYZERS.register("traffic_flow")
class TrafficFlowAnalyzer:
    """Cuenta los vehiculos que cruzan cada linea, por sentido, y mide cuantos hay en escena.

    Emite, por cada cruce confirmado, un `Event("line_crossing")` con `line`, `direction`,
    `track_id` y `label`, y una `Metric("crossings", 1.0)` con `line` y `direction` como
    etiquetas: es lo que hace que la agregacion por minuto de #9 de **vehiculos por minuto
    por sentido** con su `sum_value`, sin que el dashboard tenga que agrupar eventos crudos.
    Y en cada `process()`, una `Metric("vehicles_in_scene", len(tracks))`, cuya media
    ponderada por tiempo es la densidad.

    **`label` en los eventos no es fiable para desglosar por tipo de vehiculo** en el
    proyecto 01: los autobuses salen como `car` a 640 o como `truck` a 960, y la clase `bus`
    no aparece nunca (`_docs/rendimiento.md`, medicion de #6). Se guarda porque no cuesta
    nada y porque en otra escena puede valer; el conteo **total** si es estable.

    `name` es el del YAML (`effective_name`) y es lo que va en `analyzer` de cada evento y
    de cada metrica: dos analizadores de trafico en el mismo proyecto tienen que
    distinguirse. `min_frames_on_side` y `forget_after_frames` se expresan en fotogramas,
    no en pixeles ni en segundos, para no depender de la resolucion; el primero cuenta
    observaciones consecutivas de este analizador y el segundo, `frame.index` de la fuente,
    que no se reinicia al reconectar.
    """

    def __init__(
        self,
        lines: Iterable[LineaDeConteo],
        *,
        name: str = "traffic_flow",
        every_n_frames: int = 1,
        min_frames_on_side: int = MIN_FRAMES_ON_SIDE_POR_DEFECTO,
        forget_after_frames: int = FORGET_AFTER_FRAMES_POR_DEFECTO,
    ) -> None:
        if not name.strip():
            raise ValueError("name no puede estar vacio: es la columna `analyzer` de cada serie")
        if every_n_frames < 1:
            raise ValueError(f"every_n_frames es una cadencia >= 1, no {every_n_frames!r}")
        if min_frames_on_side < 1:
            raise ValueError(
                f"min_frames_on_side son observaciones >= 1, no {min_frames_on_side!r}"
            )
        if forget_after_frames < 1:
            raise ValueError(f"forget_after_frames son fotogramas >= 1, no {forget_after_frames!r}")
        lineas = tuple(lines)
        if not lineas:
            raise ValueError("sin lineas no hay nada que contar")
        ids = [linea.id for linea in lineas]
        repetidos = sorted({i for i in ids if ids.count(i) > 1})
        if repetidos:
            raise ValueError(f"hay lineas con el mismo id: {repetidos}")

        self.name = name
        self.every_n_frames = int(every_n_frames)
        self.min_frames_on_side = int(min_frames_on_side)
        self.forget_after_frames = int(forget_after_frames)
        self.lines = lineas
        self._geometria: GeometriaPorFotograma[tuple[LineaEnPixeles, ...]] = GeometriaPorFotograma(
            self._a_pixeles, nombre=name
        )
        self._tracks: dict[int, _EstadoDeTrack] = {}
        self._contadores: dict[tuple[str, str], int] = {
            (linea.id, sentido): 0
            for linea in lineas
            for sentido in (linea.positive_direction, linea.negative_direction)
        }

    @classmethod
    def from_config(cls, cfg: TrafficFlowConfig, **kwargs: Any) -> TrafficFlowAnalyzer:
        """Construye desde el bloque `analyzers` del `project.yaml`. Es el unico puente
        con la capa de configuracion: aqui `Line` se convierte en `LineaDeConteo`."""
        return cls(
            [
                LineaDeConteo(
                    id=linea.id,
                    p1=(linea.p1.x, linea.p1.y),
                    p2=(linea.p2.x, linea.p2.y),
                    positive_direction=linea.positive_direction,
                    negative_direction=linea.negative_direction,
                )
                for linea in cfg.lines
            ],
            name=cfg.effective_name,
            every_n_frames=cfg.every_n_frames,
            **kwargs,
        )

    # --- Lo que se puede consultar ------------------------------------------------------

    @property
    def contadores(self) -> Mapping[tuple[str, str], int]:
        """Cruces acumulados por `(id de linea, sentido)` desde el arranque."""
        return MappingProxyType(self._contadores)

    @property
    def tracks_recordados(self) -> int:
        """Cuantos tracks tienen estado en memoria. Es lo que `forget_after_frames` acota."""
        return len(self._tracks)

    @property
    def resolucion(self) -> tuple[int, int] | None:
        """`(width, height)` con la que estan convertidas las lineas, o `None` antes del
        primer `process()`."""
        return self._geometria.resolucion

    # --- process --------------------------------------------------------------------------

    def process(
        self, frame: Frame, tracks: list[Track], detections: list[Detection]
    ) -> AnalyzerOutput:
        """Cuenta cruces sobre los **tracks** e ignora `detections` a proposito.

        Un cruce necesita identidad: sin `track_id` se contaria el mismo coche una vez
        por fotograma. Las detecciones crudas estan en la firma porque `parking_occupancy`
        (#11) las necesita —una plaza esta ocupada aunque el coche de dentro no tenga
        identidad confirmada— y un parametro que este analizador no usa cuesta menos que
        un caso especial en el runner.
        """
        del detections
        lineas = self._geometria.para(frame)
        eventos: list[Event] = []
        metricas: list[Metric] = []

        for track in tracks:
            estado = self._tracks.get(track.track_id)
            if estado is None:
                estado = self._tracks[track.track_id] = _EstadoDeTrack(ultimo_visto=frame.index)
            estado.ultimo_visto = frame.index
            punto = track.bbox.bottom_center
            for linea in lineas:
                lado = estado.lados.get(linea.id)
                if lado is None:
                    lado = estado.lados[linea.id] = _LadoDeLinea()
                cruce = lado.observar(linea.lado_de(punto), self.min_frames_on_side)
                if cruce is None:
                    continue
                sentido = linea.sentido_hacia(cruce[1])
                self._contadores[(linea.id, sentido)] += 1
                eventos.append(
                    Event(
                        analyzer=self.name,
                        name=EVENTO_CRUCE,
                        timestamp=frame.timestamp,
                        attributes={
                            "line": linea.id,
                            "direction": sentido,
                            "track_id": track.track_id,
                            "label": track.label,
                        },
                    )
                )
                metricas.append(
                    Metric(
                        analyzer=self.name,
                        name=METRICA_CRUCES,
                        value=1.0,
                        timestamp=frame.timestamp,
                        labels={"line": linea.id, "direction": sentido},
                    )
                )

        self._olvidar(frame.index)
        metricas.append(
            Metric(
                analyzer=self.name,
                name=METRICA_EN_ESCENA,
                value=float(len(tracks)),
                timestamp=frame.timestamp,
            )
        )
        return AnalyzerOutput(events=eventos, metrics=metricas)

    def _olvidar(self, indice: int) -> None:
        """Suelta el estado de los tracks que llevan mas de `forget_after_frames` sin verse."""
        olvidados = [
            track_id
            for track_id, estado in self._tracks.items()
            if indice - estado.ultimo_visto > self.forget_after_frames
        ]
        for track_id in olvidados:
            del self._tracks[track_id]

    def _a_pixeles(self, width: int, height: int) -> tuple[LineaEnPixeles, ...]:
        return tuple(linea.to_pixels(width, height) for linea in self.lines)

    # --- annotate -------------------------------------------------------------------------

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray:
        """Cada linea con su id y sus contadores, y el punto de apoyo de cada track.

        Devuelve un array nuevo; el que recibe queda intacto. Convierte las lineas con la
        forma de **esta** imagen y sin tocar la geometria de `process`: un preview
        reescalado no puede disparar el aviso de cambio de forma ni desplazar las lineas
        con las que se cuenta.
        """
        lienzo = copia_para_anotar(image)
        alto, ancho = lienzo.shape[:2]
        for linea in self._a_pixeles(ancho, alto):
            dibujar_linea(lienzo, linea.p1, linea.p2, color=AMARILLO)
            # Una marca en el lado positivo, para que al colocar la linea se vea hacia
            # donde cuenta `positive_direction`.
            dibujar_punto(lienzo, _hacia_el_lado_positivo(linea, 18), color=AMARILLO, radio=5)
            texto = (
                f"{linea.id}  {linea.positive_direction}: "
                f"{self._contadores[(linea.id, linea.positive_direction)]}  "
                f"{linea.negative_direction}: "
                f"{self._contadores[(linea.id, linea.negative_direction)]}"
            )
            medio = linea.punto_medio
            dibujar_etiqueta(lienzo, texto, (medio[0] - 120, medio[1] - 44), color_fondo=AMARILLO)
        for track in tracks:
            punto = track.bbox.bottom_center
            dibujar_punto(lienzo, punto, color=ROJO)
            dibujar_etiqueta(
                lienzo,
                str(track.track_id),
                (punto[0] + 10, punto[1] - 30),
                color_fondo=CIAN,
                color_texto=NEGRO,
                escala=0.5,
                grosor=1,
            )
        return lienzo


def _hacia_el_lado_positivo(linea: LineaEnPixeles, distancia: float) -> tuple[float, float]:
    """Un punto a `distancia` pixeles del punto medio, hacia el lado positivo de la linea.

    La normal a la derecha de `p1 -> p2` en coordenadas de imagen es `(-dy, dx)`; es la
    misma convencion de signo que `lado_de`, y por eso se calcula aqui y no a ojo.
    """
    dx, dy = linea.p2[0] - linea.p1[0], linea.p2[1] - linea.p1[1]
    largo = float(np.hypot(dx, dy)) or 1.0
    medio = linea.punto_medio
    return (medio[0] - dy / largo * distancia, medio[1] + dx / largo * distancia)
