"""El sink que persiste: eventos, metricas y agregados por minuto, por lotes.

## Cuantos datos se pierden si el proceso muere

Agrupar escrituras tiene una consecuencia y quien opere esto tiene derecho a saberla, asi
que va aqui y no en un comentario perdido:

- **Si el proceso muere de golpe** (un `kill -9`, un fallo del kernel, la bateria), se
  pierde **el lote pendiente**: como mucho `batch_size` registros o `max_interval_s`
  segundos de medidas, lo que llegue antes. Con los valores por defecto —200 registros y
  5 s— y el proyecto 01 a 5 fps con una metrica por fotograma, manda el intervalo: se
  pierden como mucho **5 segundos de medidas, unos 25 registros**. Con `batch_size: 1` no
  se pierde nada y se paga una transaccion por medida.
- **Si el proceso termina de forma ordenada** —fin del video, `--max-frames`, Ctrl+C— no se
  pierde nada: el runner llama a `flush()` en su `finally`, tambien en el camino de Ctrl+C
  (#8).
- **Si se va la luz**, ademas del lote pendiente pueden perderse las ultimas transacciones
  ya confirmadas: SQLite corre en WAL con `synchronous=NORMAL` (ver `cvp.storage.engine`).
  Un fallo del proceso, en cambio, no toca lo ya confirmado.
- **Un fallo al escribir no tumba el pipeline** (#8 lo garantiza), pero el lote se
  **descarta** en vez de reintentarse indefinidamente: si el disco esta lleno o la base
  bloqueada, acumular en memoria hasta reventar el worker seria cambiar perder unos
  segundos de datos por perder la ejecucion entera. Se registra en el log con su tamano.

El intervalo se mira **cuando llega una escritura**, no en un hilo aparte: sin hilo no hay
carrera que probar, y un worker sin fotogramas que escribir no tiene nada pendiente que
volcar. Lo que quede al final lo saca el `flush()` del cierre.

## Lo que este sink no hace

No mira el fotograma. Lo recibe porque el contrato de `Sink.write` lo pasa a todos los
sinks —la decision 1 de #9, para que el `preview` no necesite un caso especial en el
runner— y aqui se ignora a proposito.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter
from typing import Any

from sqlmodel import Session

from cvp.core import AnalyzerOutput, Event, Frame, Metric, register_sink
from cvp.storage import (
    URL_POR_DEFECTO,
    AgregadorPorMinuto,
    crear_engine,
    crear_esquema,
    escribir_lote,
)

__all__ = ["SqliteSink"]

_log = logging.getLogger(__name__)

# Los mismos valores por defecto que el esquema de `cvp.pipeline.config`, para que
# construir el sink a mano y construirlo desde el YAML den lo mismo.
LOTE_POR_DEFECTO = 200
INTERVALO_POR_DEFECTO_S = 5.0


@register_sink("sqlite")
class SqliteSink:
    """Escribe en la base de datos por lotes. Vale igual para SQLite y para Postgres.

    El nombre del registro es `sqlite` porque es lo que dice el YAML desde #3 y es la base
    del entorno local; lo que decide contra que motor se escribe es la `url`, y un
    `postgresql://...` funciona sin tocar nada mas (#15).
    """

    def __init__(
        self,
        url: str = URL_POR_DEFECTO,
        *,
        project_id: str,
        project_name: str | None = None,
        batch_size: int = LOTE_POR_DEFECTO,
        max_interval_s: float = INTERVALO_POR_DEFECTO_S,
        engine: Any | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size es un numero de registros >= 1, no {batch_size!r}")
        if max_interval_s <= 0:
            raise ValueError(f"max_interval_s son segundos > 0, no {max_interval_s!r}")
        self.project_id = project_id
        self.project_name = project_name if project_name is not None else project_id
        self.batch_size = int(batch_size)
        self.max_interval_s = float(max_interval_s)
        # Cronometro monotono, no reloj de pared: la ventana de escritura no puede
        # descuadrarse porque NTP eche el reloj atras a mitad de una ejecucion de dias.
        self._timer = timer if timer is not None else perf_counter
        # Crear el engine abre el fichero y crea `data/` si falta: es "del sink al
        # arrancar", que es lo que la configuracion se niega a hacer (#3). Y falla aqui, al
        # construir el pipeline, en vez de en el minuto 40 con el primer lote.
        self.engine = engine if engine is not None else crear_engine(url)
        crear_esquema(self.engine)
        self._run_id: str | None = None
        self._eventos: list[Event] = []
        self._metricas: list[Metric] = []
        # El estado entre lotes de la agregacion por minuto: la ultima muestra de cada
        # serie, cuyo tramo de tiempo no se conoce hasta que llega la siguiente. Vive en el
        # sink porque el sink es quien ve todos los lotes de una ejecucion.
        self._agregador = AgregadorPorMinuto()
        self._ultimo_volcado = self._timer()

    @classmethod
    def from_config(cls, cfg: Any, *, project_id: str, project_name: str, **_: Any) -> SqliteSink:
        """Construye desde el bloque `sinks` del `project.yaml`.

        `project_id` y `project_name` no salen del bloque `sinks` sino del proyecto, y los
        pasa la fabrica: un sink tiene que saber a que proyecto pertenece lo que escribe, y
        escribirlo otra vez debajo de `sinks` seria una forma segura de que los dos acabaran
        discrepando. Es la misma regla por la que el tracker recibe su `frame_rate`.
        """
        return cls(
            cfg.url,
            project_id=project_id,
            project_name=project_name,
            batch_size=getattr(cfg, "batch_size", LOTE_POR_DEFECTO),
            max_interval_s=getattr(cfg, "max_interval_s", INTERVALO_POR_DEFECTO_S),
        )

    @property
    def pendientes(self) -> int:
        """Cuantos registros hay sin escribir. Es lo que se perderia ahora mismo."""
        return len(self._eventos) + len(self._metricas)

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        """Acumula la salida del analizador y vuelca cuando toca. `frame` se ignora."""
        del frame  # el contrato lo pasa por el `preview`; aqui no pinta nada
        if self._run_id is not None and run_id != self._run_id and self.pendientes:
            # Cambiar de ejecucion con cosas pendientes no deberia pasar (un runner, un
            # run_id), pero si pasara, mezclarlas escribiria filas con el run_id que no es.
            self.flush()
        self._run_id = run_id
        self._eventos.extend(output.events)
        self._metricas.extend(output.metrics)
        if self._toca_volcar():
            self.flush()

    def flush(self) -> None:
        """Escribe el lote pendiente en una transaccion. Lo llama el runner al terminar.

        La transaccion abarca el lote entero: o entran las filas crudas **y** sus agregados
        del minuto, o no entra nada. Un fallo a mitad no puede dejar un minuto contando
        muestras que no estan en la tabla.
        """
        self._ultimo_volcado = self._timer()
        if self._run_id is None or not self.pendientes:
            self._eventos.clear()
            self._metricas.clear()
            return
        eventos, metricas = self._eventos, self._metricas
        # Se vacia **antes** de escribir: si la escritura falla, el lote se descarta en vez
        # de crecer sin limite hasta llevarse por delante al worker.
        self._eventos, self._metricas = [], []
        # Los agregados se calculan fuera de la transaccion y el agregador avanza aunque la
        # escritura falle: un lote descartado se lleva sus muestras y tambien el tramo de
        # tiempo que cerraba. Reintentar no esta sobre la mesa (ver arriba), y dejar el
        # agregador atras haria que el lote siguiente cerrara un tramo sobre un hueco.
        agregados = self._agregador.agregar(metricas)
        try:
            with Session(self.engine) as session:
                escribir_lote(
                    session,
                    run_id=self._run_id,
                    project_id=self.project_id,
                    project_name=self.project_name,
                    eventos=eventos,
                    metricas=metricas,
                    agregados=agregados,
                )
                session.commit()
        except Exception as exc:
            # Sin traceback: el que llama —el runner— ya lo escribe. Lo que anade esta
            # linea es **cuanto se ha perdido**, que es el dato que no esta en el suyo.
            _log.error(
                "se descarta un lote de %d evento(s) y %d metrica(s) de %s: %s",
                len(eventos),
                len(metricas),
                self._run_id,
                exc,
            )
            raise

    def _toca_volcar(self) -> bool:
        if self.pendientes >= self.batch_size:
            return True
        return (self._timer() - self._ultimo_volcado) >= self.max_interval_s
