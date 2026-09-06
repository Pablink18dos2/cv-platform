"""El bucle del worker: fuente -> detector -> tracker -> analizadores -> sinks.

Es el punto de integracion, y sus fallos caracteristicos son de los silenciosos. Por eso
las cuatro decisiones que manda este modulo estan escritas aqui arriba:

1. **`source.close()` va en un `finally`.** Los cuatro caminos de salida —fin del video,
   `--max-frames`, Ctrl+C y excepcion— pasan por el mismo sitio. El connector en directo
   levanta un hilo lector, y abandonar el generador sin cerrar la fuente lo deja leyendo
   del stream para siempre: es `daemon`, asi que no impide terminar, pero mientras el
   proceso viva sigue gastando ancho de banda y CPU para nada.
2. **La cadencia se cuenta sobre `frame.index`**, que es el numero de fotograma **emitido
   por la fuente** y no se reinicia al reconectar (contrato de #2). Un analizador con
   `every_n_frames: 10` corre en los indices 0, 10, 20...: sobre 30 fotogramas son 3
   ejecuciones, no 2 ni 4. Contar con un contador propio del runner daria lo mismo hasta
   la primera caida del directo, y despues no.
3. **El ROI se traduce una sola vez, despues de `source.open()`**, cuando `SourceInfo` ya
   dice cuanto mide el fotograma. El YAML habla en fracciones (decision de #3) y el core
   en pixeles (decision de #2); esta es la unica costura entre los dos, y el mismo `BBox`
   se pasa en cada llamada a `detect()`, porque el ROI es argumento por llamada y no
   estado del detector.
4. **Nada de `datetime.now()` ni de `time.sleep` en la logica.** El reloj de pared
   (`clock`), el cronometro monotono (`timer`) y la espera (`sleeper`) se inyectan, igual
   que en `cvp/sources/live.py`. Es lo que permite probar el paceo, el backoff de arranque
   y las latencias sin que la suite duerma ni un milisegundo.
"""

from __future__ import annotations

import logging
import signal
import statistics
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event
from time import perf_counter
from time import sleep as _dormir
from typing import Any

from cvp.core import BBox
from cvp.pipeline.config import ProjectConfig, Reconnect, Region
from cvp.pipeline.errors import AbortRequested, PipelineError
from cvp.pipeline.factory import build_pipeline
from cvp.storage.run_id import nuevo_run_id

__all__ = [
    "AbortRequested",
    "BlockStats",
    "PipelineRunner",
    "RunStats",
    "manejar_sigint",
    "run_project",
]

_log = logging.getLogger(__name__)

# Cada cuanto, por defecto, se escribe la linea de salud en el log.
SALUD_CADA_S = 30.0

# Cuantas latencias se guardan por bloque para calcular percentiles. A 5 fps son mas de
# quince minutos de historia; el limite existe porque un worker corre durante dias y una
# lista sin tope es una fuga de memoria lenta con forma de metrica.
MUESTRAS_DE_LATENCIA = 5000


def _ahora_utc() -> datetime:
    """El reloj de pared de verdad. Es el valor por defecto y no lo usa ningun test."""
    return datetime.now(UTC)


def _percentil(muestras: Sequence[float], q: float) -> float:
    """El percentil `q` (0-1) por el metodo del vecino mas cercano.

    Sin interpolar y sin dependencias: con cuatro muestras interpolar solo inventa
    precision, y lo que se quiere saber es si el p95 triplica a la mediana.
    """
    if not muestras:
        return 0.0
    ordenadas = sorted(muestras)
    indice = min(len(ordenadas) - 1, max(0, round(q * (len(ordenadas) - 1))))
    return ordenadas[indice]


