"""El connector de directo de YouTube: hilo lector, caducidad de la URL y reconexion.

Lo que este connector tiene que demostrar no es que lea fotogramas, sino que **no se
queda atras**. Con un fichero, muestrear contando fotogramas de origen es correcto porque
el fichero espera; con un directo, cada fotograma que se encola es retraso permanente. Por
eso casi todos los tests de aqui miran lo mismo desde angulos distintos: que lo que sale
es lo mas fresco que hay, no lo siguiente de una cola.

Desde #23 el muestreo lo decide el hilo lector **en tiempo de video** (`VideoTimeline`) y
lo que llega al consumidor pasa por un buffer acotado, asi que aqui se prueba sobre todo
una cosa: que la secuencia que sale es la misma llegue el transporte como llegue.

Ningun test sale a la red ni duerme de verdad. El reloj y el `sleeper` son inyectables, y
la sincronizacion con el hilo lector se hace con semaforos y eventos, no con esperas: si
un test tardara segundos, estaria mal escrito.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from cvp.core import Frame, SourceInfo
from cvp.core.protocols import VideoSource
from cvp.core.registry import SOURCES
from cvp.sources import SourceError, YouTubeLiveSource
from cvp.sources import youtube_live as modulo
from cvp.sources.youtube_live import clientes_que_funcionan, resolver_con_ytdlp

URL = "https://www.youtube.com/watch?v=EPKWu223XEg"
DIRECTA = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/1755000000/x.m3u8"
INICIO = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
VALIDO = Path(__file__).resolve().parents[1] / "fixtures" / "config" / "valido.yaml"

# Tope de seguridad de las esperas sobre eventos y `join`. Si el codigo es correcto se
# cumplen en microsegundos; si no lo es, el test falla en vez de colgar la suite.
ESPERA_MAXIMA_S = 2.0

# Los clientes sin los cuales este directo concreto responde "This video is not
# available", que suena a video borrado y no lo es. Se escriben literales a proposito:
# un test que los leyera del codigo no comprobaria nada.
CLIENTES = {"youtube": {"player_client": ["web_safari", "tv", "android"]}}

# La captura falsa declara 30 fps, asi que con `target_fps=5` sale uno de cada seis.
NATIVO = 30.0
MUESTREO = 5.0
CADA = int(NATIVO / MUESTREO)


# --- Dobles ---------------------------------------------------------------------------


class RelojFalso:
    """El reloj y el `sleeper` inyectados. Dormir aqui es adelantar el reloj, no esperar.

    `paso` es lo que avanza en cada consulta: con `0` queda congelado, que es lo que
    permite afirmar instantes exactos.
    """

    def __init__(self, inicio: datetime = INICIO, paso: timedelta = timedelta(0)) -> None:
        self._ahora = inicio
        self._paso = paso
        self._lock = threading.Lock()
        self.esperas: list[float] = []

    def __call__(self) -> datetime:
        with self._lock:
            valor = self._ahora
            self._ahora += self._paso
            return valor

    def dormir(self, segundos: float) -> None:
        self.esperas.append(round(float(segundos), 6))
        self.avanzar(segundos)

    def avanzar(self, segundos: float) -> None:
        with self._lock:
            self._ahora += timedelta(seconds=segundos)

    @property
    def ahora(self) -> datetime:
        with self._lock:
            return self._ahora


def _imagen(numero: int) -> np.ndarray:
    """Un fotograma minusculo que lleva escrito su numero de orden."""
    imagen = np.zeros((4, 4, 3), dtype=np.uint8)
    imagen[0, 0, 0] = numero & 0xFF
    imagen[0, 0, 1] = (numero >> 8) & 0xFF
    imagen[0, 0, 2] = (numero >> 16) & 0xFF
    return imagen


def numero_de(fotograma: Frame) -> int:
    """El numero de origen del fotograma, para ver que fotogramas se han descartado."""
    pixel = fotograma.image[0, 0]
    return int(pixel[0]) | (int(pixel[1]) << 8) | (int(pixel[2]) << 16)


class CapturaFalsa:
    """Doble de `cv2.VideoCapture` que entrega fotogramas numerados sin parar.

    `total` es cuantos entrega antes de darse por muerta, que es como un directo se cae:
    `read()` devuelve `(False, None)`, no lanza. `None` es no morirse nunca.

    Las dimensiones que reporta son independientes del tamano del array a proposito: el
    hilo lector corre a tope y allocar 1080p en cada lectura no aportaria nada al test.
    """

    def __init__(
        self,
        *,
        total: int | None = None,
        ancho: int = 1920,
        alto: int = 1080,
        fps: float = 30.0,
    ) -> None:
        self.total = total
        self.entregados = 0
        self.liberaciones = 0
        self._ultima: np.ndarray | None = None
        self._ancho = ancho
        self._alto = alto
        self._fps = fps

    def isOpened(self) -> bool:  # noqa: N802 - la firma la impone OpenCV
        return True

    def get(self, prop: int) -> float:
        import cv2

        return {
            cv2.CAP_PROP_FRAME_WIDTH: float(self._ancho),
            cv2.CAP_PROP_FRAME_HEIGHT: float(self._alto),
            cv2.CAP_PROP_FPS: float(self._fps),
        }[prop]

    def grab(self) -> bool:
        """Descodifica el siguiente, sin convertirlo. Es lo que el connector llama siempre."""
        if self.total is not None and self.entregados >= self.total:
            self._ultima = None
            return False
        self.entregados += 1
        self._ultima = _imagen(self.entregados)
        return True

    def retrieve(self) -> tuple[bool, np.ndarray | None]:
        """Materializa el ultimo descodificado. Solo se llama para los que se emiten."""
        return self._ultima is not None, self._ultima

    def release(self) -> None:
        self.liberaciones += 1

    def cerrar(self) -> None:
        """No bloquea nunca, asi que no hay nada que desbloquear antes de `close()`."""


class CapturaGobernada(CapturaFalsa):
    """Doble que entrega **un fotograma por cada permiso** que suelte el test.

    `producir(n)` no vuelve hasta que el hilo lector ha **procesado** los n, de modo que el
    test puede afirmar numeros exactos en vez de tolerancias. El truco para saber que el
    ultimo ya esta muestreado es que el hilo lector decide y encola antes de volver a leer:
    la senal se emite al principio de la lectura siguiente.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._permisos = threading.Semaphore(0)
        self._procesados = threading.Semaphore(0)
        self._pendiente = False
        self._cerrada = threading.Event()

    def producir(self, cuantos: int = 1) -> None:
        for _ in range(cuantos):
            self._permisos.release()
        for _ in range(cuantos):
            assert self._procesados.acquire(timeout=ESPERA_MAXIMA_S), (
                "el hilo lector no ha consumido los fotogramas producidos"
            )

    def grab(self) -> bool:
        if self._pendiente:
            self._pendiente = False
            self._procesados.release()
        while not self._cerrada.is_set():
            if self._permisos.acquire(timeout=0.01):
                self.entregados += 1
                self._pendiente = True
                self._ultima = _imagen(self.entregados)
                return True
        self._ultima = None
        return False

    def cerrar(self) -> None:
        """Desbloquea al hilo lector. El test lo llama antes de `close()`."""
        self._cerrada.set()


