"""El contrato de `deploy/demo/timeline.json`, que consume el front de la demo desplegada.

Lo genera `scripts/make_demo_clip.py` y se versiona junto a `clip.mp4`. Aqui solo se
comprueba la forma y que los tiempos cuadren con el video.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[2] / "deploy" / "demo"
TIMELINE = DEMO / "timeline.json"
CLIP = DEMO / "clip.mp4"

pytestmark = pytest.mark.skipif(not TIMELINE.exists(), reason="no hay clip de demo generado")


@pytest.fixture(scope="module")
def timeline() -> dict:
    return json.loads(TIMELINE.read_text(encoding="utf-8"))


def test_la_cabecera_cumple_el_contrato(timeline):
    assert timeline["version"] == 1
    assert isinstance(timeline["source"], str) and "Collingwood" in timeline["source"]
    assert timeline["video"] == "/demo/clip.mp4"
    assert isinstance(timeline["spaces_total"], int) and timeline["spaces_total"] > 0
    assert isinstance(timeline["duration_s"], float) and timeline["duration_s"] > 0


def test_la_serie_tiene_una_muestra_por_segundo_y_valores_validos(timeline):
    serie, total = timeline["series"], timeline["spaces_total"]
    assert len(serie) == int(timeline["duration_s"])
    for segundo, muestra in enumerate(serie):
        assert muestra["t"] == segundo
        assert isinstance(muestra["spaces_free"], int) and 0 <= muestra["spaces_free"] <= total
        assert isinstance(muestra["vehicles_in_scene"], int) and muestra["vehicles_in_scene"] >= 0
        assert set(muestra["crossings"]) == {"hacia_camara", "hacia_fondo"}
        assert all(isinstance(v, int) and v >= 0 for v in muestra["crossings"].values())


def test_los_eventos_estan_ordenados_dentro_del_clip(timeline):
    eventos = timeline["events"]
    tiempos = [e["t"] for e in eventos]
    assert tiempos == sorted(tiempos)
    for evento in eventos:
        assert set(evento) == {"t", "name", "zone"}
        assert evento["name"] in {"space_freed", "space_occupied"}
        assert isinstance(evento["zone"], str) and evento["zone"]
        assert 0 <= evento["t"] < timeline["duration_s"]


def test_los_eventos_explican_los_cambios_de_plazas_libres(timeline):
    """Cada segundo, la variacion de `spaces_free` es la de los eventos de ese segundo."""
    serie, eventos = timeline["series"], timeline["events"]
    for antes, despues in zip(serie, serie[1:], strict=False):
        t0, t1 = antes["t"], despues["t"]
        delta = sum(1 if e["name"] == "space_freed" else -1 for e in eventos if t0 < e["t"] <= t1)
        assert despues["spaces_free"] - antes["spaces_free"] == delta, f"entre {t0} y {t1} s"


@pytest.mark.skipif(
    not CLIP.exists() or shutil.which("ffprobe") is None, reason="sin clip.mp4 o sin ffprobe"
)
def test_la_duracion_del_json_coincide_con_la_del_video(timeline):
    salida = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(CLIP)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    assert float(salida) == pytest.approx(timeline["duration_s"], abs=0.5)
