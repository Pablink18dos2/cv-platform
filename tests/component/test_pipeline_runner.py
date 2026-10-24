"""El pipeline entero con dobles: cableado, cadencia, ROI, sinks y apagado.

Es el nivel "component" de `_docs/architecture.md`: fuente, detector, tracker,
analizadores y sinks **falsos**, determinista, sin red, sin modelo y sin dormir. La suite
completa de este fichero tarda milisegundos, y eso es una condicion del issue, no una
casualidad: el reloj, el cronometro y la espera se inyectan igual que en `cvp/sources/live.py`.

La fuente falsa **levanta un hilo de verdad** al abrirse. Es lo unico que se copia del
connector en directo, y se copia porque el fallo que estos tests existen para impedir es
justamente ese: abandonar el bucle sin cerrar la fuente deja el hilo lector leyendo del
stream para siempre.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from cvp.core import (
    AnalyzerOutput,
    BBox,
    Detection,
    Event,
    Frame,
    Metric,
    SourceInfo,
    Track,
    UnknownTypeError,
)
from cvp.core.registry import Registry
from cvp.pipeline import (
    AbortRequested,
    ConfigError,
    PipelineError,
    ProjectConfig,
    load_project_config,
)
from cvp.pipeline.config import Reconnect
from cvp.pipeline.factory import build_pipeline
from cvp.pipeline.runner import PipelineRunner, manejar_sigint, run_project

INICIO = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

# El nombre del hilo de la fuente falsa. Es lo que busca `threading.enumerate()`.
HILO_LECTOR = "fake-source-lector"

# Tope de los `join`: si el cierre es correcto se cumplen en microsegundos, y si no lo es
# el test falla en vez de colgar la suite.
ESPERA_MAXIMA_S = 2.0


# --------------------------------------------------------------------------------------
# Los dobles
# --------------------------------------------------------------------------------------


class CronometroFalso:
    """Cronometro monotono que solo avanza cuando se le dice.

    `paso_por_llamada` sirve para medir latencias exactas: con 0,01 s cada bloque tarda
    exactamente 10 ms y la mediana no depende de lo rapido que vaya la maquina del que
    corre los tests.
    """

    def __init__(self, paso_por_llamada: float = 0.0) -> None:
        self.valor = 0.0
        self.paso = paso_por_llamada

    def __call__(self) -> float:
        actual = self.valor
        self.valor += self.paso
        return actual

    def avanzar(self, segundos: float) -> None:
        self.valor += segundos


class RelojFalso:
    """Reloj de pared congelado, para que `run_id` y los timestamps sean deterministas."""

    def __init__(self, inicio: datetime = INICIO) -> None:
        self.valor = inicio

    def __call__(self) -> datetime:
        return self.valor


class FakeSource:
    """Fuente falsa con un hilo lector de verdad, como el connector en directo.

    El hilo no lee nada: se queda esperando un evento. Con eso basta para que
    `threading.enumerate()` distinga entre una fuente cerrada y una abandonada, que es
    exactamente lo que hay que comprobar.
    """

    # Contadores de clase: cuantas se han construido y cual fue la ultima que salio de
    # `from_config`. Es como los tests miran lo que fabrico el runner sin hurgar en sus
    # atributos privados.
    instancias = 0
    ultima: Any = None

    def __init__(
        self,
        *,
        target_fps: float = 5.0,
        frames: int = 5,
        width: int = 1920,
        height: int = 1080,
        fallos_al_abrir: int = 0,
        al_emitir: Callable[[int], None] | None = None,
    ) -> None:
        type(self).instancias += 1
        self.target_fps = target_fps
        self.n = frames
        self.width = width
        self.height = height
        self.fallos_al_abrir = fallos_al_abrir
        self.al_emitir = al_emitir
        self.aperturas = 0
        self.cierres = 0
        self.creada_desde: Any = None
        self._info: SourceInfo | None = None
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    @classmethod
    def from_config(cls, cfg: Any) -> FakeSource:
        fuente = cls(target_fps=cfg.target_fps)
        fuente.creada_desde = cfg
        cls.ultima = fuente
        return fuente

    @property
    def info(self) -> SourceInfo:
        if self._info is None:
            raise RuntimeError("hay que llamar a open() antes de pedir info")
        return self._info

    def open(self) -> None:
        self.aperturas += 1
        if self.aperturas <= self.fallos_al_abrir:
            raise RuntimeError(f"el directo no contesta (intento {self.aperturas})")
        self._info = SourceInfo(width=self.width, height=self.height, native_fps=30.0, is_live=True)
        self._parar.clear()
        self._hilo = threading.Thread(target=self._parar.wait, name=HILO_LECTOR, daemon=True)
        self._hilo.start()

    def frames(self) -> Iterator[Frame]:
        # Una imagen diminuta: el runner no la mira y lo unico que importa del tamano del
        # fotograma es lo que declara `SourceInfo`, que es de donde sale el ROI en pixeles.
        imagen = np.zeros((4, 4, 3), dtype=np.uint8)
        for i in range(self.n):
            if self._parar.is_set():
                return
            if self.al_emitir is not None:
                self.al_emitir(i)
            yield Frame(image=imagen, index=i, timestamp=INICIO + timedelta(seconds=i))

    def close(self) -> None:
        self.cierres += 1
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(ESPERA_MAXIMA_S)
            self._hilo = None


def _deteccion(x: float = 10.0) -> Detection:
    return Detection(bbox=BBox(x, 10, x + 20, 40), class_id=2, label="car", confidence=0.8)


class FakeDetector:
    """Detector falso que apunta el ROI que recibe en cada llamada."""

    ultima: Any = None

    def __init__(
        self,
        *,
        detecciones: list[Detection] | None = None,
        revienta_en: int | None = None,
        excepcion: type[BaseException] = RuntimeError,
    ) -> None:
        self.detecciones = detecciones if detecciones is not None else [_deteccion()]
        self.revienta_en = revienta_en
        self.excepcion = excepcion
        self.rois: list[BBox | None] = []
        self.vistos: list[int] = []
        self.warmups = 0
        self.creada_desde: Any = None

    @classmethod
    def from_config(cls, cfg: Any) -> FakeDetector:
        detector = cls()
        detector.creada_desde = cfg
        cls.ultima = detector
        return detector

    def warmup(self) -> None:
        self.warmups += 1

    def detect(self, frame: Frame, roi: BBox | None = None) -> list[Detection]:
        self.rois.append(roi)
        self.vistos.append(frame.index)
        if self.revienta_en is not None and frame.index == self.revienta_en:
            raise self.excepcion(f"el detector ha reventado en el fotograma {frame.index}")
        return list(self.detecciones)


class FakeTracker:
    """Tracker falso que se queda con el `frame_rate` que le pasa el runner."""

    def __init__(self, *, frame_rate: float) -> None:
        self.frame_rate = frame_rate
        self.updates: list[int] = []
        self.resets = 0
        self.creada_desde: Any = None

    @classmethod
    def from_config(cls, cfg: Any, *, frame_rate: float) -> FakeTracker:
        tracker = cls(frame_rate=frame_rate)
        tracker.creada_desde = cfg
        return tracker

    def update(self, frame: Frame, detections: list[Detection]) -> list[Track]:
        self.updates.append(frame.index)
        return [Track.from_detection(d, track_id=i + 1) for i, d in enumerate(detections)]

    def reset(self) -> None:
        self.resets += 1


class FakeAnalyzer:
    """Analizador falso que apunta en que fotogramas le han llamado."""

    def __init__(
        self,
        *,
        name: str = "fake",
        every_n_frames: int = 1,
        al_procesar: Callable[[Frame], None] | None = None,
    ) -> None:
        self.name = name
        self.every_n_frames = every_n_frames
        self.al_procesar = al_procesar
        self.vistos: list[int] = []
        # Lo que el runner le paso en cada llamada: es lo que fija que las detecciones
        # crudas llegan al analizador junto a los tracks (decision 1 de #11).
        self.recibido: list[tuple[list[Track], list[Detection]]] = []
        self.creada_desde: Any = None

    @classmethod
    def from_config(cls, cfg: Any) -> FakeAnalyzer:
        analizador = cls(name=cfg.effective_name, every_n_frames=cfg.every_n_frames)
        analizador.creada_desde = cfg
        return analizador

    def process(
        self, frame: Frame, tracks: list[Track], detections: list[Detection]
    ) -> AnalyzerOutput:
        self.vistos.append(frame.index)
        self.recibido.append((list(tracks), list(detections)))
        if self.al_procesar is not None:
            self.al_procesar(frame)
        return AnalyzerOutput(
            metrics=(
                Metric(
                    analyzer=self.name,
                    name="vehicles_in_scene",
                    value=float(len(tracks)),
                    timestamp=frame.timestamp,
                ),
            )
        )

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray:
        return image


class FakeSink:
    """Sink falso. Con `falla=True` lanza en cada escritura, que es el caso interesante."""

    def __init__(self, *, name: str = "fake", falla: bool = False) -> None:
        self.name = name
        self.falla = falla
        self.escrituras: list[tuple[str, Frame, AnalyzerOutput]] = []
        self.flushes = 0
        self.creada_desde: Any = None
        self.proyecto: tuple[str, str] | None = None

    @classmethod
    def from_config(cls, cfg: Any, *, project_id: str, project_name: str, **_: Any) -> FakeSink:
        # `project_id` y `project_name` no salen del bloque `sinks`: los pasa la fabrica
        # porque un sink tiene que saber de que proyecto es lo que escribe (#9).
        sink = cls(name=cfg.type)
        sink.creada_desde = cfg
        sink.proyecto = (project_id, project_name)
        return sink

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        if self.falla:
            raise RuntimeError(f"el sink {self.name} no puede escribir")
        self.escrituras.append((run_id, frame, output))

    def flush(self) -> None:
        self.flushes += 1


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------


def runner(**cambios: Any) -> PipelineRunner:
    """Un runner con dobles y relojes falsos; los tests solo cambian lo que les importa."""
    argumentos: dict[str, Any] = {
        "source": FakeSource(),
        "detector": FakeDetector(),
        "tracker": FakeTracker(frame_rate=5.0),
        "analyzers": (),
        "sinks": (),
        "clock": RelojFalso(),
        "timer": CronometroFalso(),
        "sleeper": lambda _s: None,
    }
    argumentos.update(cambios)
    return PipelineRunner(**argumentos)


def desde_config(config: ProjectConfig, **cambios: Any) -> PipelineRunner:
    """El runner que construye el CLI, pero con dobles y con los relojes parados."""
    argumentos: dict[str, Any] = {
        "registries": registros(),
        "clock": RelojFalso(),
        "timer": CronometroFalso(),
        "sleeper": lambda _s: None,
    }
    argumentos.update(cambios)
    return PipelineRunner.from_config(config, **argumentos)


def hilos_de_la_fuente() -> list[str]:
    return [h.name for h in threading.enumerate() if h.name == HILO_LECTOR]


MINIMO: dict[str, Any] = {
    "id": "proyecto",
    "name": "Proyecto de prueba",
    "source": {"type": "webcam", "target_fps": 5},
    "detector": {"type": "yolo", "classes": ["car"], "roi": [0.15, 0.28, 1.0, 1.0]},
    "tracker": {"type": "bytetrack"},
    "analyzers": [
        {
            "type": "traffic_flow",
            "every_n_frames": 10,
            "lines": [
                {
                    "id": "principal",
                    "p1": [0.1, 0.2],
                    "p2": [0.8, 0.7],
                    "positive_direction": "norte",
                    "negative_direction": "sur",
                }
            ],
        }
    ],
    "sinks": [{"type": "sqlite"}],
}


def configuracion(tmp_path: Path, **cambios: Any) -> ProjectConfig:
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(MINIMO | cambios, allow_unicode=True), encoding="utf-8")
    return load_project_config(fichero, env={})


def registros(
    *,
    source: type | None = FakeSource,
    detector: type | None = FakeDetector,
    tracker: type | None = FakeTracker,
    analyzer: type | None = FakeAnalyzer,
    sink: type | None = FakeSink,
) -> Mapping[str, Registry[Any]]:
    """Registros propios con dobles bajo los nombres de verdad.

    Con esto el cableado se prueba entero —resolver por nombre, `from_config`, el
    `frame_rate` del tracker— sin importar `cvp.sources`, `cvp.detectors` ni
    `cvp.trackers`, que son tres segundos de suite por culpa de OpenCV y supervision. Que
    los registros de verdad tengan los nombres que dicen es lo que comprueba #20.
    """
    tabla: dict[str, Registry[Any]] = {
        "source": Registry("source"),
        "detector": Registry("detector"),
        "tracker": Registry("tracker"),
        "analyzers": Registry("analyzer"),
        "sinks": Registry("sink"),
    }
    if source is not None:
        tabla["source"].register("webcam")(source)
    if detector is not None:
        tabla["detector"].register("yolo")(detector)
    if tracker is not None:
        tabla["tracker"].register("bytetrack")(tracker)
    if analyzer is not None:
        tabla["analyzers"].register("traffic_flow")(analyzer)
    if sink is not None:
        tabla["sinks"].register("sqlite")(sink)
    return tabla


# --------------------------------------------------------------------------------------
# El cableado: cada bloque se resuelve por nombre en su registro
# --------------------------------------------------------------------------------------


def test_cada_bloque_se_construye_desde_su_registro_con_su_from_config(tmp_path: Path) -> None:
    config = configuracion(tmp_path)
    bloques = build_pipeline(config, registries=registros())

    assert isinstance(bloques.source, FakeSource)
    assert isinstance(bloques.detector, FakeDetector)
    assert isinstance(bloques.tracker, FakeTracker)
    assert [type(a) for a in bloques.analyzers] == [FakeAnalyzer]
    assert [type(s) for s in bloques.sinks] == [FakeSink]
    # Y cada uno ha recibido **su** trozo de configuracion, no el documento entero.
    assert bloques.source.creada_desde is config.source
    assert bloques.detector.creada_desde is config.detector
    assert bloques.tracker.creada_desde is config.tracker
    assert bloques.analyzers[0].creada_desde is config.analyzers[0]
    assert bloques.sinks[0].creada_desde is config.sinks[0]


def test_el_frame_rate_del_tracker_sale_del_target_fps_de_la_fuente(tmp_path: Path) -> None:
    """No se escribe en el YAML a proposito (#3, #7): lo pasa el runner, que conoce los dos.

    Si alguien lo pusiera en el bloque `tracker`, acabaria discrepando del de la fuente y
    el filtro de Kalman predeciria posiciones para un tiempo que no ha pasado.
    """
    config = configuracion(tmp_path, source={"type": "webcam", "target_fps": 3.5})
    bloques = build_pipeline(config, registries=registros())
    assert bloques.tracker.frame_rate == 3.5
    assert not hasattr(config.tracker, "frame_rate")


def test_un_type_registrado_a_medias_falla_al_arrancar_con_la_lista_de_disponibles(
    tmp_path: Path,
) -> None:
    """El hueco que #3 dejo abierto: `CONFIG_SCHEMAS` y los registros son dos listas.

    `traffic_flow` tiene esquema (valida) y todavia no tiene implementacion (#10), asi que
    el YAML pasa `cvp validate` y el runner tiene que rechazarlo **al arrancar**.
    """
    config = configuracion(tmp_path)
    FakeSource.instancias = 0
    with pytest.raises(UnknownTypeError) as error:
        build_pipeline(config, registries=registros(analyzer=None))

    assert "analyzers[0].type" in str(error.value)
    assert "no hay ningun analyzer llamado 'traffic_flow'" in str(error.value)
    assert "no hay ningun analyzer registrado todavia" in str(error.value)
    # Y falla **antes** de construir nada: si no, el error llegaria medio minuto despues,
    # detras del log de ultralytics cargando los pesos.
    assert FakeSource.instancias == 0


def test_el_error_de_un_type_desconocido_lista_los_nombres_que_si_existen(
    tmp_path: Path,
) -> None:
    otro = registros()
    otro["sinks"].register("jsonl")(FakeSink)
    config = configuracion(tmp_path, sinks=[{"type": "preview"}])
    with pytest.raises(UnknownTypeError) as error:
        build_pipeline(config, registries=otro)
    assert "sinks[0].type" in str(error.value)
    assert "Disponibles: jsonl, sqlite" in str(error.value)


def test_una_clase_registrada_sin_from_config_lo_dice_por_su_nombre(tmp_path: Path) -> None:
    class SinFabrica:
        pass

    config = configuracion(tmp_path)
    with pytest.raises(PipelineError, match="from_config"):
        build_pipeline(config, registries=registros(sink=SinFabrica))


def test_un_from_config_que_rechaza_su_configuracion_es_un_configerror_con_el_campo(
    tmp_path: Path,
) -> None:
    """Lo que el esquema no puede comprobar lo comprueba el bloque al construirse: el
    contenido de `zones.json` (#11) lo valida `ParkingOccupancyAnalyzer.from_config`. Un
    `ValueError` de ahi es un error de **configuracion** —se arregla editando un fichero—
    y tiene que salir con la misma forma que uno del YAML: la ruta del `project.yaml`, el
    campo (`analyzers[0]`) y el motivo, para que el CLI lo trate igual (sin traceback,
    codigo 1). Un `PipelineError` sigue siendo de ejecucion y no se toca."""

    class AnalizadorQueRechaza(FakeAnalyzer):
        @classmethod
        def from_config(cls, cfg: Any) -> FakeAnalyzer:
            raise ValueError("zones.json: 'zones' esta vacia: sin plazas no hay nada que medir")

    config = configuracion(tmp_path)
    with pytest.raises(ConfigError) as error:
        build_pipeline(config, registries=registros(analyzer=AnalizadorQueRechaza))
    assert str(error.value).startswith(f"{config.config_path}: 1 error de configuración")
    assert error.value.problems == (
        "analyzers[0]: zones.json: 'zones' esta vacia: sin plazas no hay nada que medir",
    )

    class SinkQueNoArranca(FakeSink):
        @classmethod
        def from_config(cls, cfg: Any, **_: Any) -> FakeSink:
            raise PipelineError("la base de datos no responde")

    with pytest.raises(PipelineError, match="no responde"):
        build_pipeline(config, registries=registros(sink=SinkQueNoArranca))


def test_un_proyecto_sin_analizadores_ni_sinks_arranca_y_avisa(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Es el modo con el que se mide el encadenado antes de que existan #9, #10 y #11.

    Corre sin quejarse, pero **no en silencio**: un `sinks: []` puesto sin querer tira los
    datos a la basura, y eso merece un aviso en el log.
    """
    config = configuracion(tmp_path, analyzers=[], sinks=[])
    with caplog.at_level(logging.WARNING):
        bloques = build_pipeline(config, registries=registros())
    assert bloques.analyzers == ()
    assert bloques.sinks == ()
    assert "no tiene analizadores" in caplog.text
    assert "no tiene sinks" in caplog.text
    assert desde_config(config).run().frames == 5


def test_el_roi_del_yaml_llega_al_detector_traducido_a_pixeles_una_sola_vez(
    tmp_path: Path,
) -> None:
    """La unica costura entre el YAML (fracciones) y el core (pixeles).

    Se traduce despues de `open()`, que es cuando se sabe la resolucion, y el mismo `BBox`
    viaja en todas las llamadas: el ROI es argumento por llamada, no estado del detector.
    """
    config = configuracion(tmp_path)  # roi [0.15, 0.28, 1.0, 1.0] sobre 1920x1080
    desde_config(config).run()
    detector = FakeDetector.ultima

    assert len(detector.rois) == 5
    roi = detector.rois[0]
    assert (roi.x1, roi.y1, roi.x2, roi.y2) == pytest.approx((288.0, 302.4, 1920.0, 1080.0))
    # El mismo objeto en las cinco llamadas, no cinco iguales: es lo que prueba que la
    # traduccion se hizo una vez y no una por fotograma.
    assert all(cada is roi for cada in detector.rois)


def test_sin_roi_en_el_yaml_el_detector_recibe_none(tmp_path: Path) -> None:
    config = configuracion(tmp_path, detector={"type": "yolo", "classes": ["car"]})
    desde_config(config).run()
    assert FakeDetector.ultima.rois == [None] * 5


# --------------------------------------------------------------------------------------
# El bucle: cadencia, sinks y run_id
# --------------------------------------------------------------------------------------


def test_cada_analizador_corre_a_su_cadencia_y_las_cuentas_son_exactas() -> None:
    """30 fotogramas: cadencia 1 son 30 ejecuciones y cadencia 10 son 3, ni una mas.

    Es el criterio que mas facil se cumple con un off-by-one, asi que se comprueban
    tambien **que** fotogramas: 0, 10 y 20, contando fotogramas emitidos por la fuente.
    """
    cada_uno = FakeAnalyzer(name="cada-1", every_n_frames=1)
    cada_diez = FakeAnalyzer(name="cada-10", every_n_frames=10)
    corredor = runner(
        source=FakeSource(frames=30), analyzers=(cada_uno, cada_diez), sinks=(FakeSink(),)
    )

    stats = corredor.run()

    assert stats.frames == 30
    assert len(cada_uno.vistos) == 30
    assert len(cada_diez.vistos) == 3
    assert cada_uno.vistos == list(range(30))
    assert cada_diez.vistos == [0, 10, 20]
    assert stats.analyzer_calls == {"cada-1": 30, "cada-10": 3}


def test_la_salida_de_cada_analizador_va_a_todos_los_sinks() -> None:
    uno, otro = FakeSink(name="uno"), FakeSink(name="otro")
    corredor = runner(
        source=FakeSource(frames=3),
        analyzers=(FakeAnalyzer(name="a"), FakeAnalyzer(name="b")),
        sinks=(uno, otro),
    )

    stats = corredor.run()

    # 3 fotogramas x 2 analizadores = 6 salidas, y cada una a los dos sinks.
    assert len(uno.escrituras) == 6
    assert len(otro.escrituras) == 6
    assert stats.sink_writes == 12
    assert stats.sink_errors == 0


def test_un_sink_que_lanza_no_tumba_el_pipeline_ni_deja_sin_escribir_a_los_demas() -> None:
    """Perder una escritura es mucho menos malo que perder el worker."""
    roto, bueno = FakeSink(name="roto", falla=True), FakeSink(name="bueno")
    corredor = runner(source=FakeSource(frames=4), analyzers=(FakeAnalyzer(),), sinks=(roto, bueno))

    stats = corredor.run()

    assert stats.frames == 4
    assert len(bueno.escrituras) == 4
    assert stats.sink_errors == 4
    assert stats.sink_writes == 4
    assert bueno.flushes == 1


def test_el_run_id_es_el_mismo_en_toda_la_ejecucion_y_distinto_en_la_siguiente() -> None:
    sink = FakeSink()
    corredor = runner(source=FakeSource(frames=4), analyzers=(FakeAnalyzer(),), sinks=(sink,))
    stats = corredor.run()

    escritos = {run_id for run_id, _, _ in sink.escrituras}
    assert escritos == {stats.run_id}

    otro = runner(source=FakeSource(frames=1), analyzers=(FakeAnalyzer(),), sinks=(FakeSink(),))
    assert otro.stats.run_id != stats.run_id


def test_un_analizador_con_cadencia_invalida_se_rechaza_al_construir_el_runner() -> None:
    with pytest.raises(ValueError, match="every_n_frames"):
        runner(analyzers=(FakeAnalyzer(every_n_frames=0),))


class TrackerQueDescarta(FakeTracker):
    """Como ByteTrack con el umbral por defecto: confirma solo una parte de lo que ve."""

    def update(self, frame: Frame, detections: list[Detection]) -> list[Track]:
        self.updates.append(frame.index)
        return [Track.from_detection(d, track_id=i + 1) for i, d in enumerate(detections[:1])]


def test_el_analizador_recibe_los_tracks_y_las_detecciones_crudas_del_mismo_fotograma() -> None:
    """Decision 1 de #11, vista desde el runner: el tracker descarta lo que no confirma
    (aqui 3 detecciones -> 1 track), y el analizador tiene que recibir **las dos** listas
    tal cual salieron del detector y del tracker, para que la ocupacion de una plaza no
    dependa de la identidad."""
    tres = [_deteccion(10.0), _deteccion(100.0), _deteccion(200.0)]
    analizador = FakeAnalyzer()
    corredor = runner(
        source=FakeSource(frames=2),
        detector=FakeDetector(detecciones=tres),
        tracker=TrackerQueDescarta(frame_rate=5.0),
        analyzers=(analizador,),
    )

    corredor.run()

    assert len(analizador.recibido) == 2
    for tracks, detecciones in analizador.recibido:
        assert detecciones == tres
        assert [t.track_id for t in tracks] == [1]


# --------------------------------------------------------------------------------------
# Los limites: --max-frames y --duration
# --------------------------------------------------------------------------------------


def test_max_frames_para_en_el_numero_exacto_de_fotogramas() -> None:
    detector = FakeDetector()
    stats = runner(source=FakeSource(frames=100), detector=detector).run(max_frames=7)
    assert stats.frames == 7
    assert detector.vistos == list(range(7))


def test_duration_para_cuando_el_reloj_llega_al_limite() -> None:
    """El tiempo se inyecta: la fuente adelanta el cronometro un segundo por fotograma."""
    cronometro = CronometroFalso()
    fuente = FakeSource(frames=100, al_emitir=lambda _i: cronometro.avanzar(1.0))
    stats = runner(source=fuente, timer=cronometro).run(duration_s=3.0)
    assert stats.frames == 3
    assert stats.elapsed_s == pytest.approx(3.0)


def test_sin_limites_corre_hasta_que_se_acaba_la_fuente() -> None:
    assert runner(source=FakeSource(frames=12)).run().frames == 12


@pytest.mark.parametrize("valor", [0, -1])
def test_un_max_frames_absurdo_se_rechaza_antes_de_abrir_la_fuente(valor: int) -> None:
    fuente = FakeSource()
    with pytest.raises(ValueError, match="max_frames"):
        runner(source=fuente).run(max_frames=valor)
    assert fuente.aperturas == 0


# --------------------------------------------------------------------------------------
# Apagado: los cuatro caminos cierran la fuente
# --------------------------------------------------------------------------------------


def test_el_camino_normal_cierra_la_fuente_y_no_deja_hilos() -> None:
    fuente = FakeSource(frames=3)
    sink = FakeSink()
    stats = runner(source=fuente, analyzers=(FakeAnalyzer(),), sinks=(sink,)).run()
    assert fuente.cierres == 1
    assert sink.flushes == 1
    assert hilos_de_la_fuente() == []
    # Y una ejecucion que termina sola **no** queda marcada como interrumpida: el flag
    # distingue "la pararon" de "se acabo el video", y sirve de poco si siempre dice lo
    # mismo.
    assert stats.interrupted is False
    assert "interrumpida" not in stats.summary()


def test_max_frames_cierra_la_fuente_y_no_deja_hilos() -> None:
    fuente = FakeSource(frames=100)
    sink = FakeSink()
    runner(source=fuente, analyzers=(FakeAnalyzer(),), sinks=(sink,)).run(max_frames=2)
    assert fuente.cierres == 1
    assert sink.flushes == 1
    assert hilos_de_la_fuente() == []


def test_ctrl_c_apaga_limpio_cierra_la_fuente_y_hace_flush() -> None:
    """El primer Ctrl+C no es un fallo: se termina el fotograma, se cierra y se vuelve.

    Aqui llega como `KeyboardInterrupt` desde dentro del detector, que es donde lo levanta
    el manejador por defecto de Python cuando el proceso esta ocupado inferiendo.
    """
    fuente = FakeSource(frames=100)
    sink = FakeSink()
    corredor = runner(
        source=fuente,
        detector=FakeDetector(revienta_en=2, excepcion=KeyboardInterrupt),
        analyzers=(FakeAnalyzer(),),
        sinks=(sink,),
    )

    stats = corredor.run()  # no propaga: apagado limpio

    assert stats.interrupted is True
    assert stats.frames == 2
    assert fuente.cierres == 1
    assert sink.flushes == 1
    assert hilos_de_la_fuente() == []


def test_una_excepcion_cierra_la_fuente_antes_de_propagar() -> None:
    fuente = FakeSource(frames=100)
    sink = FakeSink()
    corredor = runner(
        source=fuente,
        detector=FakeDetector(revienta_en=1),
        analyzers=(FakeAnalyzer(),),
        sinks=(sink,),
    )

    with pytest.raises(RuntimeError, match="ha reventado"):
        corredor.run()

    assert fuente.cierres == 1
    assert sink.flushes == 1
    assert hilos_de_la_fuente() == []


def test_una_fuente_que_falla_al_cerrarse_no_tapa_el_error_original() -> None:
    class FuenteQueNoCierra(FakeSource):
        def close(self) -> None:
            super().close()
            raise RuntimeError("el cierre tambien ha fallado")

    corredor = runner(source=FuenteQueNoCierra(frames=10), detector=FakeDetector(revienta_en=0))
    with pytest.raises(RuntimeError, match="ha reventado"):
        corredor.run()
    assert hilos_de_la_fuente() == []


def test_la_parada_se_atiende_al_terminar_el_fotograma_en_curso() -> None:
    """Lo que hace el primer Ctrl+C: no se corta a mitad, se acaba lo empezado."""
    fuente = FakeSource(frames=100)
    # El analizador necesita al runner y el runner al analizador, asi que uno de los dos
    # llega por una caja. Es el equivalente a que el Ctrl+C caiga a mitad del fotograma 1.
    caja: dict[str, PipelineRunner] = {}
    analizador = FakeAnalyzer(
        al_procesar=lambda frame: caja["runner"].request_stop() if frame.index == 1 else None
    )
    corredor = runner(source=fuente, analyzers=(analizador,))
    caja["runner"] = corredor

    stats = corredor.run()

    assert stats.frames == 2  # el 0 y el 1, entero
    assert analizador.vistos == [0, 1]
    assert fuente.cierres == 1


def test_el_segundo_ctrl_c_aborta_en_vez_de_encadenar_otro_apagado() -> None:
    """Un apagado que no se puede abortar es una trampa: si lo que se ha colgado es el
    cierre de la fuente, el usuario se queda sin salida."""
    corredor = runner()
    corredor.request_stop()
    with pytest.raises(AbortRequested):
        corredor.request_stop()


def test_manejar_sigint_conecta_ctrl_c_con_la_parada_y_deja_el_manejador_como_estaba() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    corredor = runner()
    with manejar_sigint(corredor):
        assert signal.getsignal(signal.SIGINT) is not anterior
        signal.raise_signal(signal.SIGINT)
        # El primero solo pide la parada; el segundo aborta desde el propio manejador.
        with pytest.raises(AbortRequested):
            signal.raise_signal(signal.SIGINT)
        # Y desde el aborto, Ctrl+C vuelve a ser el manejador por defecto: un tercero mata
        # el proceso sin pasar por aqui. Es el ultimo escalon, por si hasta el aborto se
        # quedara colgado dentro del cierre de la fuente.
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is anterior


# --- Ctrl+C de verdad, con el bucle corriendo -----------------------------------------
#
# Los dos tests de arriba llaman a `request_stop()` **fuera** de `run()`, asi que comprueban
# lo que hace `request_stop`, no lo que le pasa a un pipeline en marcha cuando llega la
# senal. Esa distincion es la que dejo pasar el fallo que cazan los tests de aqui abajo: el
# `KeyboardInterrupt` que lanzaba el manejador se lo comia el `except KeyboardInterrupt` del
# propio `run()`, y el segundo Ctrl+C acababa siendo otro apagado ordenado con codigo 0.


class DetectorQueSenala(FakeDetector):
    """Detector que lanza `SIGINT` **de verdad** mientras el bucle esta corriendo.

    Es la unica forma de recorrer el camino del CLI: el manejador de senal se ejecuta en el
    hilo principal, dentro de la llamada que estuviera en curso, y con el proceso ocupado
    infiriendo esa llamada es `detect()`, que es justo donde caen los Ctrl+C de verdad.
    """

    def __init__(self, *, en: int, veces: int = 1, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.en = en
        self.veces = veces

    def detect(self, frame: Frame, roi: BBox | None = None) -> list[Detection]:
        if frame.index == self.en:
            for _ in range(self.veces):
                signal.raise_signal(signal.SIGINT)
        return super().detect(frame, roi)


def test_un_sigint_de_verdad_a_mitad_del_bucle_apaga_limpio_y_queda_registrado() -> None:
    """El camino real de un Ctrl+C: senal de verdad, manejador puesto y bucle corriendo.

    Ademas de apagar limpio tiene que **quedar escrito** que lo pararon: un worker que
    alguien detiene a mano a las tres de la manana no puede figurar como una ejecucion que
    termino sola, porque ese `interrupted` acabara sirviendose por la API (#12).
    """
    fuente = FakeSource(frames=100)
    sink = FakeSink()
    corredor = runner(
        source=fuente,
        detector=DetectorQueSenala(en=2),
        analyzers=(FakeAnalyzer(),),
        sinks=(sink,),
    )

    with manejar_sigint(corredor):
        stats = corredor.run()  # no propaga: apagado limpio, y el CLI saldria con 0

    assert stats.interrupted is True
    assert "interrumpida" in stats.summary()
    assert stats.frames == 3  # el fotograma en curso se termina entero
    assert fuente.cierres == 1
    assert sink.flushes == 1
    assert hilos_de_la_fuente() == []


def test_dos_sigint_de_verdad_a_mitad_del_bucle_abortan_en_vez_de_apagar_ordenadamente() -> None:
    """Los dos Ctrl+C caen **dentro del bucle**, que es donde caen casi siempre.

    El proceso se pasa el 71 % del tiempo esperando fotograma (`_docs/rendimiento.md`), asi
    que la ventana no es estrecha. El aborto tiene que escaparse de `run()` y llegar al CLI
    como tal; si algo lo convierte en un apagado ordenado, el usuario pulsa dos veces y el
    programa le contesta con un 0 educado mientras sigue apagando.
    """
    fuente = FakeSource(frames=100)
    sink = FakeSink()
    corredor = runner(
        source=fuente,
        detector=DetectorQueSenala(en=1, veces=2),
        analyzers=(FakeAnalyzer(),),
        sinks=(sink,),
    )

    with manejar_sigint(corredor), pytest.raises(AbortRequested):
        corredor.run()

    # El fotograma 1 no llega a procesarse: se aborta dentro de su `detect()`.
    assert corredor.stats.frames == 1
    assert corredor.stats.interrupted is True
    # Abortar no es dejarlo todo tirado: si el cierre es rapido se cierra igual. Lo que no
    # se hace es seguir procesando ni esperar a un apagado que podria estar colgado.
    assert fuente.cierres == 1
    assert hilos_de_la_fuente() == []


class SinkQueSenala(FakeSink):
    """Sink que se lleva los dos Ctrl+C dentro de su propio `write()`.

    `_escribir` envuelve cada escritura en un `except Exception` para que un sink roto no
    tumbe el worker, y ese `except` es la **segunda puerta** por la que el aborto podria
    volver al camino ordenado. Es la simetrica de la que fallo en la primera vuelta: alli
    era el `except KeyboardInterrupt`, aqui seria el `except Exception`.
    """

    def __init__(self, *, veces: int = 2, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.veces = veces

    def write(self, run_id: str, frame: Frame, output: AnalyzerOutput) -> None:
        for _ in range(self.veces):
            signal.raise_signal(signal.SIGINT)
        super().write(run_id, frame, output)


def test_el_aborto_tampoco_se_lo_traga_el_except_que_protege_a_los_sinks() -> None:
    """La otra mitad del requisito: `AbortRequested` no puede ser un `Exception`.

    Los tests de arriba prueban que no la caza el `except KeyboardInterrupt` del apagado
    ordenado. Falta la puerta simetrica: si derivara de `Exception`, dos Ctrl+C que cayeran
    dentro de una escritura los tragaria el `except Exception` de `_escribir`, se apuntarian
    como un error de sink, el bucle seguiria tan tranquilo y el usuario recibiria un exit 0.
    Seria el fallo de la primera vuelta entrando por otra puerta.
    """
    fuente = FakeSource(frames=100)
    senalador = SinkQueSenala(name="el-que-recibe-el-ctrl-c")
    siguiente = FakeSink(name="el-de-detras")
    corredor = runner(source=fuente, analyzers=(FakeAnalyzer(),), sinks=(senalador, siguiente))

    with manejar_sigint(corredor), pytest.raises(AbortRequested):
        corredor.run()

    # Un aborto no es un fallo de escritura y no puede contarse como tal: si aparece aqui un
    # 1, es que alguien se lo ha comido y lo ha apuntado en la columna equivocada.
    assert corredor.stats.sink_errors == 0
    assert corredor.stats.sink_writes == 0
    # Y el bucle se para de verdad: el sink de detras no llega a recibir esta salida.
    assert siguiente.escrituras == []
    assert corredor.stats.frames == 1
    assert fuente.cierres == 1
    assert hilos_de_la_fuente() == []


class DetectorQueSenalaDosVeces(DetectorQueSenala):
    """El de arriba con los parametros fijados, para poder registrarlo como un bloque."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(en=1, veces=2, **kwargs)


def test_el_aborto_se_escapa_de_run_project_entero_y_no_solo_de_run(tmp_path: Path) -> None:
    """El camino completo del CLI: `run_project` pone el manejador y corre el bucle.

    Si el aborto se quedara dentro, `cvp run` saldria con 0 y el usuario habria pulsado dos
    veces Ctrl+C para que le contestaran que todo fue bien.
    """
    config = configuracion(tmp_path, analyzers=[], sinks=[])

    with pytest.raises(AbortRequested):
        run_project(
            config,
            registries=registros(detector=DetectorQueSenalaDosVeces),
            clock=RelojFalso(),
            timer=CronometroFalso(),
            sleeper=lambda _s: None,
        )

    assert FakeSource.ultima.cierres == 1
    assert hilos_de_la_fuente() == []


# --------------------------------------------------------------------------------------
# Arranque con la fuente caida
# --------------------------------------------------------------------------------------


def test_el_arranque_reintenta_con_la_politica_del_yaml_y_lo_registra(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#5 dejo `open()` sin reintentos y la decision de politica para aqui: **se reintenta**.

    Un worker que arranca a las tres de la manana y se encuentra un parpadeo de YouTube
    tiene que sobrevivirlo, no dejar la noche sin datos.
    """
    esperas: list[float] = []
    fuente = FakeSource(frames=2, fallos_al_abrir=2)
    corredor = runner(
        source=fuente,
        reconnect=Reconnect(max_retries=20, backoff_s=[1.0, 2.0, 5.0]),
        sleeper=esperas.append,
    )
    with caplog.at_level(logging.WARNING):
        stats = corredor.run()

    assert fuente.aperturas == 3
    assert stats.frames == 2
    assert stats.startup_retries == 2
    assert esperas == [1.0, 2.0]  # la escalera del YAML, en orden
    assert "reintento 1/20" in caplog.text


def test_agotados_los_reintentos_el_arranque_falla_con_la_causa_y_cierra() -> None:
    esperas: list[float] = []
    fuente = FakeSource(fallos_al_abrir=99)
    corredor = runner(
        source=fuente,
        reconnect=Reconnect(max_retries=2, backoff_s=[1.0]),
        sleeper=esperas.append,
    )

    with pytest.raises(PipelineError, match="no contesta"):
        corredor.run()

    assert fuente.aperturas == 3  # el intento inicial y dos reintentos
    assert esperas == [1.0, 1.0]  # se repite el ultimo valor del backoff
    assert fuente.cierres == 1
    assert hilos_de_la_fuente() == []


def test_sin_politica_de_reconexion_el_arranque_no_reintenta() -> None:
    """Un fichero de video que no existe no mejora esperando; el directo si."""
    fuente = FakeSource(fallos_al_abrir=1)
    with pytest.raises(PipelineError):
        runner(source=fuente, reconnect=None).run()
    assert fuente.aperturas == 1


# --------------------------------------------------------------------------------------
# Salud: los contadores, la linea periodica y el resumen
# --------------------------------------------------------------------------------------


def test_las_latencias_se_miden_por_bloque_y_por_separado() -> None:
    """Sin el desglose no se sabe a quien culpar cuando bajen los fps."""
    corredor = runner(source=FakeSource(frames=10), timer=CronometroFalso(paso_por_llamada=0.01))
    stats = corredor.run()

    assert stats.detector.calls == 10
    assert stats.tracker.calls == 10
    assert stats.detector.p50_ms == pytest.approx(10.0)
    assert stats.tracker.p50_ms == pytest.approx(10.0)
    assert stats.detector.p95_ms == pytest.approx(10.0)
    # La espera de la fuente se mide aparte: con un directo que no da abasto es donde se va
    # el tiempo, y sin este numero el desglose culparia al detector de lo que no es suyo.
    assert stats.source.calls == 10
    assert stats.source.p50_ms == pytest.approx(10.0)


def test_los_contadores_son_un_objeto_y_no_solo_texto() -> None:
    """#12 los tendra que servir por la API, y una API que parsee un log nace rota."""
    cronometro = CronometroFalso()
    fuente = FakeSource(frames=4, al_emitir=lambda _i: cronometro.avanzar(0.5))
    corredor = runner(
        source=fuente, timer=cronometro, analyzers=(FakeAnalyzer(),), sinks=(FakeSink(),)
    )
    stats = corredor.run()

    assert stats.frames == 4
    assert stats.elapsed_s == pytest.approx(2.0)
    assert stats.fps == pytest.approx(2.0)
    datos = stats.as_dict()
    assert datos["frames"] == 4
    assert datos["fps"] == pytest.approx(2.0)
    assert datos["run_id"] == stats.run_id
    assert datos["analyzer_calls"] == {"fake": 4}
    assert datos["detector"]["calls"] == 4
    assert datos["sink_writes"] == 4


def test_el_arranque_no_se_cuenta_en_los_fps_de_salida() -> None:
    """Resolver la URL con yt-dlp y cargar los pesos cuesta segundos y se paga **una vez**.

    Sumarlo a los fps solo consigue que una ejecucion corta parezca mucho mas lenta de lo
    que es, y el numero que decide si el presupuesto del plan se sostiene es el del bucle.
    La duracion total sigue estando, y con el desglose.
    """
    cronometro = CronometroFalso()

    class DetectorLento(FakeDetector):
        def warmup(self) -> None:
            super().warmup()
            cronometro.avanzar(10.0)

    fuente = FakeSource(frames=4, al_emitir=lambda _i: cronometro.avanzar(0.5))
    stats = runner(source=fuente, detector=DetectorLento(), timer=cronometro).run()

    assert stats.startup_s == pytest.approx(10.0)
    assert stats.elapsed_s == pytest.approx(12.0)
    assert stats.processing_s == pytest.approx(2.0)
    assert stats.fps == pytest.approx(2.0)
    assert "arranque 10.0 s + bucle 2.0 s" in stats.summary()


def test_la_linea_de_salud_se_escribe_cada_tanto_con_el_desglose(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cronometro = CronometroFalso()
    fuente = FakeSource(frames=6, al_emitir=lambda _i: cronometro.avanzar(10.0))
    corredor = runner(source=fuente, timer=cronometro, health_every_s=30.0)

    with caplog.at_level(logging.INFO):
        corredor.run()

    lineas = [m for m in caplog.messages if m.startswith("salud ")]
    assert len(lineas) == 2  # a los 30 s y a los 60 s
    assert "fps" in lineas[0]
    assert "detector p50" in lineas[0]
    assert "tracker p50" in lineas[0]


def test_el_resumen_final_lleva_los_mismos_numeros_y_la_duracion_total() -> None:
    cronometro = CronometroFalso()
    fuente = FakeSource(frames=5, al_emitir=lambda _i: cronometro.avanzar(1.0))
    stats = runner(
        source=fuente, timer=cronometro, analyzers=(FakeAnalyzer(),), sinks=(FakeSink(),)
    ).run()

    resumen = stats.summary()
    assert stats.run_id in resumen
    assert "duracion total:  5.0 s (arranque 0.0 s + bucle 5.0 s)" in resumen
    assert "fotogramas:      5 (1.00 fps de salida)" in resumen
    assert "detector:" in resumen and "tracker:" in resumen
    assert "fake x5" in resumen
    assert "5 escrituras, 0 errores" in resumen


class FuenteQueCuentaDeSiMisma(FakeSource):
    """Lo que hace `LiveVideoSource`: contadores propios y un gancho `drain_output`."""

    reconnections = 3
    last_reconnection_at = INICIO + timedelta(minutes=7)
    reanchors = 1
    max_latency_s = 42.5

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pendientes = [
            Event(
                analyzer="source",
                name="reanchor",
                timestamp=INICIO,
                attributes={"reason": "deriva", "jump_s": 120.0, "ahead_s": 0.0},
            )
        ]

    def drain_output(self) -> AnalyzerOutput:
        eventos, self.pendientes = self.pendientes, []
        return AnalyzerOutput(events=eventos)


def test_los_reanclajes_y_las_reconexiones_de_la_fuente_llegan_a_los_sinks_y_al_resumen() -> None:
    # #24 y #26: un salto en la serie tiene que poder persistirse (por el mismo canal que los
    # eventos de los analizadores) y el resumen de la ejecucion tiene que decir cuantas
    # veces se cayo la fuente y cuando fue la ultima.
    sink = FakeSink()
    stats = runner(source=FuenteQueCuentaDeSiMisma(frames=3), sinks=(sink,)).run()

    (escritura,) = sink.escrituras
    evento = escritura[2].events[0]
    assert (evento.analyzer, evento.name, evento.attributes["jump_s"]) == (
        "source",
        "reanchor",
        120,
    )
    assert (stats.reconnections, stats.reanchors, stats.source_max_lag_s) == (3, 1, 42.5)
    assert stats.as_dict()["last_reconnection_at"] == "2026-08-23T12:07:00+00:00"
    resumen = stats.summary()
    assert (
        "reconexiones de la fuente (caidas recuperadas): 3, la ultima a las 2026-08-23" in resumen
    )
    assert "re-anclajes del tiempo de video: 1, retraso maximo de la fuente 42.5 s" in resumen
    # Lo de antes no se confunde con esto: son intentos de abrir, no caidas.
    assert "reintentos al abrir la fuente (arranque, no son caidas): 0" in resumen


def test_una_fuente_sin_gancho_de_estadisticas_deja_los_contadores_en_cero() -> None:
    stats = runner(source=FakeSource(frames=3), sinks=(FakeSink(),)).run()
    assert (stats.reconnections, stats.last_reconnection_at, stats.reanchors) == (0, None, 0)


def test_run_project_construye_corre_y_devuelve_los_contadores(tmp_path: Path) -> None:
    config = configuracion(tmp_path, analyzers=[], sinks=[])
    stats = run_project(
        config,
        max_frames=3,
        registries=registros(),
        clock=RelojFalso(),
        timer=CronometroFalso(),
        sleeper=lambda _s: None,
    )
    assert stats.frames == 3
    assert stats.run_id
    assert FakeSource.ultima.cierres == 1


# --------------------------------------------------------------------------------------
# Lo que este modulo duplica a proposito
# --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_el_backoff_del_runner_dice_lo_mismo_que_el_de_las_fuentes() -> None:
    """El runner reimplementa `espera_de_backoff` para no importar OpenCV; que las dos
    versiones no se separen tiene que comprobarlo alguien, y es este test."""
    from cvp.pipeline.runner import _espera_de_backoff
    from cvp.sources.live import espera_de_backoff

    escalera = [1.0, 2.0, 5.0]
    for intento in range(1, 8):
        assert _espera_de_backoff(escalera, intento) == espera_de_backoff(escalera, intento)
