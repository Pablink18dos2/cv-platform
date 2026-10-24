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

from cvp.core import AnalyzerOutput, Event, Frame, Metric, SourceInfo
from cvp.sources.errors import SourceError

__all__ = ["LiveVideoSource", "RecentFrameBuffer", "Snapshot", "VideoTimeline"]

_log = logging.getLogger(__name__)

# Cuanto espera `close()` a que el hilo lector muera. Solo se agota si la lectura se ha
# quedado colgada dentro de OpenCV; el hilo es `daemon`, asi que ni en ese caso impide
# que el proceso termine.
ESPERA_DE_CIERRE_S = 5.0

# La escalera de esperas por defecto, la misma que el modelo `Reconnect` de #3.
BACKOFF_POR_DEFECTO = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)

# Cuanto video puede quedar esperando por delante del analisis. Tiene que caber **un
# segmento entero y sobrar**: los de la fuente del proyecto 01 traen ~4,8 s de video de
# golpe (`_docs/rendimiento.md`), asi que con 4 s una sola rafaga llenaba el buffer ella
# sola y el fotograma siguiente ya desalojaba, aunque el consumidor fuera al dia. Eso era
# un bloque contiguo tirado de la cabeza de la cola, o sea un hueco en la serie temporal,
# antes de que la realimentacion hubiera tenido tiempo de medir nada.
#
# Con 8 s la rafaga entra entera y queda margen para el ciclo siguiente, que es todo lo
# que hace falta: el retraso **tipico** no lo fija este numero sino el muestreo adaptativo,
# que lo lleva a cero; esto es solo el tope de un transitorio.
#
# Cuesta memoria y conviene decirlo: lo que de verdad se guarda es una rafaga, ~20
# fotogramas, que a 1080p son ~124 MB. El tope solo se toca si el analisis se para del
# todo 8 s seguidos.
MAX_BACKLOG_S = 8.0

# Cuanto puede quedarse el tiempo de video por detras del reloj antes de re-anclar. Tiene
# que estar bastante por encima de la oscilacion normal del transporte (dentro de un ciclo
# de segmento, el tiempo de video adelanta y atrasa varios segundos respecto al reloj) y
# por debajo de lo que deformaria una agregacion por minuto. 30 s es ademas el orden de la
# latencia que ya trae un directo por HLS, que es incertidumbre que no podemos medir.
MAX_DERIVA_S = 30.0

# Cada cuanto (de reloj) se mira si el analisis sostiene el ritmo y se ajusta el muestreo.
# Es del orden de un ciclo de segmento HLS: mas corto reaccionaria al ruido de una rafaga,
# mas largo tardaria en enterarse de que la maquina ya no da abasto.
VENTANA_DE_AJUSTE_S = 4.0

# Cuanto se sube el muestreo por ventana en la que el consumidor va sobrado, hasta el
# `target_fps` pedido. La bajada es de golpe a lo que sostiene y la subida a tientas, que
# es la asimetria de siempre: pasarse por arriba se paga en huecos y quedarse corto solo
# en fotogramas que no se miran.
SUBIDA_POR_VENTANA = 1.25

# Suelo del muestreo adaptativo, como fraccion del `target_fps`. Un consumidor atascado
# mediria cero y sin suelo dejariamos de muestrear del todo, que es peor que ir lento.
SUELO_DEL_MUESTREO = 0.1

# En cuantas ventanas se drena el retraso que no se va solo, y cuanto puede recortar ese
# drenaje por debajo de la capacidad medida. Drenarlo de una vez -que es lo que hacia la
# primera version- hunde el muestreo en cuanto hay un arranque con el buffer lleno: medido
# contra el directo, la primera ventana lo mandaba de 5 fps al suelo de 0,5 y tardaba
# minutos en volver a subir, con 2,47 fps de salida en vez de 3,7.
DRENAJE_EN_VENTANAS = 4.0

