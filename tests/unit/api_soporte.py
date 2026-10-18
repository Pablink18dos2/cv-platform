"""Lo que comparten los tests de la API: una base poblada por los sinks reales y un reloj.

Es el criterio de higiene de #12: la base SQLite va en un fichero temporal y se llena
**con `SqliteSink` y `PreviewSink`** a partir de `AnalyzerOutput` construidos a mano, no
con `INSERT`. Asi la API se prueba contra lo que el worker escribe de verdad, incluida la
agregacion ponderada por tiempo y sus bordes (#29), y un cambio en el sink que rompiera
la API se veria aqui y no en el dashboard.

Es un modulo y no el `conftest.py` para que los tests puedan importar de el por nombre
(`en`, `metrica`, `RUN`...); `conftest.py` solo registra las fixtures que lo envuelven.
"""

from __future__ import annotations

import builtins
import io
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from cvp.api import Settings
from cvp.core import AnalyzerOutput, Event, Frame, Metric

RUN = "20260824T142018Z-2808cc47"
OTRO_RUN = "20260824T143000Z-9f1b0c33"
PROYECTO = "01-parking-trafico"
NOMBRE = "Parking y tráfico"
INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)


def en(segundos: float) -> datetime:
    """`INICIO` mas `segundos`: el eje de tiempo de todos los tests de la API."""
    return INICIO + timedelta(seconds=segundos)


def fotograma(indice: int = 0, color: int = 128) -> Frame:
    imagen = np.full((16, 24, 3), color, dtype=np.uint8)
    return Frame(image=imagen, index=indice, timestamp=en(indice))


def metrica(
    valor: float,
    segundos: float,
    *,
    name: str = "vehicles_in_scene",
    analyzer: str = "traffic_flow",
    labels: dict[str, str] | None = None,
) -> Metric:
    return Metric(
        analyzer=analyzer, name=name, value=valor, timestamp=en(segundos), labels=labels or {}
    )


def evento(
    segundos: float,
    *,
    name: str = "line_crossing",
    analyzer: str = "traffic_flow",
    attributes: dict[str, Any] | None = None,
) -> Event:
    return Event(
        analyzer=analyzer,
        name=name,
        timestamp=en(segundos),
        attributes=attributes if attributes is not None else {"direction": "norte"},
    )


class RelojFalso:
    """Un reloj que da la hora que el test le pone. Es lo que `crear_app` recibe en `clock`."""

    def __init__(self, ahora: datetime = INICIO) -> None:
        self.ahora = ahora

    def __call__(self) -> datetime:
        return self.ahora


@dataclass
class Base:
    """Una base temporal, con su directorio de preview al lado, y como se puebla."""

    directorio: Path
    reloj: RelojFalso = field(default_factory=RelojFalso)
    _sinks: list[Any] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"sqlite:///{(self.directorio / 'cvp.sqlite3').as_posix()}"

    @property
    def preview_dir(self) -> Path:
        return self.directorio / "preview"

    def sink(self, project_id: str = PROYECTO, project_name: str = NOMBRE, **kwargs: Any) -> Any:
        """Un `SqliteSink` real sobre esta base. Se cierra al terminar el test."""
        from cvp.sinks.sqlite import SqliteSink

        argumentos: dict[str, Any] = {"batch_size": 1000, "max_interval_s": 3600.0}
        argumentos.update(kwargs)
        almacen = SqliteSink(
            self.url, project_id=project_id, project_name=project_name, **argumentos
        )
        self._sinks.append(almacen)
        return almacen

    def escribir(
        self,
        *salidas: AnalyzerOutput,
        run_id: str = RUN,
        project_id: str = PROYECTO,
        project_name: str = NOMBRE,
    ) -> None:
        """Escribe las salidas como un solo lote del worker y hace flush."""
        almacen = self.sink(project_id, project_name)
        for i, salida in enumerate(salidas):
            almacen.write(run_id, fotograma(i), salida)
        almacen.flush()

    def escribir_metricas(self, *metricas: Metric, **kwargs: Any) -> None:
        self.escribir(AnalyzerOutput(metrics=metricas), **kwargs)

    def escribir_eventos(self, *eventos: Event, **kwargs: Any) -> None:
        self.escribir(AnalyzerOutput(events=eventos), **kwargs)

    def preview(self, project_id: str = PROYECTO, color: int = 128, indice: int = 0) -> Path:
        """Escribe un fotograma con el `PreviewSink` real y devuelve su ruta."""
        from cvp.sinks.preview import PreviewSink, ruta_de_preview

        ruta = ruta_de_preview(project_id, base=self.preview_dir)
        PreviewSink(ruta).write(RUN, fotograma(indice, color), AnalyzerOutput.empty())
        return ruta

    def settings(self, **cambios: Any) -> Settings:
        ajustes: dict[str, Any] = {"database_url": self.url, "preview_dir": self.preview_dir}
        return Settings(**(ajustes | cambios))

    def cerrar(self) -> None:
        for almacen in self._sinks:
            almacen.engine.dispose()
        self._sinks.clear()


class VigilanteDeFicheros:
    """Anota cada objeto de fichero que se abre sobre `ruta`, por cualquier camino."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, ruta: Path) -> None:
        self.ruta = ruta.resolve()
        self.abiertos: list[Any] = []
        original = io.open

        def vigilado(fichero: Any, *args: Any, **kwargs: Any) -> Any:
            objeto = original(fichero, *args, **kwargs)
            try:
                if isinstance(fichero, str | os.PathLike) and Path(fichero).resolve() == self.ruta:
                    self.abiertos.append(objeto)
            except OSError:  # pragma: no cover - una ruta que no se puede resolver
                pass
            return objeto

        monkeypatch.setattr(io, "open", vigilado)
        monkeypatch.setattr(builtins, "open", vigilado)

    @property
    def sin_cerrar(self) -> list[Any]:
        return [f for f in self.abiertos if not f.closed]


def read_bytes_que_falla(
    monkeypatch: pytest.MonkeyPatch, veces: int, *, error: type[OSError] = PermissionError
) -> list[int]:
    """Parchea `Path.read_bytes` para que lance `error` las primeras `veces` llamadas.

    Es la carrera de #27 vista desde el lector, reproducida de forma determinista como
    hizo QA: en Windows, abrir el fichero mientras el sink hace `os.replace` es un
    `PermissionError [Errno 13]`. Devuelve la lista donde se cuentan las llamadas.
    """
    original = Path.read_bytes
    llamadas: list[int] = []

    def falso(self: Path) -> bytes:
        llamadas.append(len(llamadas) + 1)
        if len(llamadas) <= veces:
            raise error(13, "Permission denied", str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", falso)
    return llamadas
