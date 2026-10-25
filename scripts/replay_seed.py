"""Reproduce en bucle la ejecucion real grabada en `deploy/seed.sqlite3`, con fechas de ahora.

Es la fuente de datos del despliegue ligero (`docker/render.Dockerfile`): no hay deteccion ni
worker, solo filas que ya existian. Al arrancar copia el seed a la base de trabajo (se
reconstruye entera, asi que arrancar dos veces da el mismo estado) y vuelca todos sus minutos
terminando en el minuto anterior al actual; despues, cada minuto, inserta el siguiente minuto
del seed (en bucle) con el timestamp de ese minuto. No toca `cvp.core`: solo habla SQL.

    python scripts/replay_seed.py [seed] [base_de_trabajo]
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

FORMATO = "%Y-%m-%d %H:%M:%S.%f"  # como guarda SQLAlchemy los DateTime: UTC sin zona
UN_MINUTO = timedelta(minutes=1)
COLUMNAS = (
    "project_id, minute, analyzer, name, labels, samples, "
    "sum_value, min_value, max_value, seconds, integral, mean_value"
)


def _fecha(texto: str) -> datetime:
    return datetime.strptime(texto, FORMATO)


def _texto(fecha: datetime) -> str:
    return fecha.strftime(FORMATO)


def minuto_de(marca: datetime) -> datetime:
    return marca.replace(second=0, microsecond=0)


def minutos_del_seed(seed: sqlite3.Connection) -> list[datetime]:
    filas = seed.execute("SELECT DISTINCT minute FROM metric_minute ORDER BY minute").fetchall()
    return [_fecha(minuto) for (minuto,) in filas]


def insertar_minuto(
    seed: sqlite3.Connection, db: sqlite3.Connection, origen: datetime, destino: datetime
) -> None:
    """Copia el minuto `origen` del seed a `db` como si hubiera pasado en `destino`."""
    marcadores = ", ".join("?" * 12)
    for fila in seed.execute(
        f"SELECT {COLUMNAS} FROM metric_minute WHERE minute = ?", (_texto(origen),)
    ):
        db.execute(
            f"INSERT INTO metric_minute ({COLUMNAS}) VALUES ({marcadores})",
            (fila[0], _texto(destino), *fila[2:]),
        )
    desplazamiento = destino - origen
    for run_id, analyzer, name, marca, atributos in seed.execute(
        "SELECT run_id, analyzer, name, timestamp, attributes FROM event "
        "WHERE timestamp >= ? AND timestamp < ?",
        (_texto(origen), _texto(origen + UN_MINUTO)),
    ):
        db.execute(
            "INSERT INTO event (run_id, analyzer, name, timestamp, attributes) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, analyzer, name, _texto(_fecha(marca) + desplazamiento), atributos),
        )
    # `ended_at` es el instante de la ultima muestra: el final del minuto que se acaba de cerrar.
    db.execute("UPDATE run SET ended_at = ?", (_texto(destino + UN_MINUTO),))


def preparar(seed_path: Path, destino: Path, ultimo_minuto: datetime) -> list[datetime]:
    """Deja en `destino` el seed con todos sus minutos terminando en `ultimo_minuto`.

    Se escribe en un fichero temporal y se renombra, para que la API nunca vea una base a medias.
    """
    temporal = destino.with_name(destino.name + ".tmp")
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal.unlink(missing_ok=True)
    shutil.copyfile(seed_path, temporal)
    seed = sqlite3.connect(f"file:{Path(seed_path).as_posix()}?mode=ro", uri=True)
    db = sqlite3.connect(temporal)
    minutos = minutos_del_seed(seed)
    db.execute("DELETE FROM event")
    db.execute("DELETE FROM metric_minute")
    primero = ultimo_minuto - (len(minutos) - 1) * UN_MINUTO
    (inicio,) = seed.execute("SELECT started_at FROM run").fetchone()
    db.execute("UPDATE run SET started_at = ?", (_texto(_fecha(inicio) + (primero - minutos[0])),))
    for i, origen in enumerate(minutos):
        insertar_minuto(seed, db, origen, primero + i * UN_MINUTO)
    db.commit()
    db.close()
    seed.close()
    for sufijo in ("-wal", "-shm"):
        Path(str(destino) + sufijo).unlink(missing_ok=True)
    os.replace(temporal, destino)
    return minutos


def main(seed_path: str = "deploy/seed.sqlite3", destino: str | None = None) -> None:
    url = os.environ.get("DATABASE_URL", "sqlite:///data/cvp.sqlite3")
    base = Path(destino or url.removeprefix("sqlite:///"))
    ahora = datetime.now(UTC).replace(tzinfo=None)
    ultimo = minuto_de(ahora) - UN_MINUTO
    minutos = preparar(Path(seed_path), base, ultimo)
    print(f"replay: {len(minutos)} minutos grabados volcados hasta {ultimo:%H:%M} UTC", flush=True)
    siguiente = 0
    while True:
        ahora = datetime.now(UTC).replace(tzinfo=None)
        time.sleep((minuto_de(ahora) + UN_MINUTO - ahora).total_seconds() + 2)
        ahora = datetime.now(UTC).replace(tzinfo=None)
        seed = sqlite3.connect(f"file:{Path(seed_path).as_posix()}?mode=ro", uri=True)
        db = sqlite3.connect(base, timeout=30)
        insertar_minuto(seed, db, minutos[siguiente % len(minutos)], minuto_de(ahora) - UN_MINUTO)
        db.commit()
        db.close()
        seed.close()
        siguiente += 1


if __name__ == "__main__":
    main(*sys.argv[1:])
