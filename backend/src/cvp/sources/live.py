"""Lo que comparten todas las fuentes **en directo**: un hilo lector y el ultimo fotograma.

El connector de fichero (#4) puede muestrear contando fotogramas de origen porque el
fichero espera. Un directo no espera. Los numeros de `_docs/rendimiento.md`, medidos en
esta maquina: por cada segundo de directo llegan 30 fotogramas, decodificarlos cuesta unos
150 ms y, si de esos 30 se analizan 5 a 180 ms cada uno, la inferencia se lleva 900 ms.
Son 1050 ms de trabajo por cada 1000 ms de video. Un 5 % de deriva que no para de crecer:
a las tres horas el analisis va nueve minutos por detras de la realidad y **nada en el
sistema lo dice**, porque el proceso sigue vivo y las metricas se siguen escribiendo.

La salida es que quien lee no sea quien analiza:

- Un **hilo lector** consume el stream a su ritmo y guarda solo el ultimo fotograma
  (`LatestFrameSlot`), pisando el anterior. Los intermedios se pierden a proposito.
- El **consumidor** toma el que haya en ese momento, paceado por `target_fps` contra el
  reloj de pared. Si va lento, se salta fotogramas; nunca acumula retraso.

Esta clase base existe porque RTSP y webcam tienen exactamente el mismo problema. Lo
especifico de cada fuente son dos ganchos: `_abrir_captura()` y, si su URL caduca,
`_toca_renovar()`.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import cv2
import numpy as np

from cvp.core import Frame, SourceInfo
from cvp.sources.errors import SourceError

__all__ = ["LatestFrameSlot", "LiveVideoSource", "Snapshot"]

_log = logging.getLogger(__name__)

# Cuanto espera `close()` a que el hilo lector muera. Solo se agota si la lectura se ha
# quedado colgada dentro de OpenCV; el hilo es `daemon`, asi que ni en ese caso impide
# que el proceso termine.
ESPERA_DE_CIERRE_S = 5.0

# Con lo que se separa un timestamp del anterior cuando el reloj no ha avanzado entre dos
# lecturas. Pasa con un reloj de baja resolucion y, sobre todo, cuando NTP echa el reloj
# atras en un proceso que lleva dias corriendo: sin esto, una serie temporal retrocede.
_MINIMO = timedelta(microseconds=1)

# La escalera de esperas por defecto, la misma que el modelo `Reconnect` de #3.
BACKOFF_POR_DEFECTO = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)


class Snapshot(NamedTuple):
    """El ultimo fotograma leido, con **cuando** se leyo y su numero de publicacion.

    `sequence` es lo que distingue "todavia no ha llegado uno nuevo" de "este ya te lo
    di": el consumidor recuerda el ultimo numero que consumio y solo acepta uno mayor.
    """

    image: np.ndarray
    read_at: datetime
    sequence: int


class LatestFrameSlot:
    """Un unico fotograma compartido entre el hilo lector y el consumidor.

    No es una cola de tamano 1: es un hueco que se **pisa**. Publicar mientras nadie mira
    descarta lo que hubiera, que es justo lo que mantiene fresco lo que sale.

    Sobre la carrera entre publicar y consumir: las dos operaciones van bajo el mismo
    `Condition`, asi que el consumidor nunca ve una publicacion a medias. Y el buffer no
    se copia porque no hace falta: `cv2.VideoCapture.read()` devuelve un array **nuevo**
    en cada llamada, de modo que el hilo lector jamas reescribe uno ya entregado.
    """

    def __init__(self) -> None:
        self._condicion = threading.Condition()
        self._imagen: np.ndarray | None = None
        self._instante: datetime | None = None
        self._secuencia = 0
        self._error: SourceError | None = None
        self._cerrada = False

    def publicar(self, imagen: np.ndarray, instante: datetime) -> None:
        """Guarda `imagen` como el ultimo fotograma y despierta a quien espere."""
        with self._condicion:
            if self._cerrada:
                return
            self._imagen = imagen
            self._instante = instante
            self._secuencia += 1
            self._condicion.notify_all()

    def esperar(self, visto: int = 0, *, timeout: float | None = None) -> Snapshot | None:
        """El ultimo fotograma **mas nuevo que `visto`**, esperando si no hay ninguno.

        Devuelve `None` si la ranura se cierra o si se agota `timeout`, y relanza el error
        con el que murio el hilo lector. Nunca devuelve dos veces el mismo fotograma:
        cuando el consumidor va mas deprisa de lo que llegan, espera al siguiente.
        """
        with self._condicion:
            while self._error is None and not self._cerrada and self._secuencia <= visto:
                if not self._condicion.wait(timeout):
                    return None
            if self._error is not None:
                raise self._error
            if self._secuencia > visto and self._imagen is not None:
                assert self._instante is not None
                return Snapshot(self._imagen, self._instante, self._secuencia)
            return None

    def fallar(self, error: SourceError) -> None:
        """Deja el error del hilo lector para que lo levante el consumidor.

        El hilo no puede lanzar hacia el generador: son pilas distintas. Sin esto, quedarse
        sin reintentos seria indistinguible de un directo que va lento.
        """
        with self._condicion:
            self._error = error
            self._condicion.notify_all()

    def cerrar(self) -> None:
        """Cierra la ranura y despierta a quien espere, para que nadie quede colgado."""
        with self._condicion:
            self._cerrada = True
            self._condicion.notify_all()

    @property
    def sequence(self) -> int:
        """Cuantos fotogramas se han publicado. Util para observar al hilo desde fuera."""
        with self._condicion:
            return self._secuencia


def espera_de_backoff(backoff_s: Sequence[float], intento: int) -> float:
    """La espera del reintento `intento` (1-based), **repitiendo el ultimo valor**.

    Es la semantica que fijo el modelo `Reconnect` de #3: con `[1, 2, 5]` y cinco fallos
    seguidos, las esperas son `[1, 2, 5, 5, 5]`.
    """
    if intento < 1:
        raise ValueError(f"el numero de intento empieza en 1, no en {intento}")
    return float(backoff_s[min(intento, len(backoff_s)) - 1])


class LiveVideoSource:
    """Base de los connectors en directo: hilo lector, muestreo por reloj y reconexion.

    Toma argumentos planos y no un modelo de configuracion, igual que `FileVideoSource`:
    un adaptador tiene que poder instanciarse en un test sin YAML de por medio. `clock` y
    `sleeper` son los puntos de inyeccion que permiten probar el paceo, la caducidad y el
    backoff **sin esperar de verdad**.

    Una subclase implementa:

    - `_abrir_captura()` — devuelve una captura abierta (o lanza `SourceError`).
    - `_descripcion()` — como se llama esta fuente en los mensajes y en el log.
    - `_toca_renovar()` / `_motivo_de_renovacion()` — solo si su URL caduca.
    """

    # Va en el nombre del hilo, que es lo que se ve en un volcado y lo que mira el test
    # que comprueba que `close()` no deja nada vivo.
    _ETIQUETA = "live"

    def __init__(
        self,
        target_fps: float,
        *,
        max_retries: int | None = 20,
        backoff_s: Sequence[float] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], object] | None = None,
    ) -> None:
        if target_fps <= 0:
            raise ValueError(f"target_fps debe ser > 0, no {target_fps!r}")
        if max_retries is not None and max_retries < 0:
            raise ValueError(f"max_retries debe ser >= 0 o None, no {max_retries!r}")
        esperas = list(BACKOFF_POR_DEFECTO) if backoff_s is None else [float(v) for v in backoff_s]
        if not esperas or any(espera <= 0 for espera in esperas):
            raise ValueError(
                f"backoff_s debe ser una lista no vacia de esperas > 0, no {backoff_s!r}"
            )

        self.target_fps = float(target_fps)
        self.max_retries = max_retries
        self.backoff_s = esperas
        # El unico sitio del modulo donde se consulta el reloj de verdad.
        self._clock = clock if clock is not None else _ahora_utc
        self._parar = threading.Event()
        # Por defecto se duerme sobre el evento de parada en vez de con `time.sleep`: un
        # backoff de 60 s con `sleep` haria que `close()` tuviera que esperarlos enteros.
        self._sleeper = sleeper if sleeper is not None else self._parar.wait
        self._ranura: LatestFrameSlot | None = None
        self._hilo: threading.Thread | None = None
        self._info: SourceInfo | None = None
        # Viven en la fuente y no en el generador: el indice no se reinicia al reconectar
        # (contrato de #2) y los timestamps siguen creciendo a traves de una caida.
        self._indice = 0
        self._ultima_marca: datetime | None = None

    # --- Contrato de `VideoSource` ----------------------------------------------------

    @property
    def info(self) -> SourceInfo:
        if self._info is None:
            raise SourceError(
                f"hay que llamar a open() antes de pedir info ({self._descripcion()})"
            )
        return self._info

    def open(self) -> None:
        """Abre la primera captura **en este hilo** y arranca el hilo lector.

        La primera captura no se reintenta: si el directo no esta disponible al arrancar
        todavia no hay datos que proteger, y fallar en el segundo 0 con la causa escrita
        es mejor que un worker que parece vivo y no produce nada.
        """
        if self._hilo is not None and self._hilo.is_alive():
            return
        self._parar.clear()
        captura = self._abrir_captura()
        self._info = self._describir(captura)
        ranura = LatestFrameSlot()
        self._ranura = ranura
        self._hilo = threading.Thread(
            target=self._bucle_lector,
            args=(captura, ranura),
            name=f"cvp-{self._ETIQUETA}-lector",
            daemon=True,
        )
        self._hilo.start()

    def frames(self) -> Iterator[Frame]:
        if self._hilo is None or self._ranura is None:
            raise SourceError(
                f"hay que llamar a open() antes de leer fotogramas ({self._descripcion()})"
            )
        return self._emitir(self._ranura)

    def close(self) -> None:
        """Para el hilo lector y **espera a que muera**. Idempotente.

        Tambien limpia cuando el consumidor abandona el generador a medias: el hilo es de
        la fuente, no del generador, asi que no depende de que nadie lo agote.
        """
        self._parar.set()
        if self._ranura is not None:
            self._ranura.cerrar()
        hilo = self._hilo
        if hilo is not None:
            hilo.join(timeout=ESPERA_DE_CIERRE_S)
            if hilo.is_alive():  # pragma: no cover - solo si OpenCV se cuelga leyendo
                _log.warning(
                    "el hilo lector de %s sigue vivo tras %.0f s; es daemon, asi que no "
                    "impedira que el proceso termine",
                    self._descripcion(),
                    ESPERA_DE_CIERRE_S,
                )
            self._hilo = None

    # --- Ganchos de las subclases -----------------------------------------------------

    def _abrir_captura(self) -> Any:
        """Devuelve una captura abierta, o lanza `SourceError` explicando por que no."""
        raise NotImplementedError

    def _descripcion(self) -> str:
        """Como se nombra esta fuente en los errores y en el log."""
        return self._ETIQUETA

    def _toca_renovar(self) -> bool:
        """Si hay que rehacer la captura aunque siga entregando fotogramas.

        Por defecto no: solo las fuentes cuya URL caduca (YouTube) lo necesitan.
        """
        return False

    def _motivo_de_renovacion(self) -> str:
        return "toca renovar la captura"  # pragma: no cover - solo lo usan las subclases

    # --- El hilo lector ---------------------------------------------------------------

    def _bucle_lector(self, captura: Any, ranura: LatestFrameSlot) -> None:
        """Lee sin parar y guarda solo el ultimo fotograma. Corre en su propio hilo.

        Aqui vive tambien la reconexion, porque es el unico que sabe que la captura ha
        dejado de dar. El contador de reintentos se reinicia cuando **vuelven a llegar
        fotogramas**, no cuando la captura se abre: un directo que se cae un rato cada
        hora no debe agotar el presupuesto al cabo del dia, pero uno que abre y se cae
        acto seguido tampoco puede reintentar para siempre con `max_retries` puesto.
        """
        intentos = 0
        motivo = "sin motivo registrado"
        try:
            while not self._parar.is_set():
                if captura is None:
                    intentos += 1
                    if self.max_retries is not None and intentos > self.max_retries:
                        ranura.fallar(
                            SourceError(
                                f"{self._descripcion()} no ha vuelto tras {intentos - 1} "
                                f"reintentos (max_retries={self.max_retries}); "
                                f"ultimo motivo: {motivo}"
                            )
                        )
                        return
                    espera = espera_de_backoff(self.backoff_s, intentos)
                    _log.warning(
                        "reconectando con %s (intento %d/%s) porque %s; esperando %.1f s",
                        self._descripcion(),
                        intentos,
                        "sin limite" if self.max_retries is None else self.max_retries,
                        motivo,
                        espera,
                    )
                    self._sleeper(espera)
                    if self._parar.is_set():
                        return
                    try:
                        captura = self._abrir_captura()
                    except SourceError as exc:
                        motivo = str(exc)
                        _log.warning(
                            "el intento %d de reconectar con %s ha fallado: %s",
                            intentos,
                            self._descripcion(),
                            exc,
                        )
                    continue

                if self._toca_renovar():
                    motivo = self._motivo_de_renovacion()
                    _log.info("renovando la captura de %s porque %s", self._descripcion(), motivo)
                    self._soltar(captura)
                    captura = None
                    try:
                        captura = self._abrir_captura()
                    except SourceError as exc:
                        motivo = f"la renovacion ha fallado ({exc})"
                        _log.warning("la renovacion de %s ha fallado: %s", self._descripcion(), exc)
                    continue

                ok, imagen = captura.read()
                if not ok or imagen is None:
                    motivo = "ha dejado de entregar fotogramas"
                    _log.warning("%s %s", self._descripcion(), motivo)
                    self._soltar(captura)
                    captura = None
                    continue

                ranura.publicar(imagen, self._clock())
                if intentos:
                    _log.info(
                        "%s vuelve a entregar fotogramas tras %d intento(s); el contador "
                        "de reintentos se reinicia",
                        self._descripcion(),
                        intentos,
                    )
                    intentos = 0
        except Exception as exc:  # pragma: no cover - red de seguridad del hilo
            _log.exception("el hilo lector de %s ha muerto", self._descripcion())
            ranura.fallar(SourceError(f"el hilo lector de {self._descripcion()} ha muerto: {exc}"))
        finally:
            self._soltar(captura)
            ranura.cerrar()

    # --- El consumidor ----------------------------------------------------------------

    def _emitir(self, ranura: LatestFrameSlot) -> Iterator[Frame]:
        """Emite el fotograma **mas fresco que haya**, como mucho `target_fps` por segundo.

        El paceo es contra el reloj de pared, no contra la posicion en el medio: con
        `target_fps=5` pasan al menos 200 ms entre dos emisiones y sale el mas reciente
        que haya llegado, no el siguiente de una cola. Una fuente en directo no termina
        sola: este generador solo acaba cuando se cierra la fuente.
        """
        periodo = timedelta(seconds=1.0 / self.target_fps)
        visto = 0
        proxima: datetime | None = None
        while not self._parar.is_set():
            if proxima is not None:
                retraso = (proxima - self._clock()).total_seconds()
                if retraso > 0:
                    self._sleeper(retraso)
            instantanea = ranura.esperar(visto)
            if instantanea is None:
                return
            visto = instantanea.sequence
            # El instante es el de la **lectura**, no el de la emision: el analizador
            # quiere saber cuando paso, no cuando lo miramos.
            marca = instantanea.read_at
            if self._ultima_marca is not None and marca <= self._ultima_marca:
                marca = self._ultima_marca + _MINIMO
            self._ultima_marca = marca
            yield Frame(image=instantanea.image, index=self._indice, timestamp=marca)
            self._indice += 1
            proxima = self._clock() + periodo

    # --- Utilidades -------------------------------------------------------------------

    def _describir(self, captura: Any) -> SourceInfo:
        """`SourceInfo` a partir de lo que reporte la captura ya abierta.

        A diferencia del connector de fichero, aqui un frame rate desconocido **no es un
        error**: HLS y RTSP no siempre lo declaran y el muestreo no lo necesita, porque se
        pacea contra el reloj de pared. `SourceInfo.native_fps=None` es exactamente eso.
        """
        ancho = int(captura.get(cv2.CAP_PROP_FRAME_WIDTH))
        alto = int(captura.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if ancho <= 0 or alto <= 0:
            self._soltar(captura)
            raise SourceError(
                f"OpenCV no sabe las dimensiones de {self._descripcion()}: {ancho}x{alto}"
            )
        declarado = captura.get(cv2.CAP_PROP_FPS)
        nativo = (
            float(declarado)
            if declarado is not None and math.isfinite(declarado) and declarado > 0
            else None
        )
        return SourceInfo(width=ancho, height=alto, native_fps=nativo, is_live=True)

    @staticmethod
    def _soltar(captura: Any) -> None:
        """Libera una captura sin dejar que su fallo tumbe el hilo lector."""
        if captura is None:
            return
        try:
            captura.release()
        except Exception:  # pragma: no cover - OpenCV avisa devolviendo False, no lanzando
            _log.debug("no se ha podido liberar la captura", exc_info=True)


def _ahora_utc() -> datetime:
    """El reloj de verdad. Es el valor por defecto y no lo usa ningun test."""
    return datetime.now(UTC)