class Fabrica:
    """La `capture_factory` inyectada. Sigue un guion y repite su ultimo paso.

    Cada paso es una captura, una excepcion (la captura no se pudo abrir) o un invocable
    que fabrica una captura nueva, que es como se escribe "y esta vez si funciona".
    """

    def __init__(self, *guion: object) -> None:
        self.guion = guion
        self.urls: list[str] = []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        paso = self.guion[min(len(self.urls), len(self.guion)) - 1]
        if isinstance(paso, BaseException):
            raise paso
        if callable(paso):
            return paso()
        return paso

    @property
    def llamadas(self) -> int:
        return len(self.urls)


class Resolvedor:
    """El `resolver` inyectado: cuenta llamadas y devuelve una URL distinta cada vez.

    `esperar` permite sincronizarse con la re-resolucion que hace el hilo lector sin
    dormir ni sondear.
    """

    def __init__(self) -> None:
        self._condicion = threading.Condition()
        self.llamadas = 0

    def __call__(self, url: str) -> str:
        with self._condicion:
            self.llamadas += 1
            numero = self.llamadas
            self._condicion.notify_all()
        return f"https://directo-{numero}.m3u8"

    def esperar(self, cuantas: int, timeout: float = ESPERA_MAXIMA_S) -> bool:
        with self._condicion:
            return self._condicion.wait_for(lambda: self.llamadas >= cuantas, timeout)


def doble_de_ytdlp(
    resultado: object = None, error: Exception | None = None
) -> tuple[type, dict[str, Any]]:
    """Doble de `YoutubeDL` que registra las opciones con las que se construye."""
    registro: dict[str, Any] = {}

    class Doble:
        def __init__(self, opciones: dict[str, Any]) -> None:
            registro["opciones"] = opciones

        def __enter__(self) -> Doble:
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = True) -> object:
            registro["url"] = url
            registro["download"] = download
            if error is not None:
                raise error
            return resultado

    return Doble, registro


def fuente(
    fabrica: Fabrica,
    *,
    target_fps: float = NATIVO,
    reloj: RelojFalso | None = None,
    resolvedor: Resolvedor | None = None,
    sleeper: Callable[[float], object] | None = None,
    **extra: Any,
) -> YouTubeLiveSource:
    """Una fuente con los cuatro puntos de inyeccion puestos: ni red, ni OpenCV, ni esperas.

    `target_fps` igual al nativo por defecto, para que salgan todos los fotogramas y el
    muestreo no estorbe en los tests que no van de muestreo; el que va de muestreo lo baja.

    El `sleeper` por defecto adelanta el reloj, que es lo que quiere casi todo el fichero.
    Lo cambia quien necesite un reloj que **no se mueva en absoluto**.
    """
    reloj = reloj if reloj is not None else RelojFalso()
    return YouTubeLiveSource(
        URL,
        target_fps,
        resolver=resolvedor if resolvedor is not None else Resolvedor(),
        capture_factory=fabrica,
        clock=reloj,
        sleeper=sleeper if sleeper is not None else reloj.dormir,
        **extra,
    )


