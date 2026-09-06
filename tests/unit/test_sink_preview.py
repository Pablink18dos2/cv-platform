"""El sink `preview`: el ultimo fotograma en disco, escrito de forma atomica.

Lo que se prueba aqui es lo que #9 decidio en su segunda inconsistencia: que el preview es
un fichero y no un buffer en memoria (worker y API son procesos distintos), y que la
escritura no puede dejar a la API sirviendo un JPEG a medio hacer.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

import cvp.sinks.preview
from cvp.core import AnalyzerOutput, Frame, Metric
from cvp.sinks.preview import DIRECTORIO_PREVIEW, PreviewSink, ruta_de_preview

RUN = "20260824T142018Z-2808cc47"
INICIO = datetime(2026, 8, 24, 14, 20, 0, tzinfo=UTC)


def fotograma(indice: int = 0, color: int = 128) -> Frame:
    """Un fotograma de color plano: el JPEG lo conserva casi exacto y cualquier cosa que se
    pintara encima se veria."""
    imagen = np.full((32, 48, 3), color, dtype=np.uint8)
    return Frame(image=imagen, index=indice, timestamp=INICIO + timedelta(seconds=indice))


def decodificar(path: Path) -> np.ndarray:
    imagen = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert imagen is not None, "el fichero no es un JPEG que OpenCV sepa leer"
    return imagen


def temporales(carpeta: Path) -> list[Path]:
    return [p for p in carpeta.iterdir() if p.name.endswith(".tmp")]


# --- Lo que escribe -------------------------------------------------------------------


def test_guarda_el_ultimo_fotograma_como_jpeg(tmp_path: Path) -> None:
    destino = tmp_path / "01.jpg"
    PreviewSink(destino).write(RUN, fotograma(color=200), AnalyzerOutput.empty())

    imagen = decodificar(destino)
    assert imagen.shape == (32, 48, 3)
    assert np.allclose(imagen, 200, atol=3)


def test_el_fichero_es_siempre_el_ultimo_fotograma_y_no_se_acumulan(tmp_path: Path) -> None:
    sink = PreviewSink(tmp_path / "01.jpg")
    for i, color in enumerate([10, 90, 240]):
        sink.write(RUN, fotograma(i, color), AnalyzerOutput.empty())

    assert [p.name for p in tmp_path.iterdir()] == ["01.jpg"]
    assert np.allclose(decodificar(tmp_path / "01.jpg"), 240, atol=3)


def test_no_anota_el_fotograma(tmp_path: Path) -> None:
    """Los analizadores que dibujan son #10 y #11; hasta entonces se guarda tal cual, y el
    docstring del sink lo dice."""
    destino = tmp_path / "01.jpg"
    salida = AnalyzerOutput(
        metrics=(Metric(analyzer="traffic_flow", name="vehicles", value=3.0, timestamp=INICIO),)
    )
    PreviewSink(destino).write(RUN, fotograma(color=128), salida)

    imagen = decodificar(destino)
    assert np.allclose(imagen, 128, atol=3)
    # Y esta escrito donde se lee, que es lo que pide el criterio.
    assert "No anota" in (cvp.sinks.preview.__doc__ or "")


def test_el_mismo_fotograma_no_se_codifica_dos_veces(tmp_path: Path) -> None:
    """El runner llama a los sinks una vez por analizador: con dos analizadores el mismo
    fotograma llega dos veces, y codificar un JPEG que ya esta escrito es trabajo tirado."""
    sink = PreviewSink(tmp_path / "01.jpg")
    frame = fotograma(7)
    sink.write(RUN, frame, AnalyzerOutput.empty())
    sink.write(RUN, frame, AnalyzerOutput.empty())

    assert sink.escrituras == 1


# --- La escritura atomica -------------------------------------------------------------


def test_escribir_muchos_fotogramas_no_deja_temporales_huerfanos(tmp_path: Path) -> None:
    sink = PreviewSink(tmp_path / "01.jpg")
    for i in range(50):
        sink.write(RUN, fotograma(i, color=i * 4), AnalyzerOutput.empty())

    assert temporales(tmp_path) == []
    assert len(list(tmp_path.iterdir())) == 1


def test_el_temporal_va_en_el_mismo_directorio_que_el_destino(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.replace` solo es atomico dentro del mismo sistema de ficheros: con el temporal
    en `/tmp` seria una copia, y volveria el agujero que esto viene a tapar."""
    vistos: list[tuple[Path, Path]] = []
    original = os.replace

    def espia(origen: str, destino: str) -> None:
        vistos.append((Path(origen), Path(destino)))
        original(origen, destino)

    monkeypatch.setattr("cvp.sinks.preview.os.replace", espia)
    PreviewSink(tmp_path / "01.jpg").write(RUN, fotograma(), AnalyzerOutput.empty())

    (origen, destino) = vistos[0]
    assert origen.parent == destino.parent == tmp_path


