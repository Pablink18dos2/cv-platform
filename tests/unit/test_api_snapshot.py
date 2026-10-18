"""`GET /projects/{id}/snapshot.jpg`: el fichero del `preview`, y la mitigacion de #27.

El fichero lo escribe el `PreviewSink` real. Lo que se comprueba, ademas de que se sirve,
es que **despues de responder no queda ningun descriptor abierto** sobre el: en Windows,
el `os.replace` del sink falla si alguien lo tiene abierto, y QA midio 48 escrituras
fallidas de 199 con un lector que lo mantenia abierto (#27).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from api_soporte import PROYECTO, Base, VigilanteDeFicheros, read_bytes_que_falla
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

import cvp.api.preview
import cvp.sinks.preview
from cvp.api import crear_app
from cvp.api.preview import PreviewOcupado, leer_fotograma_con_reintentos

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


class _AsgiVigilado:
    """Envuelve la app y anota, en cada `send` de la respuesta, que descriptores sobre el
    fichero siguen abiertos **en ese instante**.

    Es lo que distingue "leer entero y cerrar antes de responder" de "cerrar al terminar
    de transmitir": un `FileResponse` cierra el fichero al acabar, asi que mirar despues
    de la respuesta no lo distingue; mirar mientras se manda el cuerpo, si.
    """

    def __init__(self, app: Any, vigilante: VigilanteDeFicheros) -> None:
        self.app = app
        self.vigilante = vigilante
        self.durante: list[tuple[str, int, int]] = []  # (tipo, abiertos, sin cerrar)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def send_vigilado(mensaje: dict[str, Any]) -> None:
            if mensaje["type"] in ("http.response.start", "http.response.body"):
                self.durante.append(
                    (
                        mensaje["type"],
                        len(self.vigilante.abiertos),
                        len(self.vigilante.sin_cerrar),
                    )
                )
            await send(mensaje)

        await self.app(scope, receive, send_vigilado)


def test_el_fichero_se_ha_leido_y_cerrado_antes_de_mandar_la_respuesta(
    base: Base, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En el momento de mandar las cabeceras el fichero ya se ha abierto y ya esta
    cerrado; durante el cuerpo no hay nada abierto. Es el criterio al pie de la letra."""
    ruta = base.preview(color=50)
    vigilante = VigilanteDeFicheros(monkeypatch, ruta)
    envoltorio = _AsgiVigilado(crear_app(base.settings(), clock=base.reloj), vigilante)

    with TestClient(envoltorio) as cliente:
        assert cliente.get(URL).status_code == 200

    tipos = [t for t, _, _ in envoltorio.durante]
    assert tipos[0] == "http.response.start" and "http.response.body" in tipos
    for tipo, abiertos, sin_cerrar in envoltorio.durante:
        assert abiertos >= 1, f"al mandar {tipo} el fichero aun no se habia leido"
        assert sin_cerrar == 0, f"al mandar {tipo} habia {sin_cerrar} descriptor(es) abierto(s)"


def test_el_vigilante_si_cazaria_un_file_response(
    base: Base, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El test de arriba solo vale si distingue la mutacion que QA vio sobrevivir: una
    app que sirve el mismo fichero con `FileResponse` mantiene el descriptor abierto
    mientras manda el cuerpo, y el envoltorio lo tiene que ver."""
    ruta = base.preview(color=50)
    vigilante = VigilanteDeFicheros(monkeypatch, ruta)
    mutante = FastAPI()

    @mutante.get("/snapshot.jpg")
    def _con_file_response() -> FileResponse:
        return FileResponse(ruta, media_type="image/jpeg")

    envoltorio = _AsgiVigilado(mutante, vigilante)
    with TestClient(envoltorio) as cliente:
        assert cliente.get("/snapshot.jpg").status_code == 200

    assert any(sin_cerrar > 0 for _, _, sin_cerrar in envoltorio.durante), (
        "el envoltorio no ha visto el descriptor abierto de FileResponse: no vale como test"
    )
    # Y despues de la respuesta ya esta cerrado: por eso mirar "despues" no distinguia nada.
    assert vigilante.sin_cerrar == []


# --- La carrera del otro lado: el sink esta reemplazando el fichero -------------------


def test_si_el_sink_esta_reemplazando_el_fichero_el_snapshot_reintenta_y_responde(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PermissionError` en `read_bytes` una vez —la carrera de #27 vista desde el lector,
    determinista— y la respuesta es un 200 con el fotograma, no un 500."""
    ruta = base.preview(color=50)
    llamadas = read_bytes_que_falla(monkeypatch, 1)

    respuesta = cliente.get(URL)
    assert respuesta.status_code == 200
    assert respuesta.content == ruta.read_bytes()
    assert llamadas[:2] == [1, 2]  # fallo, reintento


def test_si_el_sink_no_suelta_el_fichero_es_503_con_la_forma_de_error(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    base.preview(color=50)
    read_bytes_que_falla(monkeypatch, 10_000)

    respuesta = cliente.get(URL)
    assert respuesta.status_code == 503
    assert respuesta.headers["retry-after"] == "1"
    assert respuesta.json() == {
        "error": {
            "code": "snapshot_busy",
            "message": (
                f"el worker esta escribiendo el fotograma de {PROYECTO!r} ahora mismo; "
                "vuelve a pedirlo"
            ),
        }
    }


def test_leer_con_reintentos_espera_entre_intentos_y_se_rinde_al_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = tmp_path / "p.jpg"
    ruta.write_bytes(b"jpeg")
    esperas: list[float] = []

    read_bytes_que_falla(monkeypatch, 2)
    fotograma = leer_fotograma_con_reintentos(ruta, intentos=3, espera_s=0.5, dormir=esperas.append)
    assert fotograma is not None and fotograma.datos == b"jpeg"
    assert esperas == [0.5, 0.5]

    esperas.clear()
    read_bytes_que_falla(monkeypatch, 10)
    with pytest.raises(PreviewOcupado):
        leer_fotograma_con_reintentos(ruta, intentos=3, espera_s=0.5, dormir=esperas.append)
    assert esperas == [0.5, 0.5]  # no se duerme despues del ultimo intento

    with pytest.raises(ValueError, match="intentos"):
        leer_fotograma_con_reintentos(ruta, intentos=0)


def test_un_fichero_que_desaparece_sigue_siendo_404_no_503(
    base: Base, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    base.preview(color=50)
    read_bytes_que_falla(monkeypatch, 1, error=FileNotFoundError)
    assert cliente.get(URL).status_code == 404


def test_una_excepcion_sin_manejar_sale_con_la_forma_unica_de_error(base: Base) -> None:
    """Un 500 no deberia salir nunca; si sale, no puede ser un `text/plain` que el
    cliente no sabe leer. Se provoca con un reloj sin zona, que `ahora` rechaza."""
    from datetime import datetime

    base.preview(color=50)
    app = crear_app(base.settings(), clock=lambda: datetime(2026, 8, 24, 14, 20))
    with TestClient(app, raise_server_exceptions=False) as cliente:
        respuesta = cliente.get("/api/v1/projects")
    assert respuesta.status_code == 500
    assert respuesta.headers["content-type"].startswith("application/json")
    error = respuesta.json()["error"]
    assert error["code"] == "internal_error"
    assert "zona horaria" in error["message"]


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