@contextmanager
def abierta(origen: YouTubeLiveSource, captura: CapturaFalsa | None = None) -> Iterator[Frame]:
    """Abre la fuente y garantiza el cierre, tambien si el test falla a media iteracion."""
    origen.open()
    try:
        yield origen
    finally:
        if captura is not None:
            captura.cerrar()
        origen.close()


def hilos_del_connector() -> list[threading.Thread]:
    return [hilo for hilo in threading.enumerate() if hilo.name.startswith("cvp-")]


@pytest.fixture(autouse=True)
def sin_hilos_colgando() -> Iterator[None]:
    """Ningun test puede dejar un hilo lector vivo para el siguiente."""
    assert hilos_del_connector() == [], "un test anterior ha dejado un hilo lector vivo"
    yield
    assert hilos_del_connector() == [], "este test ha dejado un hilo lector vivo"


# --- Contrato y registro --------------------------------------------------------------


def test_cumple_el_protocolo_video_source() -> None:
    assert isinstance(YouTubeLiveSource(URL, target_fps=5), VideoSource)


def test_esta_registrada_como_youtube_live() -> None:
    assert SOURCES.get("youtube_live") is YouTubeLiveSource


def test_los_valores_por_defecto_son_los_reales() -> None:
    # Construir el connector a mano tiene que dar lo mismo que el project.yaml por
    # defecto: si divergen, un test verde no dice nada del worker que corre de verdad.
    origen = YouTubeLiveSource(URL, target_fps=5)
    assert origen.format == "best"
    assert origen.extractor_args == CLIENTES
    assert origen.refresh_url_every_s == 18000
    assert origen.max_retries == 20
    assert origen.backoff_s == [1.0, 2.0, 5.0, 15.0, 30.0, 60.0]
    # De #23: cuanto video puede esperar por delante del analisis, y cuanto puede
    # descolgarse el tiempo de video del reloj antes de re-anclar.
    assert origen.max_backlog_s == 4.0
    assert origen.max_drift_s == 30.0
    assert origen.profundidad_del_buffer == 20


@pytest.mark.parametrize(
    ("kwargs", "mensaje"),
    [
        ({"target_fps": 0}, "target_fps"),
        ({"target_fps": 5, "max_retries": -1}, "max_retries"),
        ({"target_fps": 5, "backoff_s": []}, "backoff_s"),
        ({"target_fps": 5, "backoff_s": [1, 0]}, "backoff_s"),
        ({"target_fps": 5, "refresh_url_every_s": 0}, "refresh_url_every_s"),
        ({"target_fps": 5, "max_backlog_s": 0}, "max_backlog_s"),
        ({"target_fps": 5, "max_drift_s": 0}, "max_drift_s"),
    ],
)
def test_una_configuracion_imposible_falla_al_construir(
    kwargs: dict[str, Any], mensaje: str
) -> None:
    with pytest.raises(ValueError, match=mensaje):
        YouTubeLiveSource(URL, **kwargs)


def test_from_config_construye_desde_el_project_yaml() -> None:
    from cvp.pipeline import load_project_config

    cfg = load_project_config(VALIDO).source
    origen = YouTubeLiveSource.from_config(cfg)
    assert origen.url == URL
    assert origen.target_fps == 5
    assert origen.format == "96"
    assert origen.extractor_args == CLIENTES
    assert origen.refresh_url_every_s == 18000
    # `reconnect` se mapea a los dos argumentos planos del connector.
    assert origen.max_retries == 20
    assert origen.backoff_s == [1.0, 2.0, 5.0, 15.0, 30.0, 60.0]


# --- Ciclo de vida e `info` -----------------------------------------------------------


def test_pedir_info_antes_de_abrir_falla() -> None:
    with pytest.raises(SourceError, match="open()"):
        _ = YouTubeLiveSource(URL, target_fps=5).info


def test_leer_fotogramas_antes_de_abrir_falla() -> None:
    with pytest.raises(SourceError, match="open()"):
        YouTubeLiveSource(URL, target_fps=5).frames()


def test_info_declara_directo_y_las_dimensiones_que_reporta_la_captura() -> None:
    captura = CapturaFalsa(ancho=1280, alto=720, fps=30.0)
    with abierta(fuente(Fabrica(captura)), captura) as origen:
        assert origen.info == SourceInfo(width=1280, height=720, native_fps=30.0, is_live=True)


def test_un_directo_que_no_declara_frame_rate_se_abre_igual() -> None:
    # A diferencia del connector de fichero, aqui el frame rate no hace falta: el muestreo
    # se pacea contra el reloj de pared. HLS no siempre lo declara.
    captura = CapturaFalsa(fps=0.0)
    with abierta(fuente(Fabrica(captura)), captura) as origen:
        assert origen.info.native_fps is None


