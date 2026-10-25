"""El replayer del despliegue ligero (`scripts/replay_seed.py`) sobre un seed minusculo."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("replay_seed", RAIZ / "scripts" / "replay_seed.py")
assert _spec is not None and _spec.loader is not None
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)

ESQUEMA = """
CREATE TABLE run (id TEXT, started_at TEXT, ended_at TEXT);
CREATE TABLE metric_minute (id INTEGER PRIMARY KEY, project_id TEXT, minute TEXT, analyzer TEXT,
  name TEXT, labels TEXT, samples INT, sum_value REAL, min_value REAL, max_value REAL,
  seconds REAL, integral REAL, mean_value REAL);
CREATE TABLE event (id INTEGER PRIMARY KEY, run_id TEXT, analyzer TEXT, name TEXT,
  timestamp TEXT, attributes TEXT);
"""


def _seed(ruta: Path) -> Path:
    con = sqlite3.connect(ruta)
    con.executescript(ESQUEMA)
    con.execute("INSERT INTO run VALUES ('r1', '2026-01-01 10:00:30.000000', NULL)")
    for minuto, valor in (("10:00", 1.0), ("10:01", 2.0)):
        con.execute(
            "INSERT INTO metric_minute (project_id, minute, analyzer, name, labels, samples, "
            "sum_value, min_value, max_value, seconds, integral, mean_value) VALUES "
            f"('p', '2026-01-01 {minuto}:00.000000', 'a', 'n', '{{}}', 1, 1, 1, 1, 60, 1, ?)",
            (valor,),
        )
    con.execute(
        "INSERT INTO event (run_id, analyzer, name, timestamp, attributes) "
        "VALUES ('r1', 'a', 'e', '2026-01-01 10:01:20.500000', '{}')"
    )
    con.commit()
    con.close()
    return ruta


def test_preparar_vuelca_los_minutos_del_seed_terminando_en_el_ultimo_minuto(tmp_path: Path):
    seed = _seed(tmp_path / "seed.sqlite3")
    destino = tmp_path / "data" / "cvp.sqlite3"
    ultimo = datetime(2030, 5, 5, 12, 7)

    for _ in range(2):  # idempotente: arrancar otra vez deja el mismo estado
        replay.preparar(seed, destino, ultimo)

    db = sqlite3.connect(destino)
    minutos = [m for (m,) in db.execute("SELECT minute FROM metric_minute ORDER BY minute")]
    assert minutos == ["2030-05-05 12:06:00.000000", "2030-05-05 12:07:00.000000"]
    # El evento conserva su segundo dentro del minuto, desplazado igual que el minuto.
    assert db.execute("SELECT timestamp FROM event").fetchall() == [("2030-05-05 12:07:20.500000",)]
    assert db.execute("SELECT started_at, ended_at FROM run").fetchone() == (
        "2030-05-05 12:06:30.000000",
        "2030-05-05 12:08:00.000000",
    )


def test_insertar_minuto_del_seed_con_la_fecha_de_ahora(tmp_path: Path):
    seed = _seed(tmp_path / "seed.sqlite3")
    destino = tmp_path / "cvp.sqlite3"
    replay.preparar(seed, destino, datetime(2030, 5, 5, 12, 7))

    origen = sqlite3.connect(seed)
    db = sqlite3.connect(destino)
    replay.insertar_minuto(origen, db, datetime(2026, 1, 1, 10, 1), datetime(2030, 5, 5, 12, 8))
    db.commit()

    filas = db.execute("SELECT minute, mean_value FROM metric_minute ORDER BY minute").fetchall()
    assert filas[-1] == ("2030-05-05 12:08:00.000000", 2.0)
    assert len(filas) == 3
    assert db.execute("SELECT count(*) FROM event").fetchone() == (2,)
    assert db.execute("SELECT ended_at FROM run").fetchone() == ("2030-05-05 12:09:00.000000",)