@dataclass(slots=True)
class BlockStats:
    """Cuanto tarda un bloque. En milisegundos, porque es como se lee en el log."""

    name: str
    calls: int = 0
    total_ms: float = 0.0
    _muestras: deque[float] = field(default_factory=lambda: deque(maxlen=MUESTRAS_DE_LATENCIA))

    def observe(self, ms: float) -> None:
        self.calls += 1
        self.total_ms += ms
        self._muestras.append(ms)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    @property
    def p50_ms(self) -> float:
        return statistics.median(self._muestras) if self._muestras else 0.0

    @property
    def p95_ms(self) -> float:
        return _percentil(list(self._muestras), 0.95)

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "name": self.name,
            "calls": self.calls,
            "p50_ms": round(self.p50_ms, 1),
            "p95_ms": round(self.p95_ms, 1),
            "mean_ms": round(self.mean_ms, 1),
        }

    def __str__(self) -> str:
        return f"{self.name} p50 {self.p50_ms:.1f} ms p95 {self.p95_ms:.1f} ms ({self.calls})"


@dataclass(slots=True)
class RunStats:
    """Los contadores de una ejecucion, como objeto y no solo como texto.

    Que sean un objeto es un requisito, no un lujo: #12 los tiene que servir por la API, y
    una API que tuviera que parsear una linea de log seria una API rota desde el dia uno.
    """

    run_id: str
    started_at: datetime
    frames: int = 0
    elapsed_s: float = 0.0
    # Lo que se tarda en abrir la fuente y calentar el modelo, antes del primer fotograma.
    # Va aparte porque no es rendimiento del encadenado: resolver la URL con yt-dlp y
    # cargar los pesos cuesta segundos **una vez**, y sumarlo a los fps de salida solo
    # sirve para que una ejecucion corta parezca mucho mas lenta de lo que es.
    startup_s: float = 0.0
    ended_at: datetime | None = None
    startup_retries: int = 0
    sink_writes: int = 0
    sink_errors: int = 0
    analyzer_calls: dict[str, int] = field(default_factory=dict)
    # La espera hasta que la fuente entrega el siguiente fotograma. No es un bloque que
    # trabaje, pero es donde se va el tiempo cuando el cuello de botella no es el modelo, y
    # sin medirla el desglose culpa al detector de lo que no es suyo.
    source: BlockStats = field(default_factory=lambda: BlockStats("fuente"))
    detector: BlockStats = field(default_factory=lambda: BlockStats("detector"))
    tracker: BlockStats = field(default_factory=lambda: BlockStats("tracker"))
    interrupted: bool = False

    @property
    def processing_s(self) -> float:
        """Segundos de bucle: el total menos el arranque."""
        return max(self.elapsed_s - self.startup_s, 0.0)

    @property
    def fps(self) -> float:
        """Fps **reales de salida**: fotogramas por segundo de bucle, con el reloj de pared.

        Es el numero que decide si el presupuesto del plan se sostiene, asi que se mide
        sobre el bucle y no sobre el proceso entero.
        """
        return self.frames / self.processing_s if self.processing_s > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Los contadores en tipos serializables, que es lo que necesitara la API."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "frames": self.frames,
            "elapsed_s": round(self.elapsed_s, 2),
            "startup_s": round(self.startup_s, 2),
            "processing_s": round(self.processing_s, 2),
            "fps": round(self.fps, 2),
            "startup_retries": self.startup_retries,
            "sink_writes": self.sink_writes,
            "sink_errors": self.sink_errors,
            "analyzer_calls": dict(self.analyzer_calls),
            "source": self.source.as_dict(),
            "detector": self.detector.as_dict(),
            "tracker": self.tracker.as_dict(),
            "interrupted": self.interrupted,
        }

    def health_line(self) -> str:
        """La linea periodica. Lleva el desglose por bloque a proposito: sin el no se sabe
        a quien culpar cuando los fps bajen."""
        return (
            f"salud {self.run_id}: {self.frames} fotogramas en {self.processing_s:.0f} s "
            f"= {self.fps:.2f} fps | {self.source} | {self.detector} | {self.tracker} | "
            f"sinks {self.sink_writes} escrituras, {self.sink_errors} errores"
        )

    def summary(self) -> str:
        """El resumen del final, con los mismos numeros y la duracion total."""
        analizadores = (
            ", ".join(f"{nombre} x{veces}" for nombre, veces in sorted(self.analyzer_calls.items()))
            or "ninguno"
        )
        return "\n".join(
            [
                f"ejecucion {self.run_id}"
                + (" (interrumpida con Ctrl+C)" if self.interrupted else ""),
                f"  duracion total:  {self.elapsed_s:.1f} s "
                f"(arranque {self.startup_s:.1f} s + bucle {self.processing_s:.1f} s)",
                f"  fotogramas:      {self.frames} ({self.fps:.2f} fps de salida)",
                f"  espera fuente:   p50 {self.source.p50_ms:.1f} ms, "
                f"p95 {self.source.p95_ms:.1f} ms, media {self.source.mean_ms:.1f} ms "
                f"({self.source.calls} fotogramas)",
                f"  detector:        p50 {self.detector.p50_ms:.1f} ms, "
                f"p95 {self.detector.p95_ms:.1f} ms, media {self.detector.mean_ms:.1f} ms "
                f"({self.detector.calls} llamadas)",
                f"  tracker:         p50 {self.tracker.p50_ms:.1f} ms, "
                f"p95 {self.tracker.p95_ms:.1f} ms, media {self.tracker.mean_ms:.1f} ms "
                f"({self.tracker.calls} llamadas)",
                f"  analizadores:    {analizadores}",
                f"  sinks:           {self.sink_writes} escrituras, {self.sink_errors} errores",
                f"  reintentos de arranque: {self.startup_retries}",
            ]
        )


