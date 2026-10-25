"""Exporta de la base local el seed del despliegue ligero: `deploy/seed.sqlite3`.

Copia el esquema completo y solo las filas que sirve la API (`project`, `run`,
`metric_minute`, `event`). La tabla `metric` (muestras crudas, ~28k filas) no la lee ninguna
ruta de `cvp.api`, asi que se queda fuera. Tambien copia el ultimo fotograma anotado real.

    uv run python scripts/make_seed.py [data/cvp.sqlite3]
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

TABLAS_CON_DATOS = ("project", "run", "metric_minute", "event")
PROYECTO = "01-parking-trafico"


def main(origen: str = "data/cvp.sqlite3", destino: str = "deploy/seed.sqlite3") -> None:
    salida = Path(destino)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.unlink(missing_ok=True)

    con = sqlite3.connect(salida)
    con.execute("ATTACH DATABASE ? AS src", (str(origen),))
    # El esquema entero (tablas e indices), para que el seed sea una base valida de cvp.
    for (sql,) in con.execute(
        "SELECT sql FROM src.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type = 'index'"
    ).fetchall():
        con.execute(sql)
    for tabla in TABLAS_CON_DATOS:
        con.execute(f"INSERT INTO {tabla} SELECT * FROM src.{tabla}")
    con.commit()
    con.execute("DETACH DATABASE src")
    con.execute("VACUUM")
    con.close()

    preview = Path(origen).parent / "preview" / f"{PROYECTO}.jpg"
    shutil.copyfile(preview, salida.parent / "seed-preview.jpg")
    print(f"{salida}: {salida.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main(*sys.argv[1:])
