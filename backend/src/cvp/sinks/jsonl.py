"""El sink de depuracion: una linea JSON por evento y por metrica.

Es el sink que se lee con `tail -f` mientras se afina un analizador, y el que permite
comparar dos ejecuciones con `diff` sin abrir la base de datos.

El contenido de cada linea es **la forma canonica de #19** (`event_to_dict` /
`metric_to_dict`) mas dos campos de sobre: `run_id`, que dice de que ejecucion es, y `type`
(`event` o `metric`), que dice como leerla. El sobre va fuera y no mezclado con el objeto a
proposito: quitando esas dos claves, lo que queda es exactamente lo que `event_from_dict` y
`metric_from_dict` esperan, ni un campo mas ni uno menos —los dos rechazan campos
desconocidos—, asi que el fichero se lee de vuelta sin traducir nada.

El fichero se abre en modo **append** en cada volcado y se cierra: el `Protocol` de un sink
no tiene `close()`, y un descriptor abierto durante dias es lo que impide rotar el fichero
o borrarlo sin reiniciar el worker. Abrir cuesta microsegundos y esto es un sink de
depuracion, no el camino caliente.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cvp.core import AnalyzerOutput, Frame, event_to_dict, metric_to_dict, register_sink

__all__ = ["JsonlSink"]


@register_sink("jsonl")
class JsonlSink:
    """Vuelca eventos y metricas a un fichero de lineas JSON."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # El directorio lo exige la configuracion al cargar (#3), pero construir el sink a
        # mano en un test no pasa por ahi, y crear el padre es del sink al arrancar.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lineas: list[str] = []

    @classmethod
    def from_config(cls, cfg: Any, **_: Any) -> JsonlSink:
        """Construye desde el bloque `sinks` del `project.yaml`.

        Acepta y descarta los extras que la fabrica pasa a todos los sinks (`project_id`,
        `project_name`): a un volcado de depuracion el proyecto no le anade nada, porque el
        fichero ya es de un proyecto por la ruta a la que apunta.
        """
        return cls(cfg.path)

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        """Escribe una linea por evento y una por metrica. `frame` se ignora.

        Escribe ya, sin agrupar: la razon de ser de este sink es ver lo que esta pasando
        mientras pasa, y un lote de 200 lineas lo haria inutil para eso.
        """
        del frame  # el contrato lo pasa por el `preview`; aqui no pinta nada
        for evento in output.events:
            self._lineas.append(self._linea("event", run_id, event_to_dict(evento)))
        for metrica in output.metrics:
            self._lineas.append(self._linea("metric", run_id, metric_to_dict(metrica)))
        self.flush()

    def flush(self) -> None:
        """Vuelca las lineas pendientes y cierra el fichero."""
        if not self._lineas:
            return
        lineas, self._lineas = self._lineas, []
        with self.path.open("a", encoding="utf-8") as fichero:
            fichero.writelines(lineas)

    @staticmethod
    def _linea(tipo: str, run_id: str, datos: dict[str, Any]) -> str:
        # `ensure_ascii=False` para que un nombre con tilde se lea tal cual en el fichero, y
        # `sort_keys` para que dos ejecuciones iguales den ficheros iguales byte a byte.
        return (
            json.dumps(
                {"type": tipo, "run_id": run_id, **datos}, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        )
