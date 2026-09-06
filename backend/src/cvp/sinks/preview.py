"""El sink del preview: el ultimo fotograma, en disco, escrito de forma atomica.

## Por que en disco y no en memoria

`_docs/architecture.md` decia que el preview era "un ring buffer en memoria" que lee la
API. No puede funcionar: worker y API son **procesos distintos** —decision deliberada del
plan, y buena: la API no carga modelos y podria vivir en otra maquina— y un buffer en la
memoria del worker es invisible para la API. La decision 2 de #9 lo cambia: el preview
escribe el ultimo fotograma como JPEG en una ruta conocida y la API sirve ese fichero.
Cruza procesos, sobrevive a un reinicio de la API y no necesita memoria compartida ni un
canal entre los dos.

La ruta se deriva del proyecto: `data/preview/<id del proyecto>.jpg`, al lado de la base de
datos y por el mismo motivo que ella —los dos procesos la calculan del `id` del proyecto
sin ponerse de acuerdo—. `ruta_de_preview` es publica justamente para que la API de #12
llame a la misma funcion en vez de escribir la convencion por segunda vez.

## Por que la escritura es atomica

Fichero temporal en el **mismo directorio** y `os.replace`. Sin eso, la API acabaria
sirviendo un JPEG a medio escribir: un fallo que aparece una vez de cada cien, depende de
que el lector caiga entre el `open` y el ultimo `write`, y se diagnostica fatal porque el
fichero, cuando uno va a mirarlo, ya esta entero. En el mismo directorio porque `os.replace`
solo es atomico dentro del mismo sistema de ficheros; con un temporal en `/tmp` seria una
copia, y volveria el mismo agujero.

## Lo que este sink no hace

**No anota.** Los analizadores que dibujan cajas, zonas y lineas son #10 y #11
(`Analyzer.annotate`); hasta entonces se guarda el fotograma tal cual, y lo que se vera en
el dashboard es el video sin pintar. Tampoco mira `output`: eventos y metricas son de los
otros dos sinks.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cvp.core import AnalyzerOutput, Frame, register_sink

__all__ = ["DIRECTORIO_PREVIEW", "PreviewSink", "ruta_de_preview"]

_log = logging.getLogger(__name__)

# Al lado de la base de datos (`sqlite:///data/cvp.sqlite3`) y por el mismo criterio: es
# el directorio de datos de la ejecucion, relativo a donde se lanza el worker.
DIRECTORIO_PREVIEW = Path("data") / "preview"

# 85 es el punto donde el JPEG deja de mejorar de forma visible y sigue bajando de tamano.
CALIDAD_POR_DEFECTO = 85


def ruta_de_preview(project_id: str, *, base: Path | str = DIRECTORIO_PREVIEW) -> Path:
    """El fichero donde vive el ultimo fotograma de un proyecto.

    Es la convencion que comparten el worker (que escribe) y la API (que sirve). Vive en
    una funcion y no en dos cadenas parecidas en dos modulos, que es como se desincronizan.
    """
    if not project_id:
        raise ValueError("un preview necesita el id del proyecto para saber donde escribir")
    return Path(base) / f"{project_id}.jpg"


@register_sink("preview")
class PreviewSink:
    """Guarda el ultimo fotograma como JPEG, con escritura atomica."""

    def __init__(
        self,
        path: Path | str,
        *,
        quality: int = CALIDAD_POR_DEFECTO,
        encoder: Callable[[np.ndarray, int], bytes] | None = None,
    ) -> None:
        if not 1 <= quality <= 100:
            raise ValueError(f"quality es la calidad JPEG, de 1 a 100, no {quality!r}")
        self.path = Path(path)
        self.quality = int(quality)
        self._encoder = encoder if encoder is not None else _codificar_jpeg
        # Crear el directorio es del sink al arrancar (#3): la configuracion no los crea.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ultimo_indice: int | None = None
        self.escrituras = 0

    @classmethod
    def from_config(cls, cfg: Any, *, project_id: str, **_: Any) -> PreviewSink:
        """Construye desde el bloque `sinks` del `project.yaml`.

        La ruta sale del `id` del proyecto, que pasa la fabrica. `cfg.max_frames` describia
        el ring buffer en memoria que la decision 2 de #9 sustituye y aqui no se usa: en
        disco solo existe el ultimo fotograma.
        """
        return cls(ruta_de_preview(project_id))

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        """Guarda `frame` tal cual, sin anotar. `run_id` y `output` se ignoran.

        El runner llama a los sinks **una vez por analizador y por fotograma**, asi que con
        dos analizadores el mismo fotograma llegaria dos veces; se codifica una. La
        comparacion es por `frame.index`, que es el numero de fotograma emitido por la
        fuente y no se reinicia al reconectar (contrato de #2).
        """
        del run_id, output
        if frame.index == self._ultimo_indice:
            return
        self._ultimo_indice = frame.index
        self._escribir_atomico(self._encoder(frame.image, self.quality))
        self.escrituras += 1

    def flush(self) -> None:
        """No hay nada pendiente: cada fotograma ya se escribio entero o no se escribio."""

    def _escribir_atomico(self, datos: bytes) -> None:
        """Temporal en el mismo directorio, y `os.replace`.

        El temporal se borra si algo falla por el camino: escribir miles de fotogramas no
        puede ir dejando restos en el directorio que sirve la API.
        """
        descriptor, temporal = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as fichero:
                fichero.write(datos)
            os.replace(temporal, self.path)
        except BaseException:
            # `BaseException` y no `Exception` a proposito: el segundo Ctrl+C llega como
            # `AbortRequested`, que deriva de `BaseException`, y tampoco puede dejar restos.
            _borrar_si_existe(temporal)
            raise


def _codificar_jpeg(imagen: np.ndarray, calidad: int) -> bytes:
    """El fotograma BGR como JPEG en memoria.

    Se codifica a bytes y luego se escribe, en vez de usar `cv2.imwrite`, porque el fichero
    definitivo tiene que aparecer de una vez: `imwrite` escribiria directamente sobre la
    ruta que la API esta sirviendo.
    """
    ok, buffer = cv2.imencode(".jpg", imagen, [int(cv2.IMWRITE_JPEG_QUALITY), calidad])
    if not ok:
        raise RuntimeError("OpenCV no ha podido codificar el fotograma como JPEG")
    return bytes(buffer.tobytes())


def _borrar_si_existe(ruta: str) -> None:
    try:
        os.unlink(ruta)
    except OSError:  # pragma: no cover - solo si el temporal ya no esta
        _log.debug("no se ha podido borrar el temporal %s", ruta)