def test_una_captura_sin_dimensiones_falla_al_abrir() -> None:
    with pytest.raises(SourceError, match="dimensiones"):
        fuente(Fabrica(CapturaFalsa(ancho=0))).open()


def test_cerrar_es_idempotente_y_no_exige_haber_abierto() -> None:
    captura = CapturaFalsa()
    origen = fuente(Fabrica(captura))
    origen.close()
    origen.open()
    origen.close()
    origen.close()
    assert captura.liberaciones == 1


def test_abrir_dos_veces_no_arranca_un_segundo_hilo() -> None:
    captura = CapturaFalsa()
    with abierta(fuente(Fabrica(captura)), captura) as origen:
        origen.open()
        assert len(hilos_del_connector()) == 1


# --- El hilo lector: entregar siempre lo mas fresco -----------------------------------


def test_open_arranca_un_hilo_lector_daemon() -> None:
    captura = CapturaFalsa()
    with abierta(fuente(Fabrica(captura)), captura):
        hilos = hilos_del_connector()
        assert [hilo.name for hilo in hilos] == ["cvp-youtube_live-lector"]
        assert all(hilo.daemon for hilo in hilos)


def test_un_consumidor_lento_descarta_lo_viejo_y_su_retraso_no_pasa_del_buffer() -> None:
    # La propiedad buena de #5, que esta reescritura no puede perder: quien va lento se
    # salta fotogramas, no acumula retraso. Con `max_backlog_s=0.6` caben tres
    # seleccionados, asi que el analisis no puede ir mas de 0,6 s de video por detras del
    # directo por lento que vaya. Con una cola sin fondo recibiria 1, 7, 13... y llegaria
    # a los ultimos con seis segundos de retraso.
    captura = CapturaGobernada()
    origen = fuente(Fabrica(captura), target_fps=MUESTREO, max_backlog_s=0.6)
    with abierta(origen, captura):
        fotogramas = origen.frames()
        # 180 fotogramas son 6 s de video: 30 seleccionados para tres huecos.
        captura.producir(180)
        recibidos = [_siguiente(fotogramas) for _ in range(3)]

    # Los tres ultimos seleccionados, no los tres primeros: los indices de origen saltan.
    assert [numero_de(f) for f in recibidos] == [163, 169, 175]
    # Y el mas viejo que le llega esta a 0,4 s del ultimo que se selecciono, por debajo
    # de los 0,6 s de buffer. Ese es el techo del retraso, y no crece con el tiempo.
    ultimo_seleccionado = INICIO + timedelta(seconds=174 / NATIVO)
    assert ultimo_seleccionado - recibidos[0].timestamp == timedelta(seconds=0.4)
    assert [f.index for f in recibidos] == [0, 1, 2]


def test_no_repite_el_ultimo_fotograma_cuando_el_consumidor_va_mas_deprisa() -> None:
    captura = CapturaGobernada()
    with abierta(fuente(Fabrica(captura)), captura) as origen:
        fotogramas = origen.frames()
        captura.producir(1)
        assert numero_de(next(fotogramas)) == 1

        recibidos: list[int] = []
        pidiendo = threading.Thread(
            target=lambda: recibidos.append(numero_de(next(fotogramas))), daemon=True
        )
        pidiendo.start()
        pidiendo.join(0.05)
        assert pidiendo.is_alive(), "ha repetido el fotograma 1 en vez de esperar al siguiente"

        captura.producir(1)
        pidiendo.join(ESPERA_MAXIMA_S)
        assert recibidos == [2]


def _siguiente(fotogramas: Iterator[Frame]) -> Frame:
    """`next()` con tope de tiempo, para que un fotograma que no llega falle en vez de colgar.

    Sin esto, cualquier cambio que dejara al connector sin entregar lo que toca -por
    ejemplo volver al buffer de uno solo de #5- dejaria la suite colgada en vez de roja.
    """
    caja: list[Frame] = []
    pidiendo = threading.Thread(target=lambda: caja.append(next(fotogramas)), daemon=True)
    pidiendo.start()
    pidiendo.join(ESPERA_MAXIMA_S)
    assert caja, "el connector no ha entregado el fotograma que tocaba"
    return caja[0]


def _drenar(fotogramas, salida: list[Frame], decodificados: int, cada: int) -> None:
    """Saca del buffer los que ya tienen que estar, sabiendo cuantos se han decodificado.

    Cuantos salen no es una tolerancia: con `native_fps` conocido, el muestreo depende
    solo del numero de fotograma, asi que el test puede calcularlo.
    """
    while len(salida) < (decodificados - 1) // cada + 1:
        salida.append(_siguiente(fotogramas))


