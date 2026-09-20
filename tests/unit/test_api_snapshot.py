"""`GET /projects/{id}/snapshot.jpg`: el fichero del `preview`, y la mitigacion de #27.

El fichero lo escribe el `PreviewSink` real. Lo que se comprueba, ademas de que se sirve,
es que **despues de responder no queda ningun descriptor abierto** sobre el: en Windows,
el `os.replace` del sink falla si alguien lo tiene abierto, y QA midio 48 escrituras
fallidas de 199 con un lector que lo mantenia abierto (#27).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api_soporte import PROYECTO, Base, VigilanteDeFicheros
from fastapi.testclient import TestClient

import cvp.api.preview
import cvp.sinks.preview

URL = f"/api/v1/projects/{PROYECTO}/snapshot.jpg"


# --- Lo que sirve ------------------------------------------------------------------------


def test_sirve_el_jpeg_del_preview_sin_cache(base: Base, cliente: TestClient) -> None:
    """`base.preview` escribe con el `PreviewSink` real en la ruta del sink; la API la
    deriva por su cuenta (`cvp.api.preview`) y tiene que caer en el mismo fichero."""
    ruta = base.preview(color=200)
    respuesta = cliente.get(URL)

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "image/jpeg"
    assert respuesta.headers["cache-control"] == "no-store"
    assert respuesta.content == ruta.read_bytes()
    assert respuesta.content[:3] == b"\xff\xd8\xff"  # un JPEG de verdad, no un placeholder


def test_sirve_siempre_la_ultima_version_del_fichero(base: Base, cliente: TestClient) -> None:
    base.preview(color=10, indice=0)
    primero = cliente.get(URL).content
    base.preview(color=240, indice=1)
    segundo = cliente.get(URL).content
    assert primero != segundo
    assert segundo == base.preview_dir.joinpath(f"{PROYECTO}.jpg").read_bytes()


def test_sin_fichero_es_404_snapshot_not_found(base: Base, cliente: TestClient) -> None:
    respuesta = cliente.get(URL)
    assert respuesta.status_code == 404
    assert respuesta.json() == {
        "error": {
            "code": "snapshot_not_found",
            "message": f"el proyecto {PROYECTO!r} no tiene todavia ningun fotograma en disco",
        }
    }


def test_un_id_sin_forma_de_proyecto_es_404_sin_tocar_el_disco(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Es lo que impide que `{id}` lleve un `..` o una barra invertida hasta fuera de
    `data/preview`: se rechaza antes de derivar la ruta."""
    lecturas: list[Path] = []
    original = cvp.api.preview.leer_fotograma
    monkeypatch.setattr(
        cvp.api.preview, "leer_fotograma", lambda ruta: (lecturas.append(ruta), original(ruta))[1]
    )
    for malo in ["%2e%2e", "a%5c..%5cb", "NO", "x%2fy"]:
        respuesta = cliente.get(f"/api/v1/projects/{malo}/snapshot.jpg")
        assert respuesta.status_code == 404, malo
        assert respuesta.json()["error"]["code"] in {"project_not_found", "not_found"}, malo
    assert lecturas == []


# --- La mitigacion de #27 ----------------------------------------------------------------


def test_tras_responder_no_queda_ningun_descriptor_abierto_sobre_el_fichero(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Se vigila cada `open` sobre el fichero: los que la respuesta abrio estan todos
    cerrados cuando el cliente tiene el cuerpo, y un `os.replace` del sink sobre el
    fichero —lo que en Windows falla con el fichero abierto— funciona justo despues."""
    ruta = base.preview(color=50)
    vigilante = VigilanteDeFicheros(monkeypatch, ruta)

    respuesta = cliente.get(URL)
    assert respuesta.status_code == 200
    assert len(vigilante.abiertos) >= 1, "la respuesta no ha leido el fichero por `open`"
    assert vigilante.sin_cerrar == []

    # El sink puede reemplazarlo: es exactamente la operacion que #27 vio fallar.
    base.preview(color=90, indice=1)
    assert cliente.get(URL).content == ruta.read_bytes()


def test_leer_fotograma_abre_lee_y_cierra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ruta = tmp_path / "p.jpg"
    ruta.write_bytes(b"\xff\xd8\xff" + b"x" * 100)
    vigilante = VigilanteDeFicheros(monkeypatch, ruta)

    fotograma = cvp.api.preview.leer_fotograma(ruta)
    assert fotograma is not None
    assert fotograma.datos == ruta.read_bytes()
    assert fotograma.version == (ruta.stat().st_mtime_ns, 103)
    assert vigilante.sin_cerrar == []
    assert cvp.api.preview.leer_fotograma(tmp_path / "no.jpg") is None


# --- La convencion de la ruta, escrita dos veces ------------------------------------------


def test_la_api_deriva_la_misma_ruta_que_el_sink() -> None:
    """`cvp.api.preview.ruta_de_preview` es una copia de la del sink porque importar el
    sink arrastra cv2. Este test es lo que impide que las dos copias se separen."""
    assert cvp.api.preview.DIRECTORIO_PREVIEW == cvp.sinks.preview.DIRECTORIO_PREVIEW
    for project_id in ("01-parking-trafico", "x"):
        assert cvp.api.preview.ruta_de_preview(project_id) == cvp.sinks.preview.ruta_de_preview(
            project_id
        )
        assert cvp.api.preview.ruta_de_preview(
            project_id, base="/otro"
        ) == cvp.sinks.preview.ruta_de_preview(project_id, base="/otro")
    with pytest.raises(ValueError):
        cvp.api.preview.ruta_de_preview("")