class PipelineRunner:
    """Ata los bloques y corre el bucle. No sabe de negocio: si aparece aqui un `if
    project_id == ...` o un `if tipo == "yolo"`, el diseno esta roto.

    Toma bloques ya construidos y no un `ProjectConfig`, igual que los adaptadores toman
    argumentos planos: asi el bucle se prueba entero con dobles, sin YAML, sin modelo y sin
    red. `PipelineRunner.from_config` es el atajo que usa el CLI.
    """

    def __init__(
        self,
        *,
        source: Any,
        detector: Any,
        tracker: Any,
        analyzers: Sequence[Any] = (),
        sinks: Sequence[Any] = (),
        roi: Region | None = None,
        reconnect: Reconnect | None = None,
        run_id: str | None = None,
        health_every_s: float = SALUD_CADA_S,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
        sleeper: Callable[[float], object] | None = None,
    ) -> None:
        self._source = source
        self._detector = detector
        self._tracker = tracker
        self._analyzers = tuple(analyzers)
        self._sinks = tuple(sinks)
        self._roi = roi
        self._reconnect = reconnect
        self._health_every_s = float(health_every_s)
        self._clock = clock if clock is not None else _ahora_utc
        # Cronometro **monotono** y separado del reloj de pared: la duracion y la cadencia
        # de los informes no pueden depender de que NTP eche el reloj atras a mitad de una
        # ejecucion de dias.
        self._timer = timer if timer is not None else perf_counter
        self._sleeper = sleeper if sleeper is not None else _dormir
        self._parada = Event()

        for analizador in self._analyzers:
            cadencia = getattr(analizador, "every_n_frames", 1)
            if not isinstance(cadencia, int) or cadencia < 1:
                raise ValueError(
                    f"el analizador {getattr(analizador, 'name', analizador)!r} tiene "
                    f"every_n_frames={cadencia!r}, y una cadencia es un entero >= 1"
                )

        self.stats = RunStats(
            run_id=run_id if run_id is not None else self._nuevo_run_id(),
            started_at=self._clock(),
            analyzer_calls={a.name: 0 for a in self._analyzers},
        )

    @classmethod
    def from_config(cls, config: ProjectConfig, **kwargs: Any) -> PipelineRunner:
        """Construye los bloques del proyecto y devuelve el runner listo para `run()`.

        `registries` y `load` se le pasan a `build_pipeline` si vienen; el resto de
        `kwargs` son los del runner (`health_every_s`, `clock`, `timer`, `sleeper`...).
        """
        de_fabrica = {
            clave: kwargs.pop(clave) for clave in ("registries", "load") if clave in kwargs
        }
        bloques = build_pipeline(config, **de_fabrica)
        return cls(
            source=bloques.source,
            detector=bloques.detector,
            tracker=bloques.tracker,
            analyzers=bloques.analyzers,
            sinks=bloques.sinks,
            # El ROI llega en fracciones y se traduce dentro de `run()`, cuando la fuente
            # ya ha dicho cuanto mide el fotograma.
            roi=getattr(config.detector, "roi", None),
            # Solo las fuentes en directo traen politica de reconexion; un fichero no la
            # necesita y no la tiene.
            reconnect=getattr(config.source, "reconnect", None),
            **kwargs,
        )

    # --- El bucle ---------------------------------------------------------------------

    def run(self, *, max_frames: int | None = None, duration_s: float | None = None) -> RunStats:
        """Corre el pipeline hasta que se acabe la fuente, se cumpla un limite o lo paren.

        Sin `max_frames` ni `duration_s` no termina solo con una fuente en directo, que es
        justamente su razon de ser; los dos limites son lo que lo hace demostrable y
        testeable.

        Devuelve los contadores. De aqui solo se escapan dos cosas: un fallo de verdad (la
        fuente no abre, un bloque revienta) y el `AbortRequested` de un **segundo** Ctrl+C,
        que no se captura a proposito. El primer Ctrl+C apaga limpio y vuelve con
        normalidad.
        """
        if max_frames is not None and max_frames < 1:
            raise ValueError(f"max_frames es un numero de fotogramas >= 1, no {max_frames!r}")
        if duration_s is not None and duration_s <= 0:
            raise ValueError(f"duration_s son segundos > 0, no {duration_s!r}")

        inicio = self._timer()
        proximo_informe = self._health_every_s
        self.stats.started_at = self._clock()
        try:
            self._abrir_con_reintentos()
            roi = self._roi_en_pixeles()
            self._detector.warmup()
            # Abrir el directo y calentar el modelo cuesta segundos y se paga una vez; a
            # partir de aqui empieza lo que de verdad se esta cronometrando.
            self.stats.startup_s = self._timer() - inicio
            proximo_informe = self.stats.startup_s + self._health_every_s
            _log.info(
                "arranca %s en %.1f s: %d analizador(es), %d sink(s)%s",
                self.stats.run_id,
                self.stats.startup_s,
                len(self._analyzers),
                len(self._sinks),
                "" if roi is None else f", roi {roi}",
            )
            # El bucle se escribe con `next()` en vez de con un `for` para poder cronometrar
            # **la espera**: con una fuente en directo, el tiempo entre dos fotogramas no es
            # trabajo del pipeline, es lo que tarda la fuente en tener uno nuevo. Sin este
            # numero, un directo que no da abasto parece un detector lento.
            fotogramas = iter(self._source.frames())
            while True:
                pedido = self._timer()
                try:
                    frame = next(fotogramas)
                except StopIteration:
                    break
                self.stats.source.observe((self._timer() - pedido) * 1000.0)
                if self._parada.is_set():
                    break
                self._procesar(frame, roi)
                self.stats.elapsed_s = self._timer() - inicio
                if max_frames is not None and self.stats.frames >= max_frames:
                    _log.info("alcanzado --max-frames=%d", max_frames)
                    break
                if duration_s is not None and self.stats.elapsed_s >= duration_s:
                    _log.info("alcanzado --duration=%.1f s", duration_s)
                    break
                if self.stats.elapsed_s >= proximo_informe:
                    _log.info("%s", self.stats.health_line())
                    proximo_informe = self.stats.elapsed_s + self._health_every_s
                if self._parada.is_set():
                    break
        except KeyboardInterrupt:
            # Un Ctrl+C que llega **sin** el manejador de `manejar_sigint` puesto: lo
            # levanta el manejador por defecto de Python donde pille, normalmente dentro de
            # la inferencia. No es un fallo del programa y un traceback solo taparia el
            # resumen, asi que se apaga limpio y se sale con 0.
            #
            # Este `except` es la razon de que el aborto **no** sea un `KeyboardInterrupt`:
            # aqui dentro no hay forma de distinguir "primer Ctrl+C" de "segundo", y
            # cualquier aborto que pasara por esta puerta se convertiria en otro apagado
            # ordenado. Ver `request_stop`.
            self._parada.set()
            self.stats.interrupted = True
            _log.info("Ctrl+C: cerrando la fuente y haciendo flush de los sinks")
        finally:
            self.stats.elapsed_s = self._timer() - inicio
            self.stats.ended_at = self._clock()
            # Pase lo que pase: fin normal, limite alcanzado, Ctrl+C, aborto o excepcion.
            # En el aborto tambien se intenta, porque cerrar suele costar milisegundos y
            # perder las escrituras por las prisas seria peor; si es el propio cierre el que
            # se cuelga, el tercer Ctrl+C ya no pasa por nuestro manejador y mata el proceso.
            self._cerrar_fuente()
            self._flush_sinks()
        return self.stats

    def request_stop(self) -> None:
        """Pide la parada. **La segunda vez aborta**, y con `AbortRequested`, no con
        `KeyboardInterrupt`.

        Es lo que hace el manejador de `SIGINT`. El primero termina el fotograma en curso y
        apaga ordenadamente; el segundo lanza, desde el propio manejador, una excepcion que
        no captura nadie por el camino.

        **Por que no un `KeyboardInterrupt`, que es lo que parece natural:** porque `run()`
        captura `KeyboardInterrupt` a proposito, para convertir el primer Ctrl+C en un
        apagado limpio con codigo 0. Un aborto que fuese `KeyboardInterrupt` se lo tragaria
        ese mismo `except` siempre que el bucle estuviera dentro del `try` —o sea, casi
        siempre: el proceso se pasa el 71 % del tiempo esperando fotograma— y el usuario
        recibiria otro apagado ordenado y un 0. Solo abortaria de verdad si la senal cayera
        dentro del `finally`, que es el caso raro. Fue el fallo de la primera version de
        este runner, y `AbortRequested` deriva de `BaseException` justamente para que no
        pueda repetirse: ni el `except KeyboardInterrupt` del apagado ordenado ni ninguno de
        los `except Exception` que protegen un sink roto o el cierre de la fuente pueden
        detenerla.

        Y se lanza desde el manejador en vez de apuntarse en una bandera porque, si lo que
        se ha colgado es el cierre de la fuente, no hay ningun sitio del bucle donde mirarla.
        """
        if self._parada.is_set():
            raise AbortRequested(
                "segundo Ctrl+C: se aborta sin esperar a que termine el apagado limpio"
            )
        self._parada.set()
        # Que la ejecucion la pararon desde fuera es un dato de la ejecucion, no un detalle
        # del apagado: #12 lo servira por la API, y un worker que alguien detiene a mano a
        # las tres de la manana no puede figurar como una ejecucion que termino sola. Se
        # marca aqui, y no solo en el `except KeyboardInterrupt` de `run()`, porque el
        # camino real del CLI —senal, manejador, `request_stop`— no pasa por ese `except`.
        self.stats.interrupted = True
        _log.info("Ctrl+C: se termina el fotograma en curso (otro Ctrl+C aborta ya)")

    # --- Las piezas del bucle ---------------------------------------------------------

    def _procesar(self, frame: Any, roi: BBox | None) -> None:
        """Un fotograma: detectar, seguir, analizar a su cadencia y escribir."""
        t0 = self._timer()
        detecciones = self._detector.detect(frame, roi)
        t1 = self._timer()
        tracks = self._tracker.update(frame, detecciones)
        t2 = self._timer()
        self.stats.detector.observe((t1 - t0) * 1000.0)
        self.stats.tracker.observe((t2 - t1) * 1000.0)
        self.stats.frames += 1

        for analizador in self._analyzers:
            # `frame.index` es el fotograma **emitido por la fuente**, que empieza en 0 y
            # no se reinicia al reconectar: con cadencia 10 sobre 30 fotogramas salen 3
            # ejecuciones (indices 0, 10 y 20).
            if frame.index % analizador.every_n_frames:
                continue
            salida = analizador.process(frame, tracks)
            self.stats.analyzer_calls[analizador.name] = (
                self.stats.analyzer_calls.get(analizador.name, 0) + 1
            )
            self._escribir(frame, salida)

    def _escribir(self, frame: Any, salida: Any) -> None:
        """La salida de un analizador va a **todos** los sinks, y uno roto no tumba nada.

        Perder una escritura es mucho menos malo que perder el worker: si el disco se llena
        o SQLite se bloquea, lo que hay que conservar es el proceso que sigue midiendo.

        El fotograma viaja junto a la salida desde #9: el sink `preview` tiene que guardarlo
        y en `AnalyzerOutput` no hay ninguno. La alternativa era un `if tipo == "preview"`
        aqui dentro, que es justo lo que el registro por nombre existe para evitar.
        """
        for sink in self._sinks:
            try:
                sink.write(self.stats.run_id, frame, salida)
            except Exception:
                self.stats.sink_errors += 1
                _log.exception(
                    "el sink %s ha fallado al escribir; el pipeline sigue",
                    type(sink).__name__,
                )
            else:
                self.stats.sink_writes += 1

    def _roi_en_pixeles(self) -> BBox | None:
        """Traduce el ROI de fracciones a pixeles **una sola vez**, ya con la resolucion.

        Se llama despues de `open()` a proposito: antes, `SourceInfo` no existe. El `BBox`
        que sale de aqui es el mismo objeto en todas las llamadas a `detect()`.
        """
        if self._roi is None:
            return None
        info = self._source.info
        roi = self._roi.to_pixels(info.width, info.height)
        _log.info(
            "roi %s del YAML -> %s en pixeles sobre un fotograma de %dx%d",
            (self._roi.x1, self._roi.y1, self._roi.x2, self._roi.y2),
            roi,
            info.width,
            info.height,
        )
        return roi

    # --- Arranque y apagado -----------------------------------------------------------

    def _abrir_con_reintentos(self) -> None:
        """Abre la fuente reintentando con la politica `reconnect` de la configuracion.

        #5 dejo `open()` sin reintentos a proposito —al arrancar todavia no hay datos que
        proteger— y dejo la decision de politica aqui. **La decision es que el runner si
        reintenta**: un worker que arranca a las tres de la manana y se encuentra un
        parpadeo de YouTube tiene que sobrevivirlo, no dejar la noche sin datos. Con
        `max_retries: 0` no reintenta, y con `null` reintenta indefinidamente, que es lo
        que ya significan esos valores en el YAML.
        """
        intento = 0
        maximo = self._reconnect.max_retries if self._reconnect else 0
        esperas = list(self._reconnect.backoff_s) if self._reconnect else []
        while True:
            try:
                self._source.open()
                if intento:
                    _log.info("la fuente ha abierto al intento %d", intento + 1)
                return
            except Exception as exc:
                if self._reconnect is None or (maximo is not None and intento >= maximo):
                    raise PipelineError(
                        f"no se ha podido abrir la fuente tras {intento + 1} intento(s): {exc}"
                    ) from exc
                intento += 1
                self.stats.startup_retries = intento
                espera = _espera_de_backoff(esperas, intento)
                _log.warning(
                    "la fuente no ha abierto (%s); reintento %d/%s en %.1f s",
                    exc,
                    intento,
                    "sin limite" if maximo is None else maximo,
                    espera,
                )
                self._sleeper(espera)
                if self._parada.is_set():
                    raise PipelineError(
                        f"parada solicitada mientras se reintentaba abrir la fuente: {exc}"
                    ) from exc

    def _cerrar_fuente(self) -> None:
        """Cierra la fuente sin dejar que su fallo tape el error que nos trajo al `finally`."""
        try:
            self._source.close()
        except Exception:
            _log.exception("la fuente ha fallado al cerrarse")

    def _flush_sinks(self) -> None:
        """Flush a todos, tambien en el camino de Ctrl+C, y uno roto no impide a los demas."""
        for sink in self._sinks:
            try:
                sink.flush()
            except Exception:
                self.stats.sink_errors += 1
                _log.exception("el sink %s ha fallado al hacer flush", type(sink).__name__)

    def _nuevo_run_id(self) -> str:
        """Identificador de esta ejecucion: legible por delante y unico por detras.

        El formato lo fija `cvp.storage.run_id` desde #9, que es cuando dejo de ser una
        cadena opaca del runner para ser la clave primaria de la tabla `run` y el orden de
        las ejecuciones en la API. Aqui solo se le pasa el reloj inyectado.
        """
        return nuevo_run_id(self._clock())


