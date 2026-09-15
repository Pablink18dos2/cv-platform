"""Los cinco bloques del sistema, como `Protocol`.

Un bloque es cualquier objeto que encaje en uno de estos protocolos; no hay clases base
de las que heredar. Es lo que permite que un connector, un modelo o un analizador nuevos
sean un fichero nuevo y un decorador, sin tocar el pipeline.

Los cinco son `@runtime_checkable`, asi que `isinstance` comprueba que los metodos
existan. **Solo eso**: no comprueba las firmas. Verificarlas de verdad es trabajo del
comprobador estatico de tipos, que llega en el issue 18. Y con `Analyzer`, que tiene
atributos ademas de metodos (es un "data protocol"), `issubclass` no esta soportado: hay
que preguntar por instancias.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

import numpy as np

from cvp.core.types import AnalyzerOutput, BBox, Detection, Frame, SourceInfo, Track

__all__ = ["Analyzer", "Detector", "Sink", "Tracker", "VideoSource"]


@runtime_checkable
class VideoSource(Protocol):
    """De donde vienen los fotogramas: un fichero, un directo de YouTube, RTSP, la webcam.

    El muestreo a `target_fps` se hace descartando fotogramas al leerlos, nunca
    encolandolos: con una fuente en directo, una cola solo consigue que el analisis se
    retrase cada vez mas respecto a la realidad.
    """

    @property
    def info(self) -> SourceInfo: ...

    def open(self) -> None: ...

    def frames(self) -> Iterator[Frame]: ...

    def close(self) -> None: ...


@runtime_checkable
class Detector(Protocol):
    """Que hay en el fotograma. Devuelve `Detection` ya normalizada."""

    def warmup(self) -> None: ...

    def detect(self, frame: Frame, roi: BBox | None = None) -> list[Detection]:
        """Detecta objetos en `frame`, opcionalmente restringido a una region.

        El `roi` existe porque recortar rinde mas que agrandar: sobre la escena del
        proyecto 01, recortar la region util e inferir a 640 da 5,6 fps y detecta mas
        vehiculos en esa region que el fotograma completo a 1280, que da 2,7 fps
        (`_docs/rendimiento.md`). No es optimizacion prematura: es el doble de fps.

        Contrato del ROI:

        - `roi` va en **pixeles del fotograma original**, no en fracciones 0-1 ni en
          coordenadas del recorte. Quien quiera hablar en fracciones (el YAML) convierte
          antes de llegar aqui.
        - `roi=None` significa fotograma completo.
        - Las `Detection` devueltas estan **siempre en el sistema de coordenadas del
          fotograma original**, haya ROI o no. Quien recorta, traduce de vuelta
          (`BBox.translated`); el analizador no se entera de que hubo un recorte.
        - El ROI se recorta a los limites del fotograma con `BBox.clipped`, y un ROI sin
          interseccion con el fotograma lanza `ValueError`.
        - `roi` es un argumento **por llamada**: el detector no guarda el ROI como
          estado, para que dos analizadores puedan pedir regiones distintas del mismo
          fotograma sin pisarse.
        """
        ...


@runtime_checkable
class Tracker(Protocol):
    """Quien es quien entre fotogramas. Sin identidad estable no hay conteo."""

    def update(self, frame: Frame, detections: list[Detection]) -> list[Track]: ...

    def reset(self) -> None: ...


@runtime_checkable
class Analyzer(Protocol):
    """La logica de negocio, y el unico bloque que un proyecto nuevo suele tocar.

    No sabe de donde vino el fotograma ni que modelo lo detecto: solo ve `Frame`,
    `list[Track]` y `list[Detection]`. Por eso se testea con listas escritas a mano, sin
    video, sin modelo y sin red.

    **`process` recibe los tracks y tambien las detecciones crudas, y esa firma es una
    decision de #11.** Son dos preguntas distintas: los tracks responden a *quien es quien*
    (identidad, lo que el conteo de trafico necesita para no contar el mismo coche dos
    veces) y las detecciones a *que hay* (presencia, lo que la ocupacion de una plaza
    necesita). El tracker **descarta lo que no confirma** —en la escena del proyecto 01,
    con el umbral por defecto sigue 5,4 de los ~14 vehiculos que el detector ve
    (`_docs/rendimiento.md`, #7)—, asi que un analizador de presencia construido sobre
    tracks daria una ocupacion sistematicamente baja sin que nada lo delatara. Y el umbral
    de activacion del tracker es un mando global que no se puede poner a la vez alto para
    uno y bajo para el otro. Mismo criterio que `Sink.write(frame)`: un parametro que
    algunas implementaciones ignoran cuesta menos que un caso especial en el runner.
    `traffic_flow` ignora las detecciones y lo dice; `parking_occupancy` ignora los tracks.

    `every_n_frames` es su propio submuestreo: la ocupacion de un parking no cambia en
    dos segundos y no hace falta recalcularla en cada fotograma.
    """

    name: str
    every_n_frames: int

    def process(
        self, frame: Frame, tracks: list[Track], detections: list[Detection]
    ) -> AnalyzerOutput: ...

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray: ...


@runtime_checkable
class Sink(Protocol):
    """Donde acaba el resultado: la base de datos, un JSONL de depuracion, el preview.

    `run_id` identifica la ejecucion concreta del worker, para poder distinguir dos
    arranques del mismo proyecto. Su formato canonico lo fija `cvp.storage.run_id` (#9).

    **`write` recibe tambien el fotograma, y esa firma es una decision de #9.** El sink
    `preview` tiene que guardar el ultimo fotograma y en `AnalyzerOutput` no hay ninguno,
    asi que habia dos salidas: pasar el fotograma a todos los sinks —y que `sqlite` reciba
    uno que ignora— o sacar el `preview` del registro y cablearlo aparte en el runner. Se
    eligio lo primero: un parametro que algunas implementaciones ignoran cuesta mucho menos
    que un `if tipo == "preview"` en el orquestador, que es exactamente lo que el registro
    por nombre existe para evitar y lo que `_docs/architecture.md` prohibe. El caso especial
    se pagaria ademas cada vez que alguien anadiera un bloque, y el parametro de mas no.

    `frame` es el fotograma que produjo esta salida, **sin anotar**: quien dibuja son los
    analizadores (`Analyzer.annotate`, #10 y #11).
    """

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None: ...

    def flush(self) -> None: ...