def test_si_falla_la_codificacion_no_queda_ni_un_resto_ni_se_pisa_lo_anterior(
    tmp_path: Path,
) -> None:
    """El fichero que sirve la API se queda como estaba: entero y del fotograma anterior."""
    destino = tmp_path / "01.jpg"
    fallos = {"quedan": 0}

    def codificador(imagen: np.ndarray, calidad: int) -> bytes:
        if fallos["quedan"]:
            fallos["quedan"] -= 1
            raise RuntimeError("OpenCV no ha podido codificar el fotograma")
        return bytes(cv2.imencode(".jpg", imagen)[1].tobytes())

    sink = PreviewSink(destino, encoder=codificador)
    sink.write(RUN, fotograma(0, color=30), AnalyzerOutput.empty())
    antes = destino.read_bytes()

    fallos["quedan"] = 1
    with pytest.raises(RuntimeError):
        sink.write(RUN, fotograma(1, color=200), AnalyzerOutput.empty())

    assert destino.read_bytes() == antes
    assert temporales(tmp_path) == []


def test_si_falla_al_escribir_el_temporal_se_borra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso feo: el fallo llega despues de crear el temporal. Escribir miles de
    fotogramas no puede ir dejando restos en el directorio que sirve la API."""

    def revienta(origen: str, destino: str) -> None:
        raise OSError("el disco esta lleno")

    monkeypatch.setattr("cvp.sinks.preview.os.replace", revienta)
    sink = PreviewSink(tmp_path / "01.jpg")
    with pytest.raises(OSError, match="disco"):
        sink.write(RUN, fotograma(), AnalyzerOutput.empty())

    assert list(tmp_path.iterdir()) == []


def test_flush_no_tiene_nada_que_hacer(tmp_path: Path) -> None:
    """Cada fotograma ya se escribio entero o no se escribio: no hay nada pendiente."""
    sink = PreviewSink(tmp_path / "01.jpg")
    sink.write(RUN, fotograma(), AnalyzerOutput.empty())
    sink.flush()
    assert (tmp_path / "01.jpg").exists()


# --- La ruta ---------------------------------------------------------------------------


def test_la_ruta_se_deriva_del_proyecto(tmp_path: Path) -> None:
    """Es la convencion que comparten el worker (que escribe) y la API (que sirve)."""
    assert ruta_de_preview("01-parking-trafico") == DIRECTORIO_PREVIEW / "01-parking-trafico.jpg"
    assert ruta_de_preview("otro", base=tmp_path) == tmp_path / "otro.jpg"


def test_from_config_escribe_donde_dice_la_convencion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cfg:
        type = "preview"
        max_frames = 2

    monkeypatch.chdir(tmp_path)
    sink = PreviewSink.from_config(Cfg(), project_id="01-parking-trafico", project_name="P")
    sink.write(RUN, fotograma(), AnalyzerOutput.empty())

    assert (tmp_path / "data" / "preview" / "01-parking-trafico.jpg").is_file()


def test_el_sink_crea_su_directorio_al_arrancar(tmp_path: Path) -> None:
    PreviewSink(tmp_path / "sin" / "crear" / "01.jpg")
    assert (tmp_path / "sin" / "crear").is_dir()


def test_una_calidad_imposible_se_rechaza_al_construir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="calidad JPEG"):
        PreviewSink(tmp_path / "01.jpg", quality=0)