# Margen antes de dar por bueno que el consumidor no da abasto, y lo mas que se le puede
# bajar el muestreo en una ventana. El detector tiene un p95 de cuatro veces su mediana
# (`_docs/rendimiento.md`), asi que en una ventana de 4 s caben dos inferencias lentas que
# no dicen nada de la capacidad de la maquina. Sin amortiguar, el muestreo persigue ese
# ruido hacia abajo y no vuelve: medido contra el directo, 35 ajustes en 200 s y 2,85 fps
# de salida en vez de los 3,7 que da la maquina.
HOLGURA_DE_CONSUMO = 0.9
BAJADA_MAXIMA = 0.85

# Cada cuanto (de reloj) se repite el aviso de que el retraso o el adelanto siguen por
# encima de `max_drift_s`. Un aviso unico para una condicion que dura horas no informa: a
# las dos horas nadie sabe si el desfase son dos segundos o una hora. Una vez por minuto
# deja el valor actual en el log sin inundarlo.
AVISO_CADA_S = 60.0

# Cada cuanto (de reloj) la fuente ofrece al runner una muestra de la latencia del
# fotograma que se analiza. Es la metrica continua de #26: sin ella, un retraso que crece
# despacio solo se ve cuando ya ha provocado un re-anclaje.
METRICA_CADA_S = 10.0

# Con que `analyzer` y `name` sale lo que la fuente cuenta de si misma. La fuente no es un
# analizador, pero los sinks y la API ya persisten y sirven eventos y metricas por esa
# clave, asi que es el canal que #9 y #12 ya conocen y no hace falta otro.
ANALIZADOR_FUENTE = "source"
EVENTO_REANCLAJE = "reanchor"
METRICA_LATENCIA = "source_latency_s"

# Cuantos eventos pendientes de recoger guarda la fuente. Un runner los recoge en cada
# fotograma; el tope existe para que una fuente que nadie drena (un script, un test) no
# crezca sin limite durante dias.
EVENTOS_PENDIENTES_MAX = 1000


