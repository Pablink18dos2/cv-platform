"""Los tres sinks de verdad, dentro del pipeline: `cvp run` escribe en los tres.

Es el nivel "component" de `_docs/architecture.md`: fuente, detector, tracker y analizador
**falsos**, y sinks **de verdad**. Los dobles son los bloques que no se estan probando aqui
—y que costarian tres segundos de OpenCV y ultralytics—; lo que se prueba es que un
`project.yaml` con `sqlite`, `jsonl` y `preview` acaba escribiendo en la base, en el fichero
de lineas y en el JPEG, sin que el runner sepa que existe ninguno de los tres.

Todo ocurre dentro de `tmp_path` (`monkeypatch.chdir`), porque la `url` por defecto del
sink `sqlite` y la ruta del `preview` son relativas al directorio desde el que se lanza el
worker.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from sqlmodel import Session

from cvp.core import (
    SINKS,
    AnalyzerOutput,
    Event,
    Frame,
    Metric,
    SourceInfo,
    Track,
    event_from_dict,
    metric_from_dict,
)
from cvp.core.registry import Registry
from cvp.pipeline import ProjectConfig, load_project_config
from cvp.pipeline.runner import PipelineRunner
from cvp.sinks import JsonlSink, PreviewSink, SqliteSink
from cvp.storage import crear_engine, eventos_de, metricas_de, minutos_de

INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)
PROYECTO = "01-parking-trafico"

PROJECT_YAML: dict[str, Any] = {
    "id": PROYECTO,
    "name": "Parking y tráfico",
    "source": {"type": "webcam", "target_fps": 5},
    "detector": {"type": "yolo", "classes": ["car"]},
    "tracker": {"type": "bytetrack"},
    "analyzers": [
        {
            "type": "traffic_flow",
            "every_n_frames": 1,
            "lines": [
                {"id": "principal", "p1": [0.1, 0.2], "p2": [0.8, 0.7], "positive_direction": "n"}
            ],
        }
    ],
    "sinks": [
        {"type": "sqlite"},
        {"type": "preview"},
        {"type": "jsonl", "path": "eventos.jsonl"},
    ],
}


# --------------------------------------------------------------------------------------
# Los dobles de los bloques que aqui no se prueban
# --------------------------------------------------------------------------------------


class FuenteFalsa:
    """Emite `frames` fotogramas de color plano, uno por segundo de video."""

    def __init__(self, *, frames: int = 6) -> None:
        self.n = frames
        self.cierres = 0

    @classmethod
    def from_config(cls, cfg: Any) -> FuenteFalsa:
        return cls()

    @property
    def info(self) -> SourceInfo:
        return SourceInfo(width=48, height=32, native_fps=5.0, is_live=False)

    def open(self) -> None:
        return None

    def frames(self) -> Iterator[Frame]:
        for i in range(self.n):
            yield Frame(
                image=np.full((32, 48, 3), 40 + i * 20, dtype=np.uint8),
                index=i,
                # 25 segundos por fotograma: seis fotogramas cruzan tres minutos, que es lo
                # que hace que la tabla de agregados tenga mas de una fila.
                timestamp=INICIO + timedelta(seconds=25 * i),
            )

    def close(self) -> None:
        self.cierres += 1


class DetectorFalso:
    @classmethod
    def from_config(cls, cfg: Any) -> DetectorFalso:
        return cls()

    def warmup(self) -> None:
        return None

    def detect(self, frame: Frame, roi: Any = None) -> list[Any]:
        return []


class TrackerFalso:
    def __init__(self, *, frame_rate: float = 5.0) -> None:
        self.frame_rate = frame_rate

    @classmethod
    def from_config(cls, cfg: Any, *, frame_rate: float) -> TrackerFalso:
        return cls(frame_rate=frame_rate)

    def update(self, frame: Frame, detections: list[Any]) -> list[Track]:
        return []

    def reset(self) -> None:
        return None


class AnalizadorFalso:
    """Emite una metrica por fotograma y un evento cada dos.

    Ocupa el sitio de #10 y #11, que todavia no existen: lo que este fichero prueba es el
    camino desde `AnalyzerOutput` hasta el disco, no lo que decide el analizador.
    """

    def __init__(self, *, every_n_frames: int = 1) -> None:
        self.name = "traffic_flow"
        self.every_n_frames = every_n_frames

    @classmethod
    def from_config(cls, cfg: Any) -> AnalizadorFalso:
        return cls(every_n_frames=cfg.every_n_frames)

    def process(self, frame: Frame, tracks: list[Track]) -> AnalyzerOutput:
        metrica = Metric(
            analyzer=self.name,
            name="vehicles_in_scene",
            value=float(frame.index),
            timestamp=frame.timestamp,
            labels={"direction": "norte"},
        )
        eventos = ()
        if frame.index % 2 == 0:
            eventos = (
                Event(
                    analyzer=self.name,
                    name="line_crossing",
                    timestamp=frame.timestamp,
                    attributes={"line": "principal", "track_id": frame.index},
                ),
            )
        return AnalyzerOutput(events=eventos, metrics=(metrica,))

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray:
        return image


def registros() -> Mapping[str, Registry[Any]]:
    """Dobles para los cuatro primeros bloques y **los sinks de verdad**."""
    tabla: dict[str, Registry[Any]] = {
        "source": Registry("source"),
        "detector": Registry("detector"),
        "tracker": Registry("tracker"),
        "analyzers": Registry("analyzer"),
        "sinks": SINKS,
    }
    tabla["source"].register("webcam")(FuenteFalsa)
    tabla["detector"].register("yolo")(DetectorFalso)
    tabla["tracker"].register("bytetrack")(TrackerFalso)
    tabla["analyzers"].register("traffic_flow")(AnalizadorFalso)
    return tabla


def configuracion(tmp_path: Path, **cambios: Any) -> ProjectConfig:
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(PROJECT_YAML | cambios, allow_unicode=True), encoding="utf-8")
    return load_project_config(fichero, env={})


def corredor(config: ProjectConfig) -> PipelineRunner:
    return PipelineRunner.from_config(
        config,
        registries=registros(),
        clock=lambda: INICIO,
        timer=CronometroQueAvanza(),
        sleeper=lambda _s: None,
    )


class CronometroQueAvanza:
    """Cronometro monotono que avanza un milisegundo por llamada: determinista y sin dormir."""

    def __init__(self) -> None:
        self.valor = 0.0

    def __call__(self) -> float:
        self.valor += 0.001
        return self.valor


# --------------------------------------------------------------------------------------
# Los tres sinks configurados escriben en los tres
# --------------------------------------------------------------------------------------


def test_los_tres_sinks_configurados_escriben_en_los_tres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = configuracion(tmp_path)
    ejecucion = corredor(config)
    stats = ejecucion.run()

    # 1. La base de datos: las filas crudas y los agregados por minuto.
    motor = crear_engine("sqlite:///data/cvp.sqlite3")
    try:
        with Session(motor) as session:
            metricas = metricas_de(session, stats.run_id)
            eventos = eventos_de(session, stats.run_id)
            minutos = minutos_de(session, PROYECTO)
    finally:
        motor.dispose()
    assert [f.value for f in metricas] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(eventos) == 3
    # Seis fotogramas cada 25 s reparten las medidas en tres minutos: 3 + 2 + 1.
    assert [m.samples for m in minutos] == [3, 2, 1]

    # 2. El JSONL: una linea por evento y por metrica, y se lee de vuelta.
    lineas = (tmp_path / "eventos.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 9
    objetos = []
    for linea in lineas:
        datos = json.loads(linea)
        assert datos.pop("run_id") == stats.run_id
        tipo = datos.pop("type")
        objetos.append(event_from_dict(datos) if tipo == "event" else metric_from_dict(datos))
    assert sum(isinstance(o, Event) for o in objetos) == 3

    # 3. El preview: el ultimo fotograma, en la ruta que se deriva del proyecto.
    preview = tmp_path / "data" / "preview" / f"{PROYECTO}.jpg"
    assert preview.is_file()
    assert preview.stat().st_size > 0

    assert stats.sink_errors == 0
    assert stats.sink_writes == 18  # 6 fotogramas x 3 sinks


def test_dos_ejecuciones_seguidas_acumulan_en_los_mismos_minutos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El reinicio del worker, visto desde el pipeline entero: `cvp run` dos veces sobre la
    misma base, con el mismo video de mentira, y los minutos suman en vez de reemplazarse.

    La segunda ejecucion estrena `run_id` (los dos arrancan en el mismo segundo del reloj
    falso y el sufijo los distingue), sink nuevo y agregador nuevo; la fila del minuto es la
    misma.
    """
    monkeypatch.chdir(tmp_path)
    primera = corredor(configuracion(tmp_path)).run()
    segunda = corredor(configuracion(tmp_path)).run()
    assert primera.run_id != segunda.run_id

    motor = crear_engine("sqlite:///data/cvp.sqlite3")
    try:
        with Session(motor) as session:
            minutos = minutos_de(session, PROYECTO)
            assert len(metricas_de(session, primera.run_id)) == 6
            assert len(metricas_de(session, segunda.run_id)) == 6
    finally:
        motor.dispose()

    # Los mismos tres minutos, cada uno con el doble de muestras que con una ejecucion.
    assert [m.samples for m in minutos] == [6, 4, 2]
    # Y el tiempo tambien suma. La fuente falsa reproduce la misma linea de tiempo las dos
    # veces, asi que las dos ejecuciones cubren **el mismo** minuto y la fila junta 2 x 60 s:
    # es lo que pasa con un fichero reproducido dos veces, y la media sigue siendo la
    # ponderada de las dos observaciones. Con un directo nunca se solapan.
    assert minutos[0].seconds == pytest.approx(2 * 60.0)
    # El segundo minuto: :75 cubre [60, 87,5) y :100 cubre [87,5, 115): 55 s por ejecucion.
    assert minutos[1].seconds == pytest.approx(2 * 55.0)
    # El tercero: su unica muestra es la ultima de cada ejecucion y no tiene tramo.
    assert minutos[2].seconds == 0.0
    assert (minutos[0].min_value, minutos[0].max_value) == (0.0, 2.0)