def _en_rafagas(
    captura: CapturaGobernada,
    origen: YouTubeLiveSource,
    reloj: RelojFalso,
    *,
    por_rafaga: int,
    rafagas: int,
    silencio_s: float,
) -> list[Frame]:
    """Un transporte a rafagas: un segmento entero de golpe y despues nada, en bucle."""
    fotogramas = origen.frames()
    salida: list[Frame] = []
    decodificados = 0
    for _ in range(rafagas):
        captura.producir(por_rafaga)
        decodificados += por_rafaga
        _drenar(fotogramas, salida, decodificados, CADA)
        reloj.avanzar(silencio_s)
    return salida


def _uniforme(
    captura: CapturaGobernada,
    origen: YouTubeLiveSource,
    reloj: RelojFalso,
    *,
    total: int,
    paso_s: float,
) -> list[Frame]:
    """El transporte que se porta: un fotograma cada 1/30 s, repartidos en el tiempo."""
    fotogramas = origen.frames()
    salida: list[Frame] = []
    for decodificados in range(1, total + 1):
        captura.producir(1)
        reloj.avanzar(paso_s)
        _drenar(fotogramas, salida, decodificados, CADA)
    return salida


def test_una_fuente_a_rafagas_y_una_uniforme_dan_exactamente_la_misma_secuencia() -> None:
    """El arreglo de #23, en un solo assert.

    Cuatro segmentos de 4 s entregados de golpe con 4 s de silencio detras, contra los
    mismos 480 fotogramas repartidos uno a uno. Antes, la primera daba 0,85 fps y
    timestamps comprimidos y la segunda iba bien; ahora la salida es identica, porque el
    muestreo se decide en tiempo de video y no contra el reloj.
    """
    captura_a = CapturaGobernada()
    reloj_a = RelojFalso()
    origen_a = fuente(Fabrica(captura_a), target_fps=MUESTREO, reloj=reloj_a, max_backlog_s=5.0)
    with abierta(origen_a, captura_a):
        a_rafagas = _en_rafagas(
            captura_a, origen_a, reloj_a, por_rafaga=120, rafagas=4, silencio_s=4.0
        )

    captura_b = CapturaGobernada()
    reloj_b = RelojFalso()
    origen_b = fuente(Fabrica(captura_b), target_fps=MUESTREO, reloj=reloj_b, max_backlog_s=5.0)
    with abierta(origen_b, captura_b):
        uniforme = _uniforme(captura_b, origen_b, reloj_b, total=480, paso_s=1 / NATIVO)

    assert [(numero_de(f), f.timestamp, f.index) for f in a_rafagas] == [
        (numero_de(f), f.timestamp, f.index) for f in uniforme
    ]


def test_sobre_una_fuente_a_rafagas_salen_cinco_fotogramas_por_segundo_de_video() -> None:
    # No los que quepan en la rafaga. 480 fotogramas decodificados son 16 s de video, y a
    # `target_fps: 5` eso son 80 fotogramas, lleguen como lleguen.
    captura = CapturaGobernada()
    reloj = RelojFalso()
    origen = fuente(Fabrica(captura), target_fps=MUESTREO, reloj=reloj, max_backlog_s=5.0)
    with abierta(origen, captura):
        emitidos = _en_rafagas(captura, origen, reloj, por_rafaga=120, rafagas=4, silencio_s=4.0)

    segundos_de_video = 480 / NATIVO
    assert len(emitidos) == 80
    assert len(emitidos) / segundos_de_video == MUESTREO
    # Uno de cada seis, sin repetir ninguno y sin reiniciar el indice.
    assert [numero_de(f) for f in emitidos] == [1 + CADA * n for n in range(80)]
    assert [f.index for f in emitidos] == list(range(80))