class Snapshot(NamedTuple):
    """Un fotograma **seleccionado**, con el instante del video al que corresponde."""

    image: np.ndarray
    timestamp: datetime
    # Cuando se leyo y cuanto se separaba ya entonces del borde en vivo. Con ellos el
    # consumidor dice la latencia **total** del fotograma que analiza: la que traia del
    # decodificador mas lo que ha esperado en el buffer.
    leido_a: datetime | None = None
    retraso_s: float = 0.0


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
      12:00 cuando son las 12:40. Pasado `max_drift_s` se re-ancla, y como un re-anclaje
      es un salto en la serie temporal no basta con el log: queda como `Event` con el
      salto en segundos (#26). Re-anclar arregla la etiqueta, no el retraso, asi que ademas
      se mide la latencia real (`medir_retraso`) y se avisa **cada minuto** mientras dure.
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
        al_reanclar: Callable[[Event], object] | None = None,
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
        self._al_reanclar = al_reanclar
        self._tasa = self.target_fps
        self._periodo = timedelta(seconds=1.0 / self.target_fps)
        self._ancla: datetime | None = None
        self._decodificados = 0
        self._ultimo: datetime | None = None
        self._proximo: datetime | None = None
        self._avisos: dict[str, datetime] = {}
        # Reloj y PTS del primer fotograma de la captura actual: la referencia contra la
        # que `medir_retraso` ve si el stream avanza mas despacio que el reloj.
        self._base_reloj: datetime | None = None
        self._base_pts: float | None = None
        self._ultimo_pts: float | None = None
        # Lo que los re-anclajes por deriva han "perdonado" a la etiqueta desde que se abrio
        # la captura. Sin PTS es lo unico que impide que re-anclar borre el retraso medido.
        self._absorbido_s = 0.0
        self._seleccionados = 0
        self._inicio_de_ventana: datetime | None = None
        self._tomados_al_abrir = 0
        self._seleccionados_al_abrir = 0
        self._valle = 0
        self._ha_arrancado = False
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

    def anclar(self, motivo: str, *, captura_nueva: bool = True) -> None:
        """Ata el tiempo de video al reloj de pared. Al abrir y en **cada** reconexion.

        Nunca ancla hacia atras. Retroceder no serviria de nada: los timestamps que ya han
        salido tienen que seguir creciendo, asi que el analisis se quedaria congelado
        repitiendo el mismo instante hasta que el video volviera a alcanzarlo. La
        consecuencia es que **un adelanto no se corrige**: una reconexion con el video
        600 s por delante arranca otra vez 600 s por delante. No se puede arreglar, pero
        tampoco se hereda en silencio: el evento lo lleva en `ahead_s`.

        `captura_nueva` es `False` solo en el re-anclaje por deriva, que sigue leyendo de
        la misma captura: el PTS de referencia de `medir_retraso` no se toca, porque el
        retraso del decodificador sigue siendo el mismo aunque la etiqueta se haya movido.
        """
        ahora = self._clock()
        ancla = ahora
        if self._ultimo is not None and ancla <= self._ultimo:
            ancla = self._ultimo + self._periodo
        # Donde estaria ahora el fotograma de seguir con el ancla vieja: lo que separa a
        # ese instante del nuevo es el escalon que ve quien mire la serie.
        esperado = None
        if self._ancla is not None and self.native_fps is not None:
            esperado = self._ancla + timedelta(seconds=self._decodificados / self.native_fps)
        self._ancla = ancla
        self._decodificados = 0
        # La rejilla de muestreo se recoloca sobre el ancla nueva: el primer fotograma que
        # llegue detras de una caida sale, en vez de esperar a cuadrar con la rejilla vieja.
        self._proximo = None
        self._avisos.clear()
        if captura_nueva:
            self._base_reloj = self._base_pts = self._ultimo_pts = None
            self._absorbido_s = 0.0
        _log.info("%s: tiempo de video anclado a %s (%s)", self._nombre, ancla.isoformat(), motivo)
        if esperado is not None and self._ultimo is not None:
            self._anotar_reanclaje(motivo, ancla, (ancla - esperado).total_seconds(), ahora)

    def _anotar_reanclaje(
        self, motivo: str, ancla: datetime, salto_s: float, ahora: datetime
    ) -> None:
        """Deja el re-anclaje como `Event` y, si arrastra un adelanto, lo dice."""
        adelanto_s = max((ancla - ahora).total_seconds(), 0.0)
        if adelanto_s > self.max_drift_s:
            _log.warning(
                "%s: tras el re-anclaje (%s) el tiempo de video sigue %.0f s por delante del "
                "reloj; no se puede retroceder, asi que el adelanto se hereda",
                self._nombre,
                motivo,
                adelanto_s,
            )
        if self._al_reanclar is not None:
            self._al_reanclar(
                Event(
                    analyzer=ANALIZADOR_FUENTE,
                    name=EVENTO_REANCLAJE,
                    timestamp=ancla,
                    attributes={
                        "reason": motivo,
                        "jump_s": round(salto_s, 3),
                        "ahead_s": round(adelanto_s, 3),
                    },
                )
            )

    def medir_retraso(self, pts_s: float | None, marca: datetime) -> float:
        """Cuanto se separa el fotograma que sale del borde en vivo, en segundos.

        Positivo es por detras. **No es la deriva de la etiqueta** (`ahora - marca`), que
        es el reloj que hemos derivado nosotros y se borra al re-anclar: es cuanto menos ha
        avanzado el stream (su PTS, `CAP_PROP_POS_MSEC`) que el reloj de pared desde que se
        abrio la captura. Medido contra un HLS en directo con un consumidor lento, es lo
        que cuenta lo que se acumula dentro del decodificador y la etiqueta no ve.

        Es relativo a la apertura de la captura: OpenCV no expone el borde del directo, asi
        que la latencia propia del HLS (varios segmentos) queda fuera. Sin PTS valido
        -webcam, backend que devuelve 0- se cae a la deriva de la etiqueta mas lo que los
        re-anclajes por deriva le han absorbido, para que re-anclar no la borre.
        """
        ahora = self._clock()
        valido = (
            pts_s is not None
            and math.isfinite(pts_s)
            and pts_s > 0
            and (self._ultimo_pts is None or pts_s >= self._ultimo_pts)
        )
        if valido:
            assert pts_s is not None
            self._ultimo_pts = pts_s
            if self._base_pts is None or self._base_reloj is None:
                self._base_pts, self._base_reloj = pts_s, ahora
            retraso = (ahora - self._base_reloj).total_seconds() - (pts_s - self._base_pts)
        else:
            retraso = (ahora - marca).total_seconds() + self._absorbido_s
        # Solo el retraso avisa aqui; el adelanto ya tiene su propio aviso periodico en
        # `_vigilar_deriva`, y repetirlo con otro nombre solo duplicaria la linea.
        if retraso > self.max_drift_s:
            self._avisar_cada_tanto(
                "retraso",
                "%s: el fotograma que se analiza va %.0f s por detras del borde en vivo "
                "(mas de los %.0f s de margen): el decodificador no sostiene el ritmo "
                "del directo",
                self._nombre,
                retraso,
                self.max_drift_s,
            )
        return retraso

    def _avisar_cada_tanto(self, clave: str, texto: str, *args: object) -> None:
        """WARNING como mucho una vez cada `AVISO_CADA_S`, y la primera vez en cuanto toca."""
        ahora = self._clock()
        previo = self._avisos.get(clave)
        if previo is not None and (ahora - previo).total_seconds() < AVISO_CADA_S:
            return
        self._avisos[clave] = ahora
        _log.warning(texto, *args)

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
        assert self._ancla is not None
        marca = self._ancla + timedelta(seconds=n / self.native_fps)
        if self._proximo is not None and marca < self._proximo:
            return None
        marca = self._vigilar_deriva(marca)
        self._ultimo = marca
        self._seleccionados += 1
        # El objetivo se acumula, no se recoloca sobre el fotograma que ha salido: es lo
        # que mantiene la tasa exacta cuando `target_fps` no divide al nativo (7 sobre 30
        # da huecos de 4 y 5 fotogramas, no de 5 siempre). Y si se ha quedado atras -un
        # hueco del transporte, o mas fps pedidos de los que trae el video- se recoloca,
        # que es lo que evita una rafaga de recuperacion despues de un paron.
        self._proximo = (self._proximo or marca) + self._periodo
        if self._proximo <= marca:
            self._proximo = marca + self._periodo
        return marca

    @property
    def tasa_efectiva(self) -> float:
        """Los fotogramas por segundo de video que se estan muestreando de verdad."""
        return self._tasa

    def realimentar(self, *, tomados: int, pendientes: int) -> None:
        """Le cuenta a la linea como le va al consumidor. Una vez por fotograma emitido.

        Sin esto, pedir 5 fps a una maquina que sostiene 3,7 no da 3,7 fps repartidos: da
        5 seleccionados, un buffer que se llena y un **bloque contiguo** descartado por
        ciclo de segmento, o sea 200 ms, 200 ms, 200 ms y de pronto 1,2 s. La mediana sale
        perfecta y la serie esta rota, que es el fallo silencioso que este issue existe
        para quitar. Muestreando a lo que el consumidor sostiene, los huecos salen
        regulares y no hay nada que descartar.

        Lo que se compara es **cuantos se ofrecieron y cuantos se consumieron** en la
        ventana, no si el buffer ha desbordado: para cuando desborda, el bloque ya se ha
        tirado y el hueco ya esta en la serie. Y no vale mirar cuanto ocupa el buffer,
        porque con un transporte a rafagas se llena por definicion cada segmento aunque
        el consumidor vaya sobrado.

        De `pendientes` -lo que hay esperando ahora mismo- lo que interesa es el **valle**
        de la ventana: el minimo, que es el retraso que no se va solo. El maximo no dice
        nada, porque con un transporte a rafagas el buffer se llena por definicion cada
        segmento aunque el consumidor vaya sobrado. Sin descontar el valle, cada tanteo
        hacia arriba deja unos fotogramas de mas esperando y el retraso crece hasta
        desbordar, que es volver al bloque descartado de golpe por otro camino.

        El valle se mide **por ventana**, y por eso se reinicia al abrir cada una. Si solo
        pudiera decrecer, en cuanto el buffer se vaciara una vez el drenaje quedaria muerto
        para el resto de la ejecucion y nada lo diria: justo el fallo de larga duracion que
        no se ve mirando la pantalla.

        La medida es en **tiempo de reloj** y no de video a proposito: lo que se estima es
        la capacidad del consumidor, y esa no cambia porque el transporte entregue de
        golpe 30 s de video al arrancar.
        """
        ahora = self._clock()
        if tomados:
            self._ha_arrancado = True
        if self._inicio_de_ventana is None:
            self._abrir_ventana(ahora, tomados, pendientes)
            return
        self._valle = min(self._valle, pendientes)
        transcurrido = (ahora - self._inicio_de_ventana).total_seconds()
        if transcurrido < VENTANA_DE_AJUSTE_S:
            return
        consumidos = tomados - self._tomados_al_abrir
        ofrecidos = self._seleccionados - self._seleccionados_al_abrir
        # Una ventana sin consumo antes de que el consumidor haya empezado no cuenta: es
        # la del arranque, mientras el detector carga los pesos. Una despues si cuenta, y
        # mucho: es un analisis atascado, y seguir muestreando a tope solo gasta CPU en
        # fotogramas que se van a tirar.
        if consumidos > 0 or self._ha_arrancado:
            self._ajustar(
                consumidos / transcurrido,
                valle=self._valle,
                transcurrido=transcurrido,
                sostiene=consumidos >= ofrecidos * HOLGURA_DE_CONSUMO,
            )
        self._abrir_ventana(ahora, tomados, pendientes)

    def _abrir_ventana(self, ahora: datetime, tomados: int, pendientes: int) -> None:
        self._inicio_de_ventana = ahora
        self._tomados_al_abrir = tomados
        self._seleccionados_al_abrir = self._seleccionados
        self._valle = pendientes

    def _ajustar(
        self, sostenido: float, *, valle: int, transcurrido: float, sostiene: bool
    ) -> None:
        """Baja el muestreo a lo que el consumidor aguanta, y lo sube a tientas si sobra."""
        suelo = self.target_fps * SUELO_DEL_MUESTREO
        # Si el consumidor se lleva lo que se le ofrece, se tantea hacia arriba; si no, se
        # apunta a la capacidad que ha demostrado. En los dos casos se descuenta lo que se
        # haya quedado esperando, porque un retraso que no se drena acaba desbordando el
        # buffer y volviendo al bloque descartado de golpe. Y en los dos casos hay tope a
        # lo que se puede bajar de una vez: el p95 del detector es cuatro veces su mediana,
        # asi que una ventana mala no dice lo que la maquina sostiene.
        objetivo = self._tasa * SUBIDA_POR_VENTANA if sostiene else sostenido
        drenaje = valle / (transcurrido * DRENAJE_EN_VENTANAS)
        propuesta = max(objetivo - drenaje, BAJADA_MAXIMA * self._tasa)
        nueva = min(max(propuesta, suelo), self.target_fps)
        if abs(nueva - self._tasa) < 0.01 * self.target_fps:
            return
        _log.info(
            "%s: el analisis sostiene %.2f fps, asi que se muestrea a %.2f en vez de a "
            "%.2f (pedidos %.2f). Ajustar la tasa reparte los huecos en vez de tirar "
            "bloques enteros cuando el buffer se llena",
            self._nombre,
            sostenido,
            nueva,
            self._tasa,
            self.target_fps,
        )
        self._tasa = nueva
        self._periodo = timedelta(seconds=1.0 / nueva)

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
                "de margen; se re-ancla, asi que habra un salto en la serie temporal (queda "
                "como evento %s)",
                self._nombre,
                deriva,
                self.max_drift_s,
                EVENTO_REANCLAJE,
            )
            self._absorbido_s += deriva
            self.anclar("deriva", captura_nueva=False)
            # Este fotograma es ya el primero de la epoca nueva.
            self._decodificados = 1
            assert self._ancla is not None
            return self._ancla
        if -deriva > self.max_drift_s:
            # No se re-ancla hacia atras (ver `anclar`). La causa habitual es que el
            # decodificador vacie de golpe el buffer del transporte al arrancar, y entonces
            # el desfase se queda quieto y es indistinguible de la latencia que ya trae el
            # propio directo. Se repite cada minuto: un aviso unico para algo que dura
            # horas dejaba, a las dos horas, sin saber si eran dos segundos o una hora.
            self._avisar_cada_tanto(
                "adelanto",
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

    def __init__(self, ventana_s: float) -> None:
        if ventana_s <= 0:
            raise ValueError(f"la ventana del buffer debe ser > 0 s, no {ventana_s!r}")
        self._condicion = threading.Condition()
        self._cola: deque[Snapshot] = deque()
        self._ventana = timedelta(seconds=ventana_s)
        self._descartados = 0
        self._tomados = 0
        self._error: SourceError | None = None
        self._cerrada = False

    @property
    def ventana_s(self) -> float:
        """Cuanto video puede haber esperando entre el mas viejo y el mas nuevo."""
        return self._ventana.total_seconds()

    @property
    def tomados(self) -> int:
        """Cuantos fotogramas ha consumido el analisis. Es la medida de si da abasto."""
        with self._condicion:
            return self._tomados

    @property
    def descartados(self) -> int:
        """Cuantos fotogramas seleccionados se han tirado por no caber."""
        with self._condicion:
            return self._descartados

    def __len__(self) -> int:
        with self._condicion:
            return len(self._cola)

    def publicar(
        self,
        imagen: np.ndarray,
        marca: datetime,
        *,
        leido_a: datetime | None = None,
        retraso_s: float = 0.0,
    ) -> bool:
        """Encola un fotograma. Devuelve `True` si ha habido que descartar el mas antiguo.

        El tope es **tiempo de video**, no un numero de fotogramas: lo que hay que acotar
        es cuanto puede ir el analisis por detras del directo, y eso solo coincide con un
        numero de fotogramas mientras la separacion entre ellos sea la que se supone. Con
        `native_fps` por debajo de `target_fps` deja de serlo, y un tope de 20 fotogramas
        que se creia de 4 s pasaba a ser de 19 s sin que nada lo dijera.
        """
        with self._condicion:
            if self._cerrada:
                return False
            self._cola.append(Snapshot(imagen, marca, leido_a, retraso_s))
            descarto = False
            while len(self._cola) > 1 and self._cola[-1].timestamp - self._cola[0].timestamp > (
                self._ventana
            ):
                self._cola.popleft()
                self._descartados += 1
                descarto = True
            self._condicion.notify_all()
            return descarto

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
                self._tomados += 1
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
        # Lo que la fuente cuenta de si misma (#24, #26). Lo escribe el hilo lector o el
        # consumidor, y lo lee el runner: enteros y referencias, asi que bastan las
        # asignaciones atomicas de Python; los eventos viajan por una `deque`.
        #
        # `reconnections` cuenta las caidas **de las que la fuente se ha recuperado**, no
        # los intentos de abrir al arrancar (eso es `RunStats.startup_retries`) ni las
        # renovaciones programadas de la URL, que no son una caida.
        self.reconnections = 0
        self.last_reconnection_at: datetime | None = None
        self.reanchors = 0
        self.max_latency_s = 0.0
        self.internal_buffer_bounded: bool | None = None
        self._pendientes: deque[Event] = deque(maxlen=EVENTOS_PENDIENTES_MAX)
        self._latencia_s: float | None = None
        self._marca_de_latencia: datetime | None = None
        self._ultima_metrica: datetime | None = None

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
            al_reanclar=self._registrar_reanclaje,
        )
        linea.anclar("apertura")
        self._acotar_buffer_interno(captura)
        buffer = RecentFrameBuffer(self.max_backlog_s)
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

    def drain_output(self) -> AnalyzerOutput:
        """Lo que la fuente tiene que contar desde la ultima vez: re-anclajes y latencia.

        Es un gancho **opcional** que el runner usa si existe (`getattr`): `VideoSource`
        no lo exige, porque un fichero no tiene reconexiones ni retraso que contar. Los
        eventos son los re-anclajes pendientes; la metrica es la latencia del ultimo
        fotograma entregado, como mucho una vez cada `METRICA_CADA_S`.
        """
        eventos = []
        while self._pendientes:
            try:
                eventos.append(self._pendientes.popleft())
            except IndexError:  # pragma: no cover - solo si dos hilos drenan a la vez
                break
        metricas = []
        ahora = self._clock()
        if (
            self._latencia_s is not None
            and self._marca_de_latencia is not None
            and (
                self._ultima_metrica is None
                or (ahora - self._ultima_metrica).total_seconds() >= METRICA_CADA_S
            )
        ):
            self._ultima_metrica = ahora
            metricas.append(
                Metric(
                    analyzer=ANALIZADOR_FUENTE,
                    name=METRICA_LATENCIA,
                    value=self._latencia_s,
                    timestamp=self._marca_de_latencia,
                )
            )
        return AnalyzerOutput(events=eventos, metrics=metricas)

    def _registrar_reanclaje(self, evento: Event) -> None:
        self.reanchors += 1
        self._pendientes.append(evento)

    def _acotar_buffer_interno(self, captura: Any) -> None:
        """Intenta que el decodificador no acumule fotogramas, y **comprueba** que ha colado.

        `CAP_PROP_BUFFERSIZE` es lo que OpenCV ofrece, pero no todos los backends lo
        honran. Medido en #26 contra un HLS en directo con el backend FFMPEG (OpenCV 5.0):
        `set()` devuelve `False`, `get()` devuelve -1 y un consumidor lento acumula el mismo
        retraso con la propiedad puesta que sin ella. Por eso no se da por hecho: se
        pregunta, se guarda el resultado en `internal_buffer_bounded` y, si no hay tope, se
        dice una vez. Lo que acota el retraso entonces no es esto sino lo que se mide
        (`medir_retraso`) y se avisa.
        """
        acotado = bool(captura.set(cv2.CAP_PROP_BUFFERSIZE, 1)) and (
            captura.get(cv2.CAP_PROP_BUFFERSIZE) == 1
        )
        if self.internal_buffer_bounded is None and not acotado:
            _log.warning(
                "%s: el decodificador no admite acotar su buffer interno "
                "(CAP_PROP_BUFFERSIZE); si no sostiene el ritmo del directo el retraso se "
                "acumulara ahi dentro. No se puede evitar: se mide y se avisa",
                self._descripcion(),
            )
        self.internal_buffer_bounded = acotado

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
                        self._acotar_buffer_interno(captura)
                        self.reconnections += 1
                        self.last_reconnection_at = self._clock()
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
                        linea.anclar("renovacion")
                        self._acotar_buffer_interno(captura)
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
                # El PTS lo da el decodificador, no nosotros: es lo que permite ver el
                # retraso que la etiqueta `marca` esconde (ver `medir_retraso`).
                retraso = linea.medir_retraso(captura.get(cv2.CAP_PROP_POS_MSEC) / 1000.0, marca)
                publicado = buffer.publicar(imagen, marca, leido_a=self._clock(), retraso_s=retraso)
                if publicado and not aviso_de_descartes:
                    aviso_de_descartes = True
                    _log.info(
                        "%s: el analisis no sostiene los %.1f fps pedidos, asi que se "
                        "descartan los fotogramas mas antiguos del buffer (%.1f s de "
                        "video). El retraso se queda ahi y no crece",
                        self._descripcion(),
                        self.target_fps,
                        self.max_backlog_s,
                    )
                # Y con eso se ajusta el muestreo: si el consumidor no da abasto, mas vale
                # seleccionar menos y repartido que seleccionar de mas y tirar bloques.
                linea.realimentar(tomados=buffer.tomados, pendientes=len(buffer))
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
            if instantanea.leido_a is not None:
                # Latencia **del fotograma que se va a analizar**: la que traia del
                # decodificador mas lo que ha esperado en nuestro buffer.
                espera = (self._clock() - instantanea.leido_a).total_seconds()
                self._latencia_s = instantanea.retraso_s + espera
                self._marca_de_latencia = instantanea.timestamp
                self.max_latency_s = max(self.max_latency_s, self._latencia_s)
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
