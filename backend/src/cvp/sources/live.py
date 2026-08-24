"""Lo que comparten todas las fuentes **en directo**: un hilo lector y una linea de tiempo.

Un fichero espera y un directo no, asi que quien lee no puede ser quien analiza: un **hilo
lector** consume el stream a su ritmo y el consumidor toma lo que hay. Eso lo resolvio #5.

Lo que #5 no podia ver, porque hacia falta el pipeline entero corriendo, es **como entrega
un directo por HLS**. Medido en #8 contra la fuente real: el hilo lector decodifica a 34 fps
sin despeinarse, pero no los reparte en el tiempo. Llega un segmento entero de golpe -unos
135 fps durante ~1 s- y despues ~3 s de nada. Sobre eso, un consumidor paceado contra el
**reloj de pared** solo muestrea mientras dura la rafaga: 1,55 fps de los 5 pedidos, el 71 %
del tiempo bloqueado, y timestamps comprimidos (200 ms de reloj sobre fotogramas separados
900 ms de video). Bajar la resolucion lo empeoraba, que es lo que confirmo el diagnostico.

Las tres piezas de este modulo responden a eso:

- `VideoTimeline` - a cada fotograma decodificado le corresponde un instante **del video**,
  `ancla + n / native_fps`, no el instante en que se leyo. El muestreo se decide sobre esa
  linea, asi que sale igual llegue como llegue el transporte.
- `RecentFrameBuffer` - un buffer **acotado** de fotogramas ya seleccionados. Existe porque
  una rafaga trae 4 s de video en 1 s y despues no hay nada: sin algo que absorba eso, el
  analisis se queda parado tres cuartas partes del tiempo. Cuando se llena descarta **el
  mas antiguo**, que es lo que mantiene el retraso acotado en vez de creciente.
- `LiveVideoSource` - el ciclo de vida, la reconexion con backoff y el hilo lector.

El consumidor ya no se pacea contra nada: pide y procesa tan rapido como puede, y quien
decide cuantos fotogramas por segundo de video salen es el hilo lector.

Esta clase base existe porque RTSP y webcam tienen el mismo problema. Lo especifico de cada
fuente son dos ganchos: `_abrir_captura()` y, si su URL caduca, `_toca_renovar()`.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import cv2
import numpy as np

from cvp.core import Frame, SourceInfo
from cvp.sources.errors import SourceError

__all__ = ["LiveVideoSource", "RecentFrameBuffer", "Snapshot", "VideoTimeline"]

_log = logging.getLogger(__name__)

# Cuanto espera `close()` a que el hilo lector muera. Solo se agota si la lectura se ha
# quedado colgada dentro de OpenCV; el hilo es `daemon`, asi que ni en ese caso impide
# que el proceso termine.
ESPERA_DE_CIERRE_S = 5.0

# La escalera de esperas por defecto, la misma que el modelo `Reconnect` de #3.
BACKOFF_POR_DEFECTO = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)

# Cuanto video puede quedar esperando por delante del analisis. Es el ancho de la rafaga
# que hay que absorber: los segmentos de la fuente del proyecto 01 traen ~4,8 s de video y
# dejan ~3,1 s de silencio detras (`_docs/rendimiento.md`), asi que por debajo de ~3 s el
# analisis se vuelve a quedar parado entre segmento y segmento.
#
# Cuesta memoria y conviene decirlo: 4 s a `target_fps: 5` son 20 fotogramas, y a 1080p
# eso son ~124 MB. Es el precio de no estar parado el 71 % del tiempo.
MAX_BACKLOG_S = 4.0

# Cuanto puede quedarse el tiempo de video por detras del reloj antes de re-anclar. Tiene
# que estar bastante por encima de la oscilacion normal del transporte (dentro de un ciclo
# de segmento, el tiempo de video adelanta y atrasa varios segundos respecto al reloj) y
# por debajo de lo que deformaria una agregacion por minuto. 30 s es ademas el orden de la
# latencia que ya trae un directo por HLS, que es incertidumbre que no podemos medir.
MAX_DERIVA_S = 30.0


class Snapshot(NamedTuple):
    """Un fotograma **seleccionado**, con el instante del video al que corresponde."""

    image: np.ndarray
    timestamp: datetime


class VideoTimeline:
    """El tiempo del video, anclado al reloj de pared, y que fotogramas se muestrean.

    El instante de un fotograma no es cuando se leyo, sino donde cae en el video:
    `ancla + n / native_fps`, con `n` contando **todos** los fotogramas decodificados desde
    el ancla. La diferencia no es teorica: dentro de una rafaga se leen 4 s de video en 1 s
    de reloj, asi que los dos relojes discrepan en segundos, y es el del video el que quiere
    el analizador ("cuando paso", no "cuando lo miramos").

    El muestreo se decide sobre esa linea con la misma regla que el connector de fichero
    (#4): se emite cuando el contador de "cuantos deberian haber salido ya" cruza un entero.
    Reparte el descarte de forma uniforme aunque `target_fps` no divida al nativo, y es
    **independiente del reloj**, que es justo lo que hace que un transporte a rafagas y uno
    uniforme den exactamente la misma secuencia.

    Dos casos que no se leen en la formula:

    - **La deriva.** Si el decodificador no sostiene el ritmo del directo, `n / native_fps`
      se queda cada vez mas por detras del reloj y los timestamps acabarian diciendo las
      12:00 cuando son las 12:40. Pasado `max_drift_s` se re-ancla y se deja dicho en el
      log, porque un re-anclaje es un salto en la serie temporal.
    - **`native_fps` desconocido.** Un HLS puede no declararlo (`SourceInfo.native_fps=None`,
      contemplado en #2). Sin fps nativos no hay tiempo de video que derivar, y estimarlo de
      lo que tarda en llegar seria peor que no hacerlo: sobre un transporte a rafagas se
      mediria la velocidad de la rafaga (135 fps) y no la del video. Se vuelve entonces al
      muestreo y a los timestamps del reloj de pared, diciendolo en el log.
    """

    def __init__(
        self,
        target_fps: float,
        native_fps: float | None,
        *,
        clock: Callable[[], datetime],
        max_drift_s: float = MAX_DERIVA_S,
        nombre: str = "directo",
    ) -> None:
        if target_fps <= 0:
            raise ValueError(f"target_fps debe ser > 0, no {target_fps!r}")
        if native_fps is not None and native_fps <= 0:
            raise ValueError(f"native_fps debe ser > 0 o None, no {native_fps!r}")
        if max_drift_s <= 0:
            raise ValueError(f"max_drift_s debe ser > 0, no {max_drift_s!r}")
        self.target_fps = float(target_fps)
        self.native_fps = None if native_fps is None else float(native_fps)
        self.max_drift_s = float(max_drift_s)
        self._clock = clock
        self._nombre = nombre
        self._periodo = timedelta(seconds=1.0 / self.target_fps)
        # Capado a 1: pedir mas fps de los que trae el video no duplica fotogramas.
        self._ratio = (
            1.0 if self.native_fps is None else min(self.target_fps / self.native_fps, 1.0)
        )
        self._ancla: datetime | None = None
        self._decodificados = 0
        self._ultimo: datetime | None = None
        self._aviso_de_adelanto = False
        if self.native_fps is None:
            _log.warning(
                "%s no declara frame rate, asi que no hay tiempo de video que derivar: se "
                "muestrea y se marca contra el reloj de pared. Si el transporte entrega a "
                "rafagas, los timestamps llevaran esa deformacion",
                self._nombre,
            )

    @property
    def ultimo(self) -> datetime | None:
        """El instante del ultimo fotograma seleccionado, o `None` si no hay ninguno."""
        return self._ultimo

    def anclar(self, motivo: str) -> None:
        """Ata el tiempo de video al reloj de pared. Al abrir y en **cada** reconexion.

        Nunca ancla hacia atras. Retroceder no serviria de nada: los timestamps que ya han
        salido tienen que seguir creciendo, asi que el analisis se quedaria congelado
        repitiendo el mismo instante hasta que el video volviera a alcanzarlo.
        """
        ahora = self._clock()
        ancla = ahora
        if self._ultimo is not None and ancla <= self._ultimo:
            ancla = self._ultimo + self._periodo
        self._ancla = ancla
        self._decodificados = 0
        self._aviso_de_adelanto = False
        _log.info("%s: tiempo de video anclado a %s (%s)", self._nombre, ancla.isoformat(), motivo)

    def muestrear(self) -> datetime | None:
        """Un fotograma decodificado. Devuelve su instante si toca emitirlo, o `None`.

        Se llama **una vez por fotograma decodificado**, tambien por los que se descartan:
        son ellos los que hacen avanzar el tiempo de video.
        """
        if self.native_fps is None:
            return self._muestrear_por_reloj()
        if self._ancla is None:  # pragma: no cover - `open()` siempre ancla antes
            self.anclar("primer fotograma")
        n = self._decodificados
        self._decodificados = n + 1
        if n and int(n * self._ratio) == int((n - 1) * self._ratio):
            return None
        assert self._ancla is not None
        marca = self._vigilar_deriva(self._ancla + timedelta(seconds=n / self.native_fps))
        self._ultimo = marca
        return marca

    def _muestrear_por_reloj(self) -> datetime | None:
        """El plan B de `native_fps=None`: se muestrea y se marca con el reloj de pared."""
        ahora = self._clock()
        if self._ultimo is not None and ahora < self._ultimo + self._periodo:
            return None
        self._ultimo = ahora
        return ahora

    def _vigilar_deriva(self, marca: datetime) -> datetime:
        """Re-ancla si el tiempo de video se ha quedado descolgado del reloj."""
        deriva = (self._clock() - marca).total_seconds()
        if deriva > self.max_drift_s:
            _log.warning(
                "%s: el tiempo de video iba %.0f s por detras del reloj, mas de los %.0f s "
                "de margen; se re-ancla, asi que habra un salto en la serie temporal",
                self._nombre,
                deriva,
                self.max_drift_s,
            )
            self.anclar(f"deriva de {deriva:.0f} s")
            # Este fotograma es ya el primero de la epoca nueva.
            self._decodificados = 1
            assert self._ancla is not None
            return self._ancla
        if -deriva > self.max_drift_s and not self._aviso_de_adelanto:
            # No se re-ancla hacia atras (ver `anclar`). Se avisa una vez por epoca: la
            # causa habitual es que el decodificador vacie de golpe el buffer del
            # transporte al arrancar, y entonces el desfase se queda quieto y es
            # indistinguible de la latencia que ya trae el propio directo.
            self._aviso_de_adelanto = True
            _log.warning(
                "%s: el tiempo de video va %.0f s por delante del reloj; no se re-ancla "
                "hacia atras porque los timestamps no pueden retroceder",
                self._nombre,
                -deriva,
            )
        return marca


class RecentFrameBuffer:
    """Los fotogramas ya seleccionados que el analisis todavia no ha consumido.

    Ni una cola sin fondo ni un hueco de uno solo, y las dos cosas son deliberadas:

    - **Sin fondo** seria el fallo que #5 existe para evitar: cada pico de latencia se
      convertiria en retraso permanente y a las tres horas el analisis iria nueve minutos
      por detras de la realidad sin que nada lo dijera.
    - **De uno solo** era lo de #5, y con un transporte a rafagas deja al analisis parado
      entre segmento y segmento: los veinte fotogramas que llegan de golpe se pisan unos a
      otros y despues no hay nada que hacer durante 3 s. Medido: 0,85 fps de 5.

    Acotado y descartando **el mas antiguo** se tienen las dos cosas: el analisis siempre
    tiene con que trabajar, y su retraso respecto al directo no puede pasar de la
    profundidad del buffer, que es un numero fijo y conocido, no una cola que crece.

    Cuando el consumidor sostiene el ritmo, el buffer se vacia y lo que recibe es el
    fotograma recien seleccionado; cuando no lo sostiene, se descarta lo mas viejo y los
    indices de origen saltan, que es la forma correcta de ir con menos fotogramas.
    """

    def __init__(self, maxlen: int) -> None:
        if maxlen < 1:
            raise ValueError(f"el buffer tiene que caber al menos un fotograma, no {maxlen!r}")
        self._condicion = threading.Condition()
        self._cola: deque[Snapshot] = deque(maxlen=maxlen)
        self._descartados = 0
        self._error: SourceError | None = None
        self._cerrada = False

    @property
    def maxlen(self) -> int:
        return self._cola.maxlen or 0

    @property
    def descartados(self) -> int:
        """Cuantos fotogramas seleccionados se han tirado por no caber."""
        with self._condicion:
            return self._descartados

    def __len__(self) -> int:
        with self._condicion:
            return len(self._cola)

    def publicar(self, imagen: np.ndarray, marca: datetime) -> bool:
        """Encola un fotograma. Devuelve `True` si ha habido que descartar el mas antiguo."""
        with self._condicion:
            if self._cerrada:
                return False
            lleno = len(self._cola) == self._cola.maxlen
            if lleno:
                self._descartados += 1
            # `deque` con `maxlen` descarta el mas antiguo por su cuenta al desbordar.
            self._cola.append(Snapshot(imagen, marca))
            self._condicion.notify_all()
            return lleno

    def tomar(self, *, timeout: float | None = None) -> Snapshot | None:
        """El fotograma mas antiguo que quede, esperando si no hay ninguno.

        Devuelve `None` si el buffer se cierra o si se agota `timeout`, y relanza el error
        con el que murio el hilo lector. Nunca devuelve dos veces el mismo fotograma: sale
        de la cola al entregarlo.
        """
        with self._condicion:
            while self._error is None and not self._cerrada and not self._cola:
                if not self._condicion.wait(timeout):
                    return None
            if self._error is not None:
                raise self._error
            if self._cola:
                return self._cola.popleft()
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
        """Cierra el buffer, lo vacia y despierta a quien espere.

        Se vacia porque lo que guarda son fotogramas descodificados: a 1080p, veinte son
        ~124 MB que no tienen por que sobrevivir a `close()`.
        """
        with self._condicion:
            self._cerrada = True
            self._cola.clear()
            self._condicion.notify_all()


def espera_de_backoff(backoff_s: Sequence[float], intento: int) -> float:
    """La espera del reintento `intento` (1-based), **repitiendo el ultimo valor**.

    Es la semantica que fijo el modelo `Reconnect` de #3: con `[1, 2, 5]` y cinco fallos
    seguidos, las esperas son `[1, 2, 5, 5, 5]`.
    """
    if intento < 1:
        raise ValueError(f"el numero de intento empieza en 1, no en {intento}")
    return float(backoff_s[min(intento, len(backoff_s)) - 1])


class LiveVideoSource:
    """Base de los connectors en directo: hilo lector, muestreo en video y reconexion.

    Toma argumentos planos y no un modelo de configuracion, igual que `FileVideoSource`:
    un adaptador tiene que poder instanciarse en un test sin YAML de por medio. `clock` y
    `sleeper` son los puntos de inyeccion que permiten probar el muestreo, la caducidad y
    el backoff **sin esperar de verdad**.

    Una subclase implementa:

    - `_abrir_captura()` - devuelve una captura abierta (o lanza `SourceError`).
    - `_descripcion()` - como se llama esta fuente en los mensajes y en el log.
    - `_toca_renovar()` / `_motivo_de_renovacion()` - solo si su URL caduca.
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
        max_backlog_s: float = MAX_BACKLOG_S,
        max_drift_s: float = MAX_DERIVA_S,
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
        if max_backlog_s <= 0:
            raise ValueError(f"max_backlog_s debe ser > 0, no {max_backlog_s!r}")
        if max_drift_s <= 0:
            raise ValueError(f"max_drift_s debe ser > 0, no {max_drift_s!r}")

        self.target_fps = float(target_fps)
        self.max_retries = max_retries
        self.backoff_s = esperas
        self.max_backlog_s = float(max_backlog_s)
        self.max_drift_s = float(max_drift_s)
        # El unico sitio del modulo donde se consulta el reloj de verdad.
        self._clock = clock if clock is not None else _ahora_utc
        self._parar = threading.Event()
        # Por defecto se duerme sobre el evento de parada en vez de con `time.sleep`: un
        # backoff de 60 s con `sleep` haria que `close()` tuviera que esperarlos enteros.
        self._sleeper = sleeper if sleeper is not None else self._parar.wait
        self._buffer: RecentFrameBuffer | None = None
        self._linea: VideoTimeline | None = None
        self._hilo: threading.Thread | None = None
        self._info: SourceInfo | None = None
        # Vive en la fuente y no en el generador: el indice no se reinicia al reconectar,
        # que es el contrato de #2 y lo que hace que `every_n_frames` siga significando lo
        # mismo despues de una caida.
        self._indice = 0

    @property
    def profundidad_del_buffer(self) -> int:
        """Cuantos fotogramas seleccionados caben esperando al analisis."""
        return max(1, round(self.max_backlog_s * self.target_fps))

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
        linea = VideoTimeline(
            self.target_fps,
            self._info.native_fps,
            clock=self._clock,
            max_drift_s=self.max_drift_s,
            nombre=self._descripcion(),
        )
        linea.anclar("apertura")
        buffer = RecentFrameBuffer(self.profundidad_del_buffer)
        self._linea = linea
        self._buffer = buffer
        self._hilo = threading.Thread(
            target=self._bucle_lector,
            args=(captura, buffer, linea),
            name=f"cvp-{self._ETIQUETA}-lector",
            daemon=True,
        )
        self._hilo.start()

    def frames(self) -> Iterator[Frame]:
        if self._hilo is None or self._buffer is None:
            raise SourceError(
                f"hay que llamar a open() antes de leer fotogramas ({self._descripcion()})"
            )
        return self._emitir(self._buffer)

    def close(self) -> None:
        """Para el hilo lector y **espera a que muera**. Idempotente.

        Tambien limpia cuando el consumidor abandona el generador a medias: el hilo es de
        la fuente, no del generador, asi que no depende de que nadie lo agote.
        """
        self._parar.set()
        if self._buffer is not None:
            self._buffer.cerrar()
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

    def _bucle_lector(self, captura: Any, buffer: RecentFrameBuffer, linea: VideoTimeline) -> None:
        """Decodifica sin parar, selecciona en tiempo de video y encola. En su propio hilo.

        Decodifica **todos** los fotogramas -son ellos los que hacen avanzar el tiempo de
        video- y solo encola los que caen en la rejilla de `target_fps`. Que la decision de
        muestreo viva aqui y no en el consumidor es lo que hace que un transporte a rafagas
        y uno uniforme den la misma secuencia.

        Por eso lee en dos tiempos: `grab()` para todos y `retrieve()` solo para los que
        salen. Decidir antes de materializar la imagen ahorra la conversion a BGR de cinco
        de cada seis fotogramas.

        Aqui vive tambien la reconexion, porque es el unico que sabe que la captura ha
        dejado de dar. El contador de reintentos se reinicia cuando **vuelven a llegar
        fotogramas**, no cuando la captura se abre: un directo que se cae un rato cada
        hora no debe agotar el presupuesto al cabo del dia, pero uno que abre y se cae
        acto seguido tampoco puede reintentar para siempre con `max_retries` puesto.
        """
        intentos = 0
        motivo = "sin motivo registrado"
        aviso_de_descartes = False
        try:
            while not self._parar.is_set():
                if captura is None:
                    intentos += 1
                    if self.max_retries is not None and intentos > self.max_retries:
                        buffer.fallar(
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
                    else:
                        # La captura nueva cuenta fotogramas desde cero, asi que el tiempo
                        # de video tiene que volver a atarse al reloj.
                        linea.anclar("reconexion")
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
                    else:
                        linea.anclar("renovacion de la URL")
                    continue

                if not captura.grab():
                    motivo = "ha dejado de entregar fotogramas"
                    _log.warning("%s %s", self._descripcion(), motivo)
                    self._soltar(captura)
                    captura = None
                    continue

                if intentos:
                    _log.info(
                        "%s vuelve a entregar fotogramas tras %d intento(s); el contador "
                        "de reintentos se reinicia",
                        self._descripcion(),
                        intentos,
                    )
                    intentos = 0

                marca = linea.muestrear()
                if marca is None:
                    # Descartado, y sin pagar por el. `grab()` descodifica -no hay forma de
                    # no hacerlo, los fotogramas de H.264 dependen unos de otros- pero se
                    # salta la conversion de color y la copia de 6 MB que hace `retrieve()`
                    # en un 1080p. Cinco de cada seis fotogramas se van por aqui, y en un
                    # chip de 15 W eso es CPU que necesita el detector.
                    continue

                ok, imagen = captura.retrieve()
                if not ok or imagen is None:
                    motivo = "no ha entregado un fotograma que ya habia descodificado"
                    _log.warning("%s %s", self._descripcion(), motivo)
                    self._soltar(captura)
                    captura = None
                    continue
                if buffer.publicar(imagen, marca) and not aviso_de_descartes:
                    aviso_de_descartes = True
                    _log.info(
                        "%s: el analisis no sostiene los %.1f fps pedidos, asi que se "
                        "descartan los fotogramas mas antiguos del buffer (%.1f s de "
                        "video). El retraso se queda ahi y no crece",
                        self._descripcion(),
                        self.target_fps,
                        self.max_backlog_s,
                    )
        except Exception as exc:  # pragma: no cover - red de seguridad del hilo
            _log.exception("el hilo lector de %s ha muerto", self._descripcion())
            buffer.fallar(SourceError(f"el hilo lector de {self._descripcion()} ha muerto: {exc}"))
        finally:
            self._soltar(captura)
            buffer.cerrar()

    # --- El consumidor ----------------------------------------------------------------

    def _emitir(self, buffer: RecentFrameBuffer) -> Iterator[Frame]:
        """Entrega lo que el hilo lector haya seleccionado, sin pacear nada.

        No consulta el reloj ni duerme: quien decide cuantos fotogramas por segundo de
        video salen es el hilo lector, y pacear otra vez aqui solo sumaria el periodo al
        tiempo de proceso en vez de solaparlo (~200 ms por fotograma regalados, medidos en
        #8). Si hay fotograma, sale ya; si no, se espera a que llegue.

        Una fuente en directo no termina sola: este generador solo acaba cuando se cierra.
        """
        while not self._parar.is_set():
            instantanea = buffer.tomar()
            if instantanea is None:
                return
            yield Frame(
                image=instantanea.image, index=self._indice, timestamp=instantanea.timestamp
            )
            self._indice += 1

    # --- Utilidades -------------------------------------------------------------------

    def _describir(self, captura: Any) -> SourceInfo:
        """`SourceInfo` a partir de lo que reporte la captura ya abierta.

        A diferencia del connector de fichero, aqui un frame rate desconocido **no es un
        error**: HLS y RTSP no siempre lo declaran, y `SourceInfo.native_fps=None` es
        exactamente ese caso (#2). Lo que si cambia es como se muestrea, y de eso se
        encarga `VideoTimeline`, que lo deja dicho en el log.
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