def test_los_timestamps_de_una_fuente_a_rafagas_van_separados_el_periodo() -> None:
    # Antes los fotogramas salian separados 200 ms de reloj y ~900 ms de video, con saltos
    # de 4 s entre rafagas. Cualquier agregacion por minuto construida encima salia
    # deformada, y no se veia en el dashboard.
    captura = CapturaGobernada()
    reloj = RelojFalso()
    origen = fuente(Fabrica(captura), target_fps=MUESTREO, reloj=reloj, max_backlog_s=5.0)
    with abierta(origen, captura):
        emitidos = _en_rafagas(captura, origen, reloj, por_rafaga=120, rafagas=4, silencio_s=4.0)

    marcas = [f.timestamp for f in emitidos]
    huecos = sorted((b - a).total_seconds() for a, b in zip(marcas[:-1], marcas[1:], strict=True))
    mediana = huecos[len(huecos) // 2]
    assert abs(mediana - 1 / MUESTREO) <= 0.1 / MUESTREO
    # Y ni un salto por encima de 1 s: no hay reconexion ni re-anclaje en este escenario.
    assert max(huecos) <= 1.0
    assert marcas == sorted(marcas)
    assert len(set(marcas)) == len(marcas)


def test_el_consumidor_no_espera_cuando_ya_hay_fotograma_seleccionado() -> None:
    # El detalle barato que tambien estaba medido: `_emitir` sumaba el periodo al tiempo
    # de proceso en vez de solaparlo, ~200 ms por fotograma regalados. Ahora quien decide
    # la cadencia es el hilo lector, y el consumidor no espera a nada que ya tenga.
    reloj = RelojFalso()
    captura = CapturaGobernada()
    origen = fuente(Fabrica(captura), target_fps=MUESTREO, reloj=reloj)
    with abierta(origen, captura):
        fotogramas = origen.frames()
        captura.producir(60)  # 2 s de video: 10 seleccionados esperando
        emitidos = [_siguiente(fotogramas) for _ in range(10)]

    assert [f.index for f in emitidos] == list(range(10))
    assert reloj.esperas == [], "el consumidor ha dormido teniendo fotogramas esperando"
    assert reloj.ahora == INICIO


def test_frames_no_termina_sola_en_un_directo() -> None:
    # Los tests la acotan con un limite de iteraciones; el bucle no se deja correr.
    captura = CapturaFalsa()
    with abierta(fuente(Fabrica(captura)), captura) as origen:
        fotogramas = origen.frames()
        recogidos = [f for _, f in zip(range(6), fotogramas, strict=False)]
        assert len(recogidos) == 6
        assert next(fotogramas) is not None


# --- Cierre limpio --------------------------------------------------------------------


def test_close_para_el_hilo_lector_y_espera_a_que_muera() -> None:
    captura = CapturaFalsa()
    origen = fuente(Fabrica(captura))
    origen.open()
    assert len(hilos_del_connector()) == 1
    origen.close()
    # Sin `join` esta comprobacion pasaria a veces y fallaria a veces.
    assert hilos_del_connector() == []
    origen.close()
    assert captura.liberaciones == 1


def test_close_limpia_aunque_el_consumidor_abandone_el_generador_a_medias() -> None:
    # El hilo es de la fuente, no del generador: no depende de que nadie lo agote.
    captura = CapturaFalsa()
    origen = fuente(Fabrica(captura))
    origen.open()
    for _ in origen.frames():
        break
    origen.close()
    assert hilos_del_connector() == []
    assert captura.liberaciones == 1


# --- Resolucion de la URL -------------------------------------------------------------


def test_el_resolvedor_por_defecto_pasa_a_ytdlp_el_format_y_los_clientes_que_funcionan() -> None:
    # Sin estos clientes este directo responde "This video is not available", que engana
    # porque suena a video borrado (projects/01-parking-trafico/fuente.md).
    doble, registro = doble_de_ytdlp({"url": DIRECTA})
    directa = resolver_con_ytdlp(
        URL, formato="96", extractor_args=clientes_que_funcionan(), ydl_class=doble
    )
    assert directa == DIRECTA
    assert registro["url"] == URL
    assert registro["download"] is False
    assert registro["opciones"]["format"] == "96"
    assert registro["opciones"]["extractor_args"] == CLIENTES


def test_el_resolvedor_por_defecto_acepta_formatos_separados_de_video_y_audio() -> None:
    doble, _ = doble_de_ytdlp({"requested_formats": [{"url": DIRECTA}, {"url": "audio"}]})
    assert (
        resolver_con_ytdlp(URL, formato="best", extractor_args=CLIENTES, ydl_class=doble) == DIRECTA
    )


def test_una_resolucion_sin_url_directa_es_un_source_error() -> None:
    doble, _ = doble_de_ytdlp({"title": "Town of Collingwood Live Stream"})
    with pytest.raises(SourceError, match="ninguna URL directa"):
        resolver_con_ytdlp(URL, formato="96", extractor_args=CLIENTES, ydl_class=doble)


def test_la_fuente_resuelve_con_el_format_y_los_extractor_args_de_su_configuracion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doble, registro = doble_de_ytdlp({"url": DIRECTA})
    monkeypatch.setattr(modulo, "_cargar_ydl", lambda: doble)
    reloj = RelojFalso()
    captura = CapturaFalsa()
    fabrica = Fabrica(captura)
    origen = YouTubeLiveSource(
        URL,
        target_fps=5,
        format="96",
        capture_factory=fabrica,
        clock=reloj,
        sleeper=reloj.dormir,
    )
    with abierta(origen, captura):
        pass
    assert registro["opciones"]["format"] == "96"
    assert registro["opciones"]["extractor_args"] == CLIENTES
    # Y lo que llega a OpenCV es la URL resuelta, no la de la pagina de YouTube.
    assert fabrica.urls == [DIRECTA]


def test_un_fallo_del_resolvedor_se_convierte_en_source_error_con_la_url_y_la_causa() -> None:
    # Ninguna excepcion de yt-dlp escapa hacia arriba: quien llama solo conoce SourceError.
    causa = RuntimeError("ERROR: This video is not available")

    def revienta(_url: str) -> str:
        raise causa

    origen = fuente(Fabrica(CapturaFalsa()), resolvedor=revienta)  # type: ignore[arg-type]
    with pytest.raises(SourceError, match=re.escape(URL)) as fallo:
        origen.open()
    assert fallo.value.__cause__ is causa
    assert "This video is not available" in str(fallo.value)


def test_una_captura_que_no_abre_es_un_source_error_con_la_url() -> None:
    class NoAbre(CapturaFalsa):
        def isOpened(self) -> bool:  # noqa: N802 - la firma la impone OpenCV
            return False

    with pytest.raises(SourceError, match=re.escape(URL)):
        fuente(Fabrica(NoAbre())).open()


@pytest.mark.network
def test_resuelve_el_directo_real_del_proyecto_01() -> None:
    """El unico test que sale a la red. Fuera de la suite por defecto y de CI.

    Comprueba lo que ningun doble puede comprobar: que estos clientes de yt-dlp siguen
    funcionando con este directo. Si YouTube los rompe, el sintoma sera el mensaje
    enganoso "This video is not available".
    """
    directa = resolver_con_ytdlp(URL, formato="96", extractor_args=clientes_que_funcionan())
    assert directa.startswith("http")


# --- Caducidad de la URL y reconexion -------------------------------------------------


def test_re_resuelve_la_url_antes_de_que_caduque_sin_que_el_consumidor_note_nada(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # La URL HLS caduca a las 6 h exactas. Esperar a que falle la lectura ya es tarde:
    # son seis horas y un hueco de datos cada vez.
    reloj = RelojFalso()
    resolvedor = Resolvedor()
    fabrica = Fabrica(CapturaFalsa)
    origen = fuente(fabrica, reloj=reloj, resolvedor=resolvedor, refresh_url_every_s=18000)
    with caplog.at_level(logging.INFO), abierta(origen) as fuente_abierta:
        fotogramas = fuente_abierta.frames()
        primero = next(fotogramas)
        assert resolvedor.llamadas == 1

        reloj.avanzar(18001)
        assert resolvedor.esperar(2), "no ha re-resuelto la URL al vencer refresh_url_every_s"
        segundo = next(fotogramas)

    assert resolvedor.llamadas == 2, "una re-resolucion, exactamente"
    assert fabrica.urls == ["https://directo-1.m3u8", "https://directo-2.m3u8"]
    # El consumidor no nota nada mas que un hueco: los indices siguen siendo contiguos.
    assert [primero.index, segundo.index] == [0, 1]
    assert "caduca" in caplog.text


def test_reconecta_cuando_la_captura_deja_de_entregar_fotogramas(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reloj = RelojFalso(paso=timedelta(milliseconds=1))
    resolvedor = Resolvedor()
    # Cada captura entrega 3 fotogramas y se muere, como un directo que se corta.
    fabrica = Fabrica(lambda: CapturaFalsa(total=3))
    origen = fuente(fabrica, reloj=reloj, resolvedor=resolvedor, backoff_s=[0.5])
    with caplog.at_level(logging.INFO), abierta(origen) as fuente_abierta:
        fotogramas = [f for _, f in zip(range(10), fuente_abierta.frames(), strict=False)]

    assert resolvedor.llamadas > 1, "no ha vuelto a resolver la URL al caerse la captura"
    assert fabrica.llamadas == resolvedor.llamadas
    # El indice no se reinicia al reconectar: es lo que hace que `every_n_frames` siga
    # significando lo mismo despues de una caida (contrato de #2).
    assert [f.index for f in fotogramas] == list(range(10))
    marcas = [f.timestamp for f in fotogramas]
    assert marcas == sorted(marcas)
    assert len(set(marcas)) == len(marcas)
    assert all(marca.utcoffset() == timedelta(0) for marca in marcas)
    assert "ha dejado de entregar fotogramas" in caplog.text


def test_el_backoff_repite_el_ultimo_valor_cuando_hay_mas_fallos_que_esperas() -> None:
    reloj = RelojFalso()
    # La primera captura abre pero no entrega nada, y a partir de ahi no se puede reabrir.
    fabrica = Fabrica(CapturaFalsa(total=0), SourceError("el directo no responde"))
    origen = fuente(fabrica, reloj=reloj, max_retries=5, backoff_s=[1, 2, 5])
    with abierta(origen) as fuente_abierta, pytest.raises(SourceError, match="5 reintentos"):
        next(fuente_abierta.frames())
    # Nadie ha emitido nada, asi que estas son las esperas del backoff y solo esas.
    assert reloj.esperas == [1.0, 2.0, 5.0, 5.0, 5.0]


def test_max_retries_cero_no_reintenta_y_falla_al_primer_fallo() -> None:
    reloj = RelojFalso()
    fabrica = Fabrica(CapturaFalsa(total=0), SourceError("el directo no responde"))
    origen = fuente(fabrica, reloj=reloj, max_retries=0)
    with abierta(origen) as fuente_abierta, pytest.raises(SourceError, match="0 reintentos"):
        next(fuente_abierta.frames())
    assert reloj.esperas == []
    assert fabrica.llamadas == 1


def test_max_retries_tres_se_rinde_al_cuarto_fallo_consecutivo_diciendo_cuantos_hizo() -> None:
    reloj = RelojFalso()
    fabrica = Fabrica(CapturaFalsa(total=0), SourceError("el directo no responde"))
    origen = fuente(fabrica, reloj=reloj, max_retries=3, backoff_s=[1])
    with abierta(origen) as fuente_abierta, pytest.raises(SourceError, match="3 reintentos"):
        next(fuente_abierta.frames())
    assert reloj.esperas == [1.0, 1.0, 1.0]


def test_max_retries_none_reintenta_indefinidamente() -> None:
    reloj = RelojFalso()
    # Ocho fallos seguidos: con max_retries=3 se habria rendido hace rato.
    guion: list[object] = [CapturaFalsa(total=0)]
    guion += [SourceError("el directo no responde")] * 8
    guion.append(CapturaFalsa)
    fabrica = Fabrica(*guion)
    origen = fuente(fabrica, reloj=reloj, max_retries=None, backoff_s=[0.5])
    with abierta(origen) as fuente_abierta:
        assert next(fuente_abierta.frames()).index == 0
    assert len(reloj.esperas) == 9


def test_el_contador_de_reintentos_se_reinicia_tras_una_reconexion_con_exito() -> None:
    # Un directo que se cae un rato cada hora no debe agotar el presupuesto de reintentos
    # al cabo del dia. 2 fallos, exito, 2 fallos mas, con max_retries=3: sigue vivo.
    reloj = RelojFalso()
    fabrica = Fabrica(
        CapturaFalsa(total=1),
        SourceError("fallo 1"),
        SourceError("fallo 2"),
        lambda: CapturaFalsa(total=1),
        SourceError("fallo 3"),
        SourceError("fallo 4"),
        CapturaFalsa,
    )
    origen = fuente(fabrica, reloj=reloj, max_retries=3, backoff_s=[0.5])
    with abierta(origen) as fuente_abierta:
        fotogramas = [f for _, f in zip(range(3), fuente_abierta.frames(), strict=False)]
    assert [f.index for f in fotogramas] == [0, 1, 2]
    assert fabrica.llamadas == 7


def test_cada_reconexion_deja_constancia_del_motivo_en_el_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sin esto, "por que habia un hueco de datos anoche" no tiene respuesta.
    reloj = RelojFalso()
    fabrica = Fabrica(CapturaFalsa(total=0), SourceError("el directo no responde"))
    origen = fuente(fabrica, reloj=reloj, max_retries=1, backoff_s=[1])
    with (
        caplog.at_level(logging.INFO),
        abierta(origen) as fuente_abierta,
        pytest.raises(SourceError),
    ):
        next(fuente_abierta.frames())
    assert "ha dejado de entregar fotogramas" in caplog.text
    assert "reconectando" in caplog.text.lower()
    assert "el directo no responde" in caplog.text
    assert re.search(r"esperando 1[.,]0 s", caplog.text)


# --- Los `Frame` que salen ------------------------------------------------------------


def test_el_timestamp_es_el_del_video_y_no_el_del_transporte() -> None:
    # "Cuando paso" y "cuando lo miramos" no son lo mismo, y el analizador quiere el
    # primero. Aqui el transporte entrega 2 s de video de golpe y despues el reloj avanza
    # 10 s: los timestamps siguen siendo los del video, separados su periodo.
    reloj = RelojFalso()
    captura = CapturaGobernada()
    origen = fuente(Fabrica(captura), target_fps=MUESTREO, reloj=reloj)
    with abierta(origen, captura):
        fotogramas = origen.frames()
        captura.producir(60)
        reloj.avanzar(10)
        marcas = [_siguiente(fotogramas).timestamp for _ in range(10)]
    assert marcas == [INICIO + timedelta(seconds=0.2 * n) for n in range(10)]


def test_los_timestamps_crecen_aunque_el_reloj_no_avance_ni_un_microsegundo() -> None:
    # Un reloj parado es el caso extremo de dos cosas reales: un reloj de baja resolucion
    # y un NTP que echa la hora atras en un proceso que lleva dias vivo. Con el tiempo de
    # video no hace falta ninguna defensa contra eso: los instantes salen del numero de
    # fotograma, no de cuando se leyo.
    reloj = RelojFalso()
    captura = CapturaGobernada()
    origen = fuente(
        Fabrica(captura), target_fps=MUESTREO, reloj=reloj, sleeper=lambda _segundos: None
    )
    with abierta(origen, captura):
        fotogramas = origen.frames()
        captura.producir(30)
        marcas = [_siguiente(fotogramas).timestamp for _ in range(5)]
    assert reloj.ahora == INICIO, "el reloj se ha movido: este test ya no prueba lo que dice"
    assert marcas == [INICIO + timedelta(seconds=0.2 * n) for n in range(5)]
    assert all(marca.utcoffset() == timedelta(0) for marca in marcas)


def test_las_imagenes_salen_tal_cual_las_entrego_la_captura() -> None:
    captura = CapturaGobernada()
    with abierta(fuente(Fabrica(captura)), captura) as origen:
        fotogramas = origen.frames()
        captura.producir(1)
        fotograma = next(fotogramas)
    assert fotograma.image.shape == (4, 4, 3)
    assert fotograma.image.dtype == np.uint8
