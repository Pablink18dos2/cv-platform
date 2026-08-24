"""Connector del directo de YouTube: yt-dlp resuelve la URL HLS y OpenCV la decodifica.

Dos hallazgos del reconocimiento de la fuente real (`projects/01-parking-trafico/fuente.md`)
mandan sobre todo lo que hay aqui:

1. **El cliente por defecto de yt-dlp falla.** Sin argumentos devuelve `This video is not
   available`, que suena a video borrado y no lo es. Con
   `youtube:player_client=web_safari,tv,android` resuelve sin problema.
2. **La URL HLS caduca a las 6 h exactas.** No es una estimacion: es el parametro `expire`
   del manifiesto. Por eso se re-resuelve **proactivamente** cada `refresh_url_every_s`
   ademas de reactivamente cuando la lectura falla; esperar a que falle ya es tarde y deja
   un hueco de datos cada seis horas.

Todo lo demas —el hilo lector, el paceo por reloj de pared, el backoff— es de
`LiveVideoSource`, porque RTSP y webcam tienen el mismo problema.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import cv2

from cvp.core.registry import SOURCES
from cvp.sources.errors import SourceError
from cvp.sources.live import MAX_BACKLOG_S, MAX_DERIVA_S, LiveVideoSource

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import YouTubeLiveSource as YouTubeLiveSourceConfig

__all__ = ["YouTubeLiveSource", "resolver_con_ytdlp"]

_log = logging.getLogger(__name__)

# Que espera OpenCV a que el servidor conteste antes de rendirse. Sin limite, una lectura
# colgada dejaria al hilo lector mudo para siempre sin que nada lo delatara.
TIEMPO_DE_ESPERA_MS = 15000

# Cuantos hilos se le dan a ffmpeg para descodificar. Por defecto coge uno por nucleo, y
# en esta maquina (2 P-cores y 8 E-cores de 15 W) eso significa que el decodificador y el
# detector se pelean por los mismos dos nucleos rapidos durante todo el rato: el detector
# tenia p50 196 ms y **media 302 ms**, y es la media la que manda en los fps.
#
# Dos es suficiente con holgura: medido sobre un directo 1080p30, ffmpeg descodifica a
# 42 fps con dos hilos frente a 49 fps sin limite, y solo hacen falta 30. Se cambia el
# reparto de un recurso escaso por un margen de decodificacion que sigue sobrando.
HILOS_DE_DECODIFICACION = 2


def clientes_que_funcionan() -> dict[str, dict[str, list[str]]]:
    """Los `extractor_args` con los que este directo resuelve de verdad.

    Duplica el valor por defecto de `cvp.pipeline.config`, y es a proposito: `cvp.sources`
    no importa `cvp.pipeline` (`cvp validate` no puede pagar el coste de importar yt-dlp y
    OpenCV). Si YouTube rompe estos clientes hay que cambiarlo en los dos sitios, y el
    sintoma sera el mensaje enganoso `This video is not available`.
    """
    return {"youtube": {"player_client": ["web_safari", "tv", "android"]}}


def _cargar_ydl() -> Any:
    """Importa `YoutubeDL` en el momento de usarlo.

    Importar yt-dlp cuesta bastante mas que registrar un connector, y `cvp.sources` se
    importa entero solo para resolver un `type:`. Ademas es la costura por la que los
    tests meten un doble sin tocar la red.
    """
    from yt_dlp import YoutubeDL

    return YoutubeDL


def resolver_con_ytdlp(
    url: str,
    *,
    formato: str,
    extractor_args: Mapping[str, Mapping[str, Sequence[str]]],
    ydl_class: Any | None = None,
) -> str:
    """Devuelve la URL directa del directo `url`, resuelta con yt-dlp.

    `extractor_args` y `formato` vienen de la configuracion del proyecto y llegan tal cual
    a yt-dlp: son los que deciden si este directo concreto resuelve o contesta
    `This video is not available`.
    """
    clase = ydl_class if ydl_class is not None else _cargar_ydl()
    opciones = {
        "format": formato,
        "extractor_args": dict(extractor_args),
        # El connector no descarga nada: solo quiere la URL del manifiesto.
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with clase(opciones) as ydl:
        info = ydl.extract_info(url, download=False)
    return _url_directa(info, url)


def _url_directa(info: object, url: str) -> str:
    """La URL directa que hay dentro de lo que devuelve yt-dlp.

    Los formatos del proyecto 01 son HLS muxados, asi que `info["url"]` basta; se mira
    tambien `requested_formats` para no romper con una fuente que separe video y audio.
    """
    if not isinstance(info, Mapping):
        raise SourceError(f"yt-dlp no ha devuelto informacion de {url}, sino {info!r}")
    directa = info.get("url")
    if directa:
        return str(directa)
    for formato in info.get("requested_formats") or ():
        if isinstance(formato, Mapping) and formato.get("url"):
            return str(formato["url"])
    raise SourceError(f"yt-dlp ha resuelto {url} pero no ha devuelto ninguna URL directa")


def parametros_de_captura() -> list[int]:
    """Los parametros con los que se abre la captura de verdad.

    Estan aparte para que un test pueda afirmar sobre ellos: los tres son decisiones
    medidas, no valores por defecto de OpenCV, y un cambio silencioso en cualquiera de
    los tres se paga en produccion sin que nada lo delate.
    """
    return [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
        TIEMPO_DE_ESPERA_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
        TIEMPO_DE_ESPERA_MS,
        cv2.CAP_PROP_N_THREADS,
        HILOS_DE_DECODIFICACION,
    ]


def _abrir_con_opencv(url: str) -> Any:
    """La captura de verdad. Es el valor por defecto y no lo usa ningun test."""
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, parametros_de_captura())


@SOURCES.register("youtube_live")
class YouTubeLiveSource(LiveVideoSource):
    """Un directo de YouTube, entregando siempre el fotograma mas reciente.

    Los cuatro argumentos inyectables (`resolver`, `capture_factory`, `clock`, `sleeper`)
    son lo que permite probar la caducidad, el backoff y el paceo sin red, sin OpenCV y
    sin esperar de verdad. Sus valores por defecto son los reales.
    """

    _ETIQUETA = "youtube_live"

    def __init__(
        self,
        url: str,
        target_fps: float,
        *,
        format: str = "best",  # noqa: A002 - el nombre lo fija el campo del project.yaml
        extractor_args: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
        refresh_url_every_s: int = 18000,
        max_retries: int | None = 20,
        backoff_s: Sequence[float] | None = None,
        max_backlog_s: float = MAX_BACKLOG_S,
        max_drift_s: float = MAX_DERIVA_S,
        resolver: Callable[[str], str] | None = None,
        capture_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], object] | None = None,
    ) -> None:
        super().__init__(
            target_fps,
            max_retries=max_retries,
            backoff_s=backoff_s,
            max_backlog_s=max_backlog_s,
            max_drift_s=max_drift_s,
            clock=clock,
            sleeper=sleeper,
        )
        if not url:
            raise ValueError("url no puede estar vacia")
        if refresh_url_every_s <= 0:
            raise ValueError(f"refresh_url_every_s debe ser > 0, no {refresh_url_every_s!r}")
        self.url = url
        self.format = format
        self.extractor_args = (
            clientes_que_funcionan() if extractor_args is None else dict(extractor_args)
        )
        self.refresh_url_every_s = int(refresh_url_every_s)
        self._resolver = resolver or partial(
            resolver_con_ytdlp, formato=self.format, extractor_args=self.extractor_args
        )
        self._crear_captura = capture_factory or _abrir_con_opencv
        self._resuelta_en: datetime | None = None

    @classmethod
    def from_config(
        cls,
        cfg: YouTubeLiveSourceConfig,
        *,
        resolver: Callable[[str], str] | None = None,
        capture_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], object] | None = None,
    ) -> YouTubeLiveSource:
        """Construye desde el modelo de configuracion de #3. Lo usara el runner de #8."""
        return cls(
            cfg.url,
            cfg.target_fps,
            format=cfg.format,
            extractor_args=cfg.extractor_args,
            refresh_url_every_s=cfg.refresh_url_every_s,
            max_retries=cfg.reconnect.max_retries,
            backoff_s=list(cfg.reconnect.backoff_s),
            resolver=resolver,
            capture_factory=capture_factory,
            clock=clock,
            sleeper=sleeper,
        )

    # --- Ganchos de `LiveVideoSource` -------------------------------------------------

    def _descripcion(self) -> str:
        return self.url

    def _abrir_captura(self) -> Any:
        directa = self._resolver_url()
        captura = self._crear_captura(directa)
        if not captura.isOpened():
            self._soltar(captura)
            raise SourceError(f"OpenCV no ha podido abrir la URL HLS resuelta de {self.url}")
        return captura

    def _resolver_url(self) -> str:
        """Resuelve la URL directa y apunta cuando, que es lo que caduca a las 6 h."""
        _log.info("resolviendo la URL HLS de %s con yt-dlp (format=%s)", self.url, self.format)
        try:
            directa = self._resolver(self.url)
        except SourceError:
            raise
        except Exception as exc:
            # Ninguna excepcion de yt-dlp escapa hacia arriba: quien llama solo conoce
            # `SourceError`, y la causa queda encadenada para no perder el diagnostico.
            raise SourceError(f"yt-dlp no ha podido resolver {self.url}: {exc}") from exc
        if not directa:
            raise SourceError(f"el resolvedor no ha devuelto ninguna URL directa para {self.url}")
        self._resuelta_en = self._clock()
        return str(directa)

    def _toca_renovar(self) -> bool:
        if self._resuelta_en is None:  # pragma: no cover - `open()` siempre resuelve antes
            return False
        return self._clock() - self._resuelta_en >= timedelta(seconds=self.refresh_url_every_s)

    def _motivo_de_renovacion(self) -> str:
        return (
            f"la URL HLS lleva {self.refresh_url_every_s} s resuelta y YouTube la caduca "
            "a las 6 h exactas"
        )