def test_la_ejecucion_queda_apuntada_con_su_proyecto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La fila de `run` la crea el sink con el `project_id` que le pasa la fabrica: es la
    unica manera de que la API sepa de que proyecto es una ejecucion."""
    monkeypatch.chdir(tmp_path)
    stats = corredor(configuracion(tmp_path)).run()

    motor = crear_engine("sqlite:///data/cvp.sqlite3")
    try:
        with Session(motor) as session:
            from cvp.storage.models import Project, Run

            ejecucion = session.get(Run, stats.run_id)
            assert ejecucion is not None
            assert ejecucion.project_id == PROYECTO
            proyecto = session.get(Project, PROYECTO)
            assert proyecto is not None and proyecto.name == "Parking y tráfico"
    finally:
        motor.dispose()


def test_los_tres_sinks_se_resuelven_por_su_nombre_en_el_registro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El runner no sabe que existen: los construye el registro desde el `type:` del YAML."""
    monkeypatch.chdir(tmp_path)
    assert SINKS.names() == ("jsonl", "preview", "sqlite")

    ejecucion = corredor(configuracion(tmp_path))
    construidos = [type(s) for s in ejecucion._sinks]
    assert construidos == [SqliteSink, PreviewSink, JsonlSink]


def test_un_sink_de_verdad_que_falla_no_tumba_el_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#8 lo garantiza con dobles; aqui se comprueba con los sinks de verdad.

    Se rompe el `jsonl` apuntandolo a un directorio: escribir ahi falla en cualquier sistema
    operativo, y es un fallo tan real como el disco lleno.
    """
    monkeypatch.chdir(tmp_path)
    ejecucion = corredor(configuracion(tmp_path))
    roto = next(s for s in ejecucion._sinks if isinstance(s, JsonlSink))
    (tmp_path / "un-directorio").mkdir()
    roto.path = tmp_path / "un-directorio"

    stats = ejecucion.run()

    assert stats.frames == 6
    assert stats.sink_errors == 6
    assert stats.sink_writes == 12
    # Y los otros dos han escrito igual.
    assert (tmp_path / "data" / "preview" / f"{PROYECTO}.jpg").is_file()
    motor = crear_engine("sqlite:///data/cvp.sqlite3")
    try:
        with Session(motor) as session:
            assert len(metricas_de(session, stats.run_id)) == 6
    finally:
        motor.dispose()


def test_el_flush_del_cierre_escribe_lo_que_quedaba_pendiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con un lote grande, todo queda pendiente hasta el `flush()` del `finally` del runner.

    Es la razon por la que agrupar es seguro en un apagado ordenado, incluido el de Ctrl+C.
    """
    monkeypatch.chdir(tmp_path)
    ejecucion = corredor(configuracion(tmp_path))
    almacen = next(s for s in ejecucion._sinks if isinstance(s, SqliteSink))
    almacen.batch_size = 10_000
    almacen.max_interval_s = 3600.0

    stats = ejecucion.run()

    motor = crear_engine("sqlite:///data/cvp.sqlite3")
    try:
        with Session(motor) as session:
            assert len(metricas_de(session, stats.run_id)) == 6
    finally:
        motor.dispose()