def _espera_de_backoff(backoff_s: Sequence[float], intento: int) -> float:
    """La espera del reintento `intento` (1-based), repitiendo el ultimo valor.

    Misma semantica que `cvp.sources.live.espera_de_backoff`, y duplicada por el mismo
    motivo por el que `cvp.sources.youtube_live` duplica los `extractor_args`: importar
    `cvp.sources` arrastra OpenCV, y este modulo tiene que poder probarse sin el. Un test
    marcado `slow` comprueba que las dos funciones siguen dando lo mismo.
    """
    if not backoff_s:
        return 0.0
    return float(backoff_s[min(intento, len(backoff_s)) - 1])


@contextmanager
def manejar_sigint(runner: PipelineRunner) -> Iterator[None]:
    """Conecta Ctrl+C con `runner.request_stop()` mientras dure el bloque.

    La escalera tiene tres escalones, y cada uno existe porque el anterior puede quedarse
    corto:

    1. **Primer Ctrl+C**: se termina el fotograma en curso, se cierra la fuente, se hace
       flush y se sale con 0.
    2. **Segundo**: `AbortRequested` desde este manejador. Deja de procesar ahora mismo y
       sale con 130. Todavia intenta cerrar y hacer flush, porque normalmente eso tarda
       milisegundos y perder las escrituras por las prisas seria peor.
    3. **Tercero**: ya no pasa por aqui. Al abortar se devuelve `SIGINT` al manejador por
       defecto, que mata el proceso. Es la salida si hasta el cierre de la fuente se
       cuelga, y es lo que hace que la promesa "se puede abortar" no dependa de que nuestro
       propio codigo de apagado funcione.

    El manejador se instala aqui y no dentro de `run()` porque `signal.signal` solo funciona
    en el hilo principal: un runner usado desde otro hilo (o desde un test) tiene que seguir
    funcionando, y para eso basta con llamar a `request_stop()`.
    """

    def _manejar(_signum: int, _frame: object) -> None:
        try:
            runner.request_stop()
        except AbortRequested:
            # El tercer escalon. Se restaura aqui y no en `request_stop` porque
            # `signal.signal` solo se puede llamar desde el hilo principal, y
            # `request_stop` tiene que poder llamarse desde cualquiera.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise

    anterior = signal.signal(signal.SIGINT, _manejar)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, anterior)


def run_project(
    config: ProjectConfig,
    *,
    max_frames: int | None = None,
    duration_s: float | None = None,
    **kwargs: Any,
) -> RunStats:
    """Construye el pipeline de `config`, lo corre atendiendo a Ctrl+C y devuelve los datos."""
    runner = PipelineRunner.from_config(config, **kwargs)
    with manejar_sigint(runner):
        return runner.run(max_frames=max_frames, duration_s=duration_s)
