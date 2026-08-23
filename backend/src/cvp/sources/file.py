"""Connector de fichero de video local.

Es la primera implementacion de `VideoSource` y la que usan los tests de todos los demas
bloques: determinista, offline y de 101 KB.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2

from cvp.core import Frame, SourceInfo
from cvp.core.registry import SOURCES
from cvp.sources.errors import SourceError

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import FileSource


@SOURCES.register("file")
class FileVideoSource:
    """Recorre un fichero de video local emitiendo `Frame` muestreados a `target_fps`.

    Toma argumentos planos y no un modelo de configuracion: un adaptador tiene que poder
    instanciarse en un test con dos argumentos y sin YAML de por medio. El puente con la
    capa de configuracion es `from_config`, y es el unico.
    """

    def __init__(
        self,
        path: Path | str,
        target_fps: float,
        *,
        loop: bool = False,
        start_time: datetime | None = None,
    ) -> None:
        if target_fps <= 0:
            raise ValueError(f"target_fps debe ser > 0, no {target_fps!r}")
        self.path = Path(path)
        self.target_fps = float(target_fps)
        self.loop = loop
        # El unico sitio del modulo donde se consulta el reloj, y queda fuera de todo lo
        # que se testea: los tests inyectan `start_time` y comparan secuencias exactas.
        self._start_time = start_time if start_time is not None else datetime.now(UTC)
        self._captura: Any | None = None
        self._info: SourceInfo | None = None

    @classmethod
    def from_config(cls, cfg: FileSource, *, start_time: datetime | None = None) -> FileVideoSource:
        """Construye desde el modelo de configuracion de #3. Lo usara el runner de #8."""
        return cls(cfg.path, cfg.target_fps, loop=cfg.loop, start_time=start_time)

    @property
    def info(self) -> SourceInfo:
        if self._info is None:
            raise SourceError(f"hay que llamar a open() antes de pedir info ({self.path})")
        return self._info

    def open(self) -> None:
        if self._captura is not None:
            return
        if not self.path.exists():
            raise SourceError(f"el fichero de video no existe: {self.path}")

        captura = cv2.VideoCapture(str(self.path))
        if not captura.isOpened():
            captura.release()
            raise SourceError(f"OpenCV no ha podido abrir el fichero como video: {self.path}")

        fps = captura.get(cv2.CAP_PROP_FPS)
        ancho = int(captura.get(cv2.CAP_PROP_FRAME_WIDTH))
        alto = int(captura.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if fps is None or math.isnan(fps) or fps <= 0:
            captura.release()
            # Emitirlo todo en silencio convertiria el contrato de muestreo en una mentira:
            # `target_fps: 5` diria 5 y entregaria 30.
            raise SourceError(
                f"OpenCV no sabe el frame rate de {self.path} (dice {fps!r}), asi que no se "
                "puede honrar target_fps"
            )
        if ancho <= 0 or alto <= 0:
            captura.release()
            raise SourceError(f"OpenCV no sabe las dimensiones de {self.path}: {ancho}x{alto}")

        self._captura = captura
        self._info = SourceInfo(width=ancho, height=alto, native_fps=fps, is_live=False)

    def frames(self) -> Iterator[Frame]:
        if self._captura is None or self._info is None:
            raise SourceError(f"hay que llamar a open() antes de leer fotogramas ({self.path})")
        return self._generar(self._captura, self._info)

    def _generar(self, captura: Any, info: SourceInfo) -> Iterator[Frame]:
        """Lee todo el fichero y emite solo los fotogramas que toca.

        El descarte se hace **al leer**: se leen los 300 fotogramas del fixture y se emiten
        50 con `target_fps=5`. Encolar en vez de descartar solo consigue, con una fuente en
        directo, que el analisis se retrase cada vez mas respecto a la realidad.
        """
        nativo = info.native_fps
        if nativo is None:  # pragma: no cover - open() ya ha rechazado este caso
            raise SourceError(f"{self.path} no declara frame rate")
        # Ratio de emision, capado a 1: pedir mas fps de los que tiene el fichero no
        # duplica fotogramas, entrega los que hay.
        ratio = min(self.target_fps / nativo, 1.0)

        leidos = 0  # fotogramas de origen consumidos, contando las vueltas del bucle
        emitidos = 0  # indice del `Frame`, que solo avanza cuando se emite
        while True:
            ok, imagen = captura.read()
            if not ok:
                if not self.loop or leidos == 0:
                    return
                captura.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, imagen = captura.read()
                if not ok:
                    return

            # Emite cuando el contador de "cuantos deberian haber salido ya" cruza un
            # entero. Reparte el descarte de forma uniforme incluso cuando target_fps no
            # divide al nativo: con 7 sobre 30 los huecos son de 4 o 5, nunca una rafaga
            # seguida de un silencio.
            if leidos == 0 or int(leidos * ratio) > int((leidos - 1) * ratio):
                yield Frame(
                    image=imagen,
                    index=emitidos,
                    # Se deriva de la posicion en el medio, no del reloj de pared: es lo
                    # que hace que dos ejecuciones den la misma secuencia.
                    timestamp=self._start_time + timedelta(seconds=leidos / nativo),
                )
                emitidos += 1
            leidos += 1

    def close(self) -> None:
        """Libera la captura. Idempotente: llamarlo dos veces, o sin abrir, no lanza."""
        if self._captura is not None:
            self._captura.release()
            self._captura = None
